#!/usr/bin/env python3
# -*- coding: UTF-8 -*-
'''Telegram Notes — saves Telegram messages into your notebook.

This is a WebBridge background service. It runs as a subprocess and
communicates with Moonstone exclusively through the WebBridge REST API.

Setup:
  1. Install this service via the WebBridge dashboard or API
  2. Configure bot_token via PUT /api/services/telegram-notes/config
  3. Start the service via POST /api/services/telegram-notes/start
'''

import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

# Find moonstone_sdk via MOONSTONE_SDK_PATH env var (set by ServiceManager)
sdk_env = os.environ.get('MOONSTONE_SDK_PATH')
if sdk_env and os.path.isfile(os.path.join(sdk_env, 'moonstone_sdk.py')):
    sys.path.insert(0, sdk_env)

try:
    from moonstone_sdk import MoonstoneAPI, MoonstoneAPIError, load_config, save_state, load_state, setup_logging
except ImportError:
    # Fallback: minimal inline SDK
    print('WARNING: moonstone_sdk not found, using inline fallback', file=sys.stderr)
    import urllib.request, urllib.error, urllib.parse

    class MoonstoneAPIError(Exception):
        pass

    class MoonstoneAPI:
        def __init__(self, base_url=None, auth_token=None, timeout=15):
            self.base_url = (base_url or os.environ.get('MOONSTONE_API_URL', 'http://localhost:8090/api')).rstrip('/')
            self.auth_token = auth_token or os.environ.get('MOONSTONE_AUTH_TOKEN', '')
            self.timeout = timeout

        def _request(self, method, path, data=None):
            url = '%s/%s' % (self.base_url, path.lstrip('/'))
            headers = {'Content-Type': 'application/json'}
            if self.auth_token:
                headers['X-Auth-Token'] = self.auth_token
            body = json.dumps(data, ensure_ascii=False).encode('utf-8') if data else None
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                resp = urllib.request.urlopen(req, timeout=self.timeout)
                return json.loads(resp.read().decode('utf-8'))
            except Exception as e:
                raise MoonstoneAPIError(str(e))

        def post(self, path, data=None): return self._request('POST', path, data)
        def get(self, path): return self._request('GET', path)
        def append(self, page, content, format='wiki'):
            safe = page.replace(':', '/')
            return self.post('page/%s/append' % safe, {'content': content, 'format': format})
        def create_page(self, page, content='', format='wiki'):
            safe = page.replace(':', '/')
            return self.post('page/%s' % safe, {'content': content, 'format': format})
        def get_page(self, page, format='wiki'):
            safe = page.replace(':', '/')
            return self.get('page/%s?format=%s' % (safe, format))
        def upload_attachment(self, page_path, filename, raw_bytes):
            safe = page_path.replace(":", "/")
            safe_file = urllib.parse.quote(filename)
            url = '%s/attachment/%s/%s' % (self.base_url, safe, safe_file)
            headers = {'Content-Type': 'application/octet-stream'}
            if self.auth_token:
                headers['X-Auth-Token'] = self.auth_token
            req = urllib.request.Request(url, data=raw_bytes, headers=headers, method='POST')
            try:
                resp = urllib.request.urlopen(req, timeout=self.timeout)
                return json.loads(resp.read().decode('utf-8'))
            except Exception as e:
                raise MoonstoneAPIError(str(e))
        def wait_for_api(self, max_wait=30, interval=1):
            deadline = time.time() + max_wait
            while time.time() < deadline:
                try:
                    self.get('notebook')
                    return True
                except:
                    time.sleep(interval)
            return False

    def load_config():
        data_dir = os.environ.get('MOONSTONE_SERVICE_DATA_DIR', '_data')
        cf = os.path.join(data_dir, '_config.json')
        if os.path.isfile(cf):
            with open(cf, 'r') as f:
                return json.load(f)
        return {}

    def save_state(key, value):
        data_dir = os.environ.get('MOONSTONE_SERVICE_DATA_DIR', '_data')
        os.makedirs(data_dir, exist_ok=True)
        with open(os.path.join(data_dir, '%s.json' % key), 'w') as f:
            json.dump(value, f)

    def load_state(key, default=None):
        data_dir = os.environ.get('MOONSTONE_SERVICE_DATA_DIR', '_data')
        fp = os.path.join(data_dir, '%s.json' % key)
        if os.path.isfile(fp):
            with open(fp, 'r') as f:
                return json.load(f)
        return default

    def setup_logging(level=logging.INFO):
        logging.basicConfig(level=level, format='%(asctime)s [%(name)s] %(levelname)s: %(message)s', datefmt='%H:%M:%S')


# ---------------------------------------------------------------------------
# Service logic
# ---------------------------------------------------------------------------

logger = logging.getLogger('telegram-notes')


def get_target_page(config):
    '''Determine the target page path based on config.'''
    base = config.get('target_page', 'Inbox:Telegram')
    if config.get('date_subpages', True):
        date_str = datetime.now().strftime('%Y-%m-%d')
        return '%s:%s' % (base, date_str)
    return base


def get_todo_page(config):
    '''Determine the ToDo page path based on config.'''
    return config.get('todo_page', 'Inbox:Tasks')


async def format_message(api, message, page_path):
    '''Format a Telegram message as wiki markup, parsing HTML if available and saving attachments.'''
    user = message.from_user
    username = user.first_name or user.username or 'Unknown'
    ts = message.date.strftime('%H:%M') if message.date else '??:??'

    lines = []
    lines.append('')  # blank line before entry

    def convert_html(html_text):
        if not html_text:
            return ""
        # Very simple conversion
        t = html_text
        t = t.replace('<b>', '**').replace('</b>', '**')
        t = t.replace('<strong>', '**').replace('</strong>', '**')
        t = t.replace('<i>', '//').replace('</i>', '//')
        t = t.replace('<em>', '//').replace('</em>', '//')
        t = t.replace('<code>', "''").replace('</code>', "''")
        t = t.replace('<pre>', "'''\n").replace('</pre>', "\n'''")
        return t

    # Text message
    if message.text:
        lines.append('**[%s] %s:**' % (ts, username))
        try:
            html = message.text_html
            lines.append(convert_html(html))
        except Exception:
            lines.append(message.text)

    # Photo
    elif message.photo:
        file = await message.photo[-1].get_file()
        file_bytes = await file.download_as_bytearray()
        ext = file.file_path.split('.')[-1] if '.' in file.file_path else 'jpg'
        filename = f"photo_{message.message_id}.{ext}"
        
        api.upload_attachment(page_path, filename, file_bytes)
        lines.append('**[%s] %s:** {{./%s}}' % (ts, username, filename))
        if message.caption:
            lines.append(message.caption)

    # Document
    elif message.document:
        doc_name = message.document.file_name or 'document'
        file = await message.document.get_file()
        file_bytes = await file.download_as_bytearray()
        
        # Ensure unique name to avoid collision
        filename = f"{message.message_id}_{doc_name}"
        api.upload_attachment(page_path, filename, file_bytes)
        
        lines.append('**[%s] %s:** [[./%s|%s]]' % (ts, username, filename, doc_name))
        if message.caption:
            try:
                lines.append(convert_html(message.caption_html))
            except Exception:
                lines.append(message.caption)

    # Voice
    elif message.voice:
        file = await message.voice.get_file()
        file_bytes = await file.download_as_bytearray()
        ext = file.file_path.split('.')[-1] if '.' in file.file_path else 'ogg'
        filename = f"voice_{message.message_id}.{ext}"
        
        api.upload_attachment(page_path, filename, file_bytes)
        
        duration = message.voice.duration or 0
        lines.append('**[%s] %s:** [[./%s|Voice message (%ds)]]' % (ts, username, filename, duration))

    # Video
    elif message.video:
        file = await message.video.get_file()
        file_bytes = await file.download_as_bytearray()
        ext = file.file_path.split('.')[-1] if '.' in file.file_path else 'mp4'
        filename = f"video_{message.message_id}.{ext}"
        
        api.upload_attachment(page_path, filename, file_bytes)
        
        lines.append('**[%s] %s:** [[./%s|Video message]]' % (ts, username, filename))
        if message.caption:
            lines.append(message.caption)

    # Video Note
    elif message.video_note:
        file = await message.video_note.get_file()
        file_bytes = await file.download_as_bytearray()
        ext = file.file_path.split('.')[-1] if '.' in file.file_path else 'mp4'
        filename = f"video_note_{message.message_id}.{ext}"
        
        api.upload_attachment(page_path, filename, file_bytes)
        
        lines.append('**[%s] %s:** [[./%s|Video note]]' % (ts, username, filename))

    # Sticker
    elif message.sticker:
        emoji = message.sticker.emoji or '🙂'
        lines.append('**[%s] %s:** %s //[sticker]//' % (ts, username, emoji))

    # Location
    elif message.location:
        lat = message.location.latitude
        lon = message.location.longitude
        lines.append('**[%s] %s:** //[[https://maps.google.com/?q=%f,%f|📍 Location]]//' % (
            ts, username, lat, lon))

    # Forward
    elif message.forward_date:
        lines.append('**[%s] %s:** //[forwarded message]//' % (ts, username))

    else:
        lines.append('**[%s] %s:** //[unsupported message type]//' % (ts, username))

    lines.append('')  # blank line after entry
    return '\n'.join(lines)


def ensure_page_exists(api, page_path):
    '''Create the target page if it doesn't exist yet.'''
    try:
        result = api.get_page(page_path)
        if not result.get('exists', False):
            title = page_path.split(':')[-1]
            header = '====== %s ======\n\n' % title
            api.create_page(page_path, header)
            logger.info('Created page: %s', page_path)
    except MoonstoneAPIError as e:
        if e.status == 404 or 'not found' in str(e).lower():
            title = page_path.split(':')[-1]
            header = '====== %s ======\n\n' % title
            try:
                api.create_page(page_path, header)
                logger.info('Created page: %s', page_path)
            except MoonstoneAPIError:
                pass  # May already exist (race condition)


async def run_bot(api, config):
    '''Run the Telegram bot.'''
    from telegram import Update
    from telegram.ext import Application, MessageHandler, CommandHandler, filters

    bot_token = config.get('bot_token', '')
    if not bot_token:
        logger.error('No bot_token configured! Set it via /api/services/telegram-notes/config')
        logger.error('Example: PUT {"bot_token": "123456:ABC..."} to /api/services/telegram-notes/config')
        # Keep running but check for config changes periodically
        while True:
            await asyncio.sleep(10)
            config = load_config()
            bot_token = config.get('bot_token', '')
            if bot_token:
                logger.info('Bot token detected, restarting...')
                break
        # Restart with new config
        return await run_bot(api, config)

    # Parse allowed users
    allowed_str = config.get('allowed_users', '')
    allowed_ids = set()
    if allowed_str:
        for uid in allowed_str.split(','):
            uid = uid.strip()
            if uid.isdigit():
                allowed_ids.add(int(uid))

    stats = load_state('stats', {'messages_saved': 0, 'started_at': None})
    stats['started_at'] = datetime.now(timezone.utc).isoformat()
    save_state('stats', stats)

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    async def check_auth(update: Update) -> bool:
        user_id = update.effective_user.id if update.effective_user else None
        if allowed_ids and user_id not in allowed_ids:
            logger.warning('Rejected message from unauthorized user: %s', user_id)
            if update.message:
                await update.message.reply_text('⛔ You are not authorized to use this bot.')
            return False
        return True

    async def handle_message(update: Update, context):
        '''Handle incoming Telegram messages.'''
        if not update.message:
            return

        if not await check_auth(update):
            return

        try:
            page_path = get_target_page(config)
            ensure_page_exists(api, page_path)
            wiki_text = await format_message(api, update.message, page_path)
            api.append(page_path, wiki_text)

            stats['messages_saved'] = stats.get('messages_saved', 0) + 1
            save_state('stats', stats)

            logger.info('Saved message from %s to %s',
                        update.message.from_user.first_name, page_path)
            
            keyboard = [
                [InlineKeyboardButton("Open Page", callback_data=f"open:{page_path}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text('✅ Saved to %s' % page_path.replace(':', ' → '), reply_markup=reply_markup)

        except MoonstoneAPIError as e:
            logger.error('Failed to save message: %s', e)
            await update.message.reply_text('❌ Failed to save: %s' % str(e)[:100])

    async def cmd_start(update: Update, context):
        '''Handle /start command.'''
        if not await check_auth(update):
            return
            
        nb = {}
        try:
            nb = api.get_notebook_info()
        except:
            pass
        name = nb.get('name', 'your notebook')
        
        keyboard = [
            [InlineKeyboardButton("Status", callback_data="status"),
             InlineKeyboardButton("Target Page", callback_data="target")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            '📝 Hi! I save your messages to Moonstone notebook "%s".\n\n'
            'Just send me any text, link, or file — it will appear on the page:\n'
            '📄 %s\n\n'
            'Commands:\n'
            '/todo <task> — save a task\n'
            '/search <query> — search notes\n'
            '/status — check service status\n'
            '/page — show current target page\n'
            % (name, get_target_page(config).replace(':', ' → ')),
            reply_markup=reply_markup
        )

    async def cmd_status(update: Update, context):
        '''Handle /status command.'''
        s = load_state('stats', {})
        msg = '📊 Status:\n'
        msg += '• Messages saved: %d\n' % s.get('messages_saved', 0)
        msg += '• Running since: %s\n' % (s.get('started_at', 'unknown')[:19])
        msg += '• Target page: %s\n' % get_target_page(config).replace(':', ' → ')
        await update.message.reply_text(msg)

    async def cmd_page(update: Update, context):
        '''Handle /page command — show and optionally navigate to target page.'''
        if not await check_auth(update):
            return
        page = get_target_page(config)
        try:
            api.navigate(page)
            await update.message.reply_text('📄 Opened: %s' % page.replace(':', ' → '))
        except MoonstoneAPIError:
            await update.message.reply_text('📄 Target: %s' % page.replace(':', ' → '))

    async def cmd_todo(update: Update, context):
        '''Handle /todo command — save a task.'''
        if not await check_auth(update):
            return
        
        text = update.message.text.partition(' ')[2].strip()
        if not text:
            await update.message.reply_text('ℹ️ Usage: /todo <your task>')
            return
            
        try:
            page_path = get_todo_page(config)
            ensure_page_exists(api, page_path)
            wiki_text = f"\n[ ] {text}"
            api.append(page_path, wiki_text)

            logger.info('Saved task to %s', page_path)
            
            keyboard = [
                [InlineKeyboardButton("Open Page", callback_data=f"open:{page_path}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text('✅ Task saved to %s' % page_path.replace(':', ' → '), reply_markup=reply_markup)
        except MoonstoneAPIError as e:
            logger.error('Failed to save task: %s', e)
            await update.message.reply_text('❌ Failed to save task: %s' % str(e)[:100])

    async def cmd_search(update: Update, context):
        '''Handle /search command.'''
        if not await check_auth(update):
            return
            
        query = update.message.text.partition(' ')[2].strip()
        if not query:
            await update.message.reply_text('ℹ️ Usage: /search <query>')
            return
            
        try:
            result = api.search(query)
            if not result:
                await update.message.reply_text('🔍 No results found for "%s".' % query)
                return
            
            msg = '🔍 **Search results for "%s":**\n\n' % query
            keyboard = []
            
            for i, r in enumerate(result[:5]):
                name = r.get('name', 'Unknown')
                msg += '• %s\n' % name
                keyboard.append([InlineKeyboardButton("Open " + name, callback_data=f"open:{name}")])
                
            if len(result) > 5:
                msg += '\n...and %d more.' % (len(result) - 5)
                
            reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
            await update.message.reply_text(msg, reply_markup=reply_markup, parse_mode='Markdown')
            
        except MoonstoneAPIError as e:
            logger.error('Search failed: %s', e)
            await update.message.reply_text('❌ Search failed: %s' % str(e)[:100])

    from telegram.ext import CallbackQueryHandler

    async def handle_callback(update: Update, context):
        '''Handle inline button callbacks.'''
        query = update.callback_query
        if not await check_auth(update):
            await query.answer("Unauthorized", show_alert=True)
            return
            
        await query.answer()
        data = query.data
        
        if data == "status":
            s = load_state('stats', {})
            msg = '📊 Status:\n'
            msg += '• Messages saved: %d\n' % s.get('messages_saved', 0)
            msg += '• Running since: %s\n' % (s.get('started_at', 'unknown')[:19])
            msg += '• Target page: %s\n' % get_target_page(config).replace(':', ' → ')
            await query.edit_message_text(msg)
            
        elif data == "target":
            page = get_target_page(config)
            await query.edit_message_text('📄 Target: %s' % page.replace(':', ' → '))
            
        elif data.startswith("open:"):
            page = data.split(":", 1)[1]
            try:
                api.navigate(page)
                await query.edit_message_text('📄 Opened in Moonstone: %s' % page.replace(':', ' → '))
            except MoonstoneAPIError:
                await query.edit_message_text('📄 Target: %s' % page.replace(':', ' → '))

    # Build and run the bot
    app = Application.builder().token(bot_token).build()
    app.add_handler(CommandHandler('start', cmd_start))
    app.add_handler(CommandHandler('status', cmd_status))
    app.add_handler(CommandHandler('page', cmd_page))
    app.add_handler(CommandHandler('todo', cmd_todo))
    app.add_handler(CommandHandler('search', cmd_search))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    logger.info('Starting Telegram bot polling...')
    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

        # Run until stopped
        stop_event = asyncio.Event()

        def _signal_handler(sig, frame):
            logger.info('Received signal %s, shutting down...', sig)
            stop_event.set()

        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)

        await stop_event.wait()

    finally:
        logger.info('Stopping bot...')
        try:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
        except Exception as e:
            logger.debug('Shutdown error: %s', e)


def main():
    setup_logging()
    logger.info('=== Telegram Notes Service starting ===')
    logger.info('API URL: %s', os.environ.get('MOONSTONE_API_URL', 'not set'))
    logger.info('Service name: %s', os.environ.get('MOONSTONE_SERVICE_NAME', 'not set'))
    logger.info('Data dir: %s', os.environ.get('MOONSTONE_SERVICE_DATA_DIR', 'not set'))

    api = MoonstoneAPI()

    # Wait for WebBridge API to be available
    logger.info('Waiting for WebBridge API...')
    if not api.wait_for_api(max_wait=30):
        logger.error('WebBridge API not reachable after 30s, exiting')
        sys.exit(1)

    logger.info('API is reachable')

    # Load config
    config = load_config()
    logger.info('Config loaded: target_page=%s, date_subpages=%s',
                config.get('target_page', 'Inbox:Telegram'),
                config.get('date_subpages', True))

    if not config.get('bot_token'):
        logger.warning('No bot_token set! Configure via API:')
        logger.warning('  PUT /api/services/telegram-notes/config')
        logger.warning('  Body: {"bot_token": "YOUR_TOKEN_HERE"}')
        logger.warning('Will keep checking for config changes...')

    # Run the async bot
    try:
        asyncio.run(run_bot(api, config))
    except KeyboardInterrupt:
        logger.info('Service interrupted')
    except Exception as e:
        logger.exception('Service crashed: %s', e)
        sys.exit(1)

    logger.info('=== Telegram Notes Service stopped ===')


if __name__ == '__main__':
    main()
