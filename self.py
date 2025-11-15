from telethon import TelegramClient, events, functions, types
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import DocumentAttributeVideo, DocumentAttributeSticker, InputStickerSetEmpty, DocumentAttributeAudio
from telethon.errors.rpcerrorlist import MessageNotModifiedError
from telethon.errors import ChatWriteForbiddenError, MessageDeleteForbiddenError, FloodWaitError

import asyncio
import os
import requests
import logging
import subprocess
import pytz
import math
import json
import base64
import zipfile
import pyzipper
import io
import re
import uuid
import time
import pymysql
import PyPDF2
import aiohttp
import shutil 
import qrcode
import io


from datetime import datetime, timedelta 
from geopy.geocoders import Nominatim
from PIL import Image
from mutagen.easyid3 import EasyID3
from mutagen.mp3 import MP3
from io import BytesIO
from cachetools import TTLCache
from contextlib import contextmanager
from functools import wraps
from bs4 import BeautifulSoup
from mutagen.mp4 import MP4
from mutagen.flac import FLAC
from mutagen.oggvorbis import OggVorbis
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image
import numpy as np
from PIL import Image


# Configuration
TELEGRAM_CONFIG = {
    'api_id': '*********',
    'api_hash': '********************************',
    'phone_number': '+**************',
    'session_name': 'selfbot'
}

BOT_CONFIG = {
    'active': True,
    'spam_delay': 0,
    'spam_limit': 1000,
    'spam_cooldown': 4,
    'sudo_user_id': ************
}

API_KEYS = {
    'rapidapi': os.getenv('RAPIDAPI_KEY', '******************************'),
    'coinmarketcap': os.getenv('COINMARKETCAP_KEY', '********************************************')
}

API_ENDPOINTS = {
    'weather': {
        'host': 'weatherbit-v1-mashape.p.rapidapi.com',
        'headers': {
            'x-rapidapi-key': API_KEYS['rapidapi'],
            'x-rapidapi-host': 'weatherbit-v1-mashape.p.rapidapi.com'
        }
    },
    'gpt': {
        'url': 'https://chatgpt-42.p.rapidapi.com/gpt4',
        'reasoning_url': 'https://chatgpt-42.p.rapidapi.com/o3mini',
        'headers': {
            'x-rapidapi-key': API_KEYS['rapidapi'],
            'x-rapidapi-host': 'chatgpt-42.p.rapidapi.com',
            'Content-Type': 'application/json'
        }
    },
    'image_gen': {
        'url': 'https://open-ai21.p.rapidapi.com/texttoimage2',
        'headers': {
            'x-rapidapi-key': API_KEYS['rapidapi'],
            'x-rapidapi-host': 'open-ai21.p.rapidapi.com',
            'Content-Type': 'application/json'
        }
    }
}

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)
client = TelegramClient(TELEGRAM_CONFIG['session_name'], TELEGRAM_CONFIG['api_id'], TELEGRAM_CONFIG['api_hash'])
geolocator = Nominatim(user_agent='weather-bot')

# Global variables
bot_active = BOT_CONFIG['active']
spam_delay = BOT_CONFIG['spam_delay']
spam_limit = BOT_CONFIG['spam_limit']
spam_cooldown = BOT_CONFIG['spam_cooldown']
SUDO_USER_ID = BOT_CONFIG['sudo_user_id']

user_waitlists = {}
active_spam_tasks = {}
confirmation_cache = TTLCache(maxsize=100, ttl=300)
last_spam_time = {}
previous_prices = {}
currency_task = None

CURRENCY_CHANNEL = "@CurrencyPriceUpdates"
GPT_HEADERS = API_ENDPOINTS['gpt']['headers']
IMAGE_HEADERS = API_ENDPOINTS['image_gen']['headers']
GPT_API_URL = API_ENDPOINTS['gpt']['url']
GPT_REASONING_API_URL = API_ENDPOINTS['gpt']['reasoning_url']
IMAGE_GEN_API_URL = API_ENDPOINTS['image_gen']['url']
RAPIDAPI_KEY = API_KEYS['rapidapi']


##-------------------------------------COMMAND HANDLER--------------------------------------##

@contextmanager
def get_db_cursor():
    """Context manager for database connections."""
    conn = None
    try:
        conn = pymysql.connect(
            host=os.getenv('DB_HOST', 'localhost'),
            user=os.getenv('DB_USER', '*************'),
            password=os.getenv('DB_PASSWORD', '************'),
            database=os.getenv('DB_NAME', '*************'),
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True
        )
        yield conn.cursor()
    finally:
        if conn:
            conn.close()


def initialize_admin_table():
    """Initialize users table with sudo user."""
    try:
        with get_db_cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id BIGINT PRIMARY KEY,
                    role VARCHAR(50) NOT NULL DEFAULT 'admin',
                    username VARCHAR(50) DEFAULT NULL
                ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
            """)

            # Add username column if missing
            cur.execute("""
                SELECT COUNT(*) as count
                FROM information_schema.columns 
                WHERE table_schema = DATABASE()
                  AND table_name = 'users' 
                  AND column_name = 'username';
            """)
            
            if cur.fetchone()['count'] == 0:
                cur.execute("ALTER TABLE users ADD COLUMN username VARCHAR(50) DEFAULT NULL;")
                logger.info("✅ Username column added")

            # Insert sudo user
            cur.execute("INSERT IGNORE INTO users (id, role) VALUES (%s, 'sudo')", (SUDO_USER_ID,))
            
            if cur.rowcount > 0:
                logger.info(f"✅ SUDO user {SUDO_USER_ID} added")
            else:
                logger.info(f"ℹ️ SUDO user {SUDO_USER_ID} already exists")

    except Exception as e:
        logger.exception("❌ Failed to initialize users table")


def initialize_emoji_table():
    """Initialize channel emojis table."""
    try:
        with get_db_cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS channel_emojis (
                    channel_username VARCHAR(50) PRIMARY KEY,
                    emoji VARCHAR(10) NOT NULL
                ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
            """)
            logger.info("✅ Channel emoji table initialized")
    except Exception as e:
        logger.exception("❌ Failed to initialize channel emoji table")
        
        
def initialize_qreply_table():
    """Initialize quick reply table."""
    try:
        with get_db_cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS quick_replies (
                    user_id BIGINT NOT NULL,
                    alias VARCHAR(100) NOT NULL,
                    message TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, alias)
                ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
            """)
            logger.info("✅ Quick reply table initialized")
    except Exception as e:
        logger.exception("❌ Failed to initialize quick reply table")


def is_authorized(event):
    """Check if user is authorized."""
    if event.message.out:
        return True
    
    try:
        with get_db_cursor() as cur:
            cur.execute("SELECT 1 FROM users WHERE id = %s LIMIT 1", (event.sender_id,))
            return cur.fetchone() is not None
    except Exception as e:
        logger.warning(f"DB error in authorization: {e}")
        return False


def is_sudo(event):
    """Check if user has sudo privileges."""
    return event.message.out or event.sender_id == SUDO_USER_ID


def sudo_only(func):
    """Decorator to restrict commands to sudo users."""
    @wraps(func)
    async def wrapper(event, *args, **kwargs):
        if not is_sudo(event):
            logger.info(f'❌ Unauthorized sudo attempt for "{func.__name__}" from {event.sender_id}')
            return await event.reply("❌ Only the bot owner can use this command.")
        return await func(event, *args, **kwargs)
    return wrapper


initialize_admin_table()
initialize_emoji_table()
initialize_qreply_table()

@sudo_only
async def handle_setadmin(event, *args):
    """Add a user as admin."""
    if not args:
        return await event.reply("❌ Usage: `setadmin [user_id]`")
    
    try:
        user_id = int(args[0])
        user = await event.client.get_entity(user_id)
        username = user.first_name or "Unknown User"
        
        with get_db_cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, role, username) VALUES (%s, 'admin', %s) "
                "ON DUPLICATE KEY UPDATE role = 'admin', username = %s",
                (user_id, username, username)
            )
        
        await event.reply(f"✅ User {user_id} ({username}) added as admin.")
    
    except ValueError:
        await event.reply("❌ Invalid user ID. Please provide a numeric ID.")
    except Exception as e:
        logger.exception(f"Failed to set admin for user {args[0]}")
        await event.reply(f"❌ Error: {e}")


@sudo_only
async def handle_remadmin(event, *args):
    """Remove a user from admin list."""
    if not args:
        return await event.reply("❌ Usage: `remadmin [user_id]`")
    
    try:
        user_id = int(args[0])
        
        with get_db_cursor() as cur:
            cur.execute("DELETE FROM users WHERE id = %s AND role != 'sudo'", (user_id,))
            rows_affected = cur.rowcount
        
        if rows_affected == 0:
            return await event.reply(f"ℹ️ User {user_id} not found or is a sudo user.")
        
        try:
            user = await event.client.get_entity(user_id)
            username = user.first_name or "Unknown User"
            await event.reply(f"✅ User {user_id} ({username}) removed from admin list.")
        except:
            await event.reply(f"✅ User {user_id} removed from admin list.")
    
    except ValueError:
        await event.reply("❌ Invalid user ID. Please provide a numeric ID.")
    except Exception as e:
        logger.exception(f"Failed to remove admin for user {args[0]}")
        await event.reply(f"❌ Error: {e}")


@sudo_only
async def handle_adminlist(event, *args):
    """Display list of all admins and sudo users."""
    try:
        with get_db_cursor() as cur:
            cur.execute("SELECT id, role, username FROM users ORDER BY role, id")
            users = cur.fetchall()
        
        if not users:
            return await event.reply("ℹ️ No users in the database.")
        
        role_emoji = {'sudo': '👑', 'admin': '👤'}
        
        msg_lines = ["👥 **User List:**\n"]
        for user in users:
            emoji = role_emoji.get(user['role'], '•')
            username = user['username'] or "No name"
            msg_lines.append(f"{emoji} `{user['id']}` - **{user['role']}** - {username}")
        
        await event.reply("\n".join(msg_lines))
    
    except Exception as e:
        logger.exception("Failed to fetch admin list")
        await event.reply(f"❌ Error: {e}")
        
        
@sudo_only
async def handle_self_command(event, command):
    """Toggle bot active state."""
    global bot_active
    
    if command == 'on':
        bot_active = True
        await event.reply("✅ Self-bot is now active.")
    elif command == 'off':
        bot_active = False
        await event.reply("❌ Self-bot is now deactivated.")
    else:
        await event.reply("❌ Invalid command. Use: `on` or `off`")
        
        
async def handle_spam(event, *args):
    """Send a message multiple times with configurable delay."""
    global spam_limit, spam_delay, spam_cooldown

    user_id = event.sender_id
    now = time.time()

    # Validate arguments
    if len(args) < 2:
        return await event.reply("❌ Usage: `spam [message] [count]`")

    msg = ' '.join(args[:-1])
    
    try:
        count = int(args[-1])
    except ValueError:
        return await event.reply("❌ The last argument must be a valid number.")

    # Check spam limit
    if count > spam_limit:
        return await event.reply(f"❌ Spam limit exceeded. Max allowed: {spam_limit}")

    # Check cooldown
    if user_id in last_spam_time:
        elapsed = now - last_spam_time[user_id]
        if elapsed < spam_cooldown:
            remaining = spam_cooldown - elapsed
            return await event.reply(f"⏳ Please wait {remaining:.1f} seconds before trying again.")

    # Initialize spam task
    last_spam_time[user_id] = now
    active_spam_tasks[user_id] = {
        "task": asyncio.current_task(),
        "start_time": now
    }

    cancelled = False

    try:
        for i in range(count):
            # Check if task was cancelled
            if active_spam_tasks.get(user_id, {}).get("task").cancelled():
                cancelled = True
                break

            try:
                await event.respond(msg)
            except FloodWaitError as e:
                logger.warning(f"Flood wait for {e.seconds} seconds")
                
                notice = None
                try:
                    notice = await event.respond(f"😴 Flood wait: sleeping for {e.seconds} seconds…")
                    await asyncio.sleep(e.seconds)
                except asyncio.CancelledError:
                    cancelled = True
                    break
                finally:
                    if notice:
                        try:
                            await notice.delete()
                        except Exception:
                            pass
                continue

            # Apply delay (except for last message)
            if spam_delay > 0 and i < count - 1:
                try:
                    await asyncio.sleep(spam_delay)
                except asyncio.CancelledError:
                    cancelled = True
                    break

    except asyncio.CancelledError:
        cancelled = True
    except Exception as e:
        logger.error(f"Unexpected error in spam: {str(e)}")
        await event.reply(f"❌ Error: {str(e)}")
    finally:
        active_spam_tasks.pop(user_id, None)
        
        if cancelled:
            await event.reply("🛑 Spam cancelled.")
        else:
            await event.reply("✅ Spam completed.")


async def handle_spam_cancel(event, *args):
    """Cancel active spam task."""
    user_id = event.sender_id

    if user_id not in active_spam_tasks:
        return await event.reply("ℹ️ No active spam task found.")

    task = active_spam_tasks.get(user_id, {}).get("task")
    if task and not task.cancelled():
        task.cancel()
        start_time = active_spam_tasks[user_id].get("start_time", time.time())
        elapsed = time.time() - start_time
        await event.reply(f"🛑 Cancelled after {elapsed:.1f}s")


@sudo_only
async def handle_spamset(event, *args):
    """Configure spam settings: delay, limit, and cooldown."""
    global spam_delay, spam_limit, spam_cooldown

    if len(args) != 2:
        return await event.reply(
            "❌ Usage: `spamset [delay/limit/cooldown] [value]`\n(delay in ms)"
        )

    setting, value_str = args

    # Validate value
    if not value_str.isdigit():
        return await event.reply("❌ The value must be a valid non-negative integer.")

    value = int(value_str)

    # Apply settings
    setting = setting.lower()
    
    if setting == "delay":
        spam_delay = value / 1000  # Convert ms to seconds
        await event.reply(f"✅ Spam delay set to **{value} ms** ({spam_delay:.3f}s).")
    
    elif setting == "limit":
        spam_limit = value
        await event.reply(f"✅ Spam limit set to **{spam_limit}**.")
    
    elif setting == "cooldown":
        spam_cooldown = value
        await event.reply(f"✅ Spam cooldown set to **{spam_cooldown}** seconds.")
    
    else:
        await event.reply("❌ Invalid setting. Use: `delay`, `limit`, or `cooldown`.")
        
        
@sudo_only
async def handle_del(event, arg):
    """Delete messages by count or type with confirmation for bulk operations."""
    
    TYPE_MAP = {
        "sticker": "sticker",
        "photos": "photo",
        "videos": "video",
        "voices": "voice",
        "videomsgs": "video_note",
        "musics": "audio",
        "files": "document",
        "links": "webpage",
        "gifs": "gif",
        "all": "all"
    }
    
    messages_to_delete = []

    async def request_confirmation(message):
        """Request user confirmation with timeout."""
        confirmation_event = asyncio.Event()
        confirmation_response = [None]

        @client.on(events.NewMessage(chats=event.chat_id, from_users=event.sender_id))
        async def confirmation_handler(response_event):
            confirmation_response[0] = response_event.message.text.lower()
            confirmation_event.set()
            raise events.StopPropagation

        confirmation_msg = await event.reply(message)

        try:
            await asyncio.wait_for(confirmation_event.wait(), timeout=30)
            await confirmation_msg.delete()
            return confirmation_response[0] in ['yes', 'y']
        except asyncio.TimeoutError:
            await confirmation_msg.edit("⏳ Confirmation timed out. Deletion cancelled.")
            return False
        finally:
            client.remove_event_handler(confirmation_handler)

    # Delete by count
    if arg.isdigit():
        count = int(arg)
        
        if count > 10:
            confirm_msg = f"⚠️ Delete {count} messages?\nType 'yes' within 30s to confirm."
            if not await request_confirmation(confirm_msg):
                return
        
        async for msg in client.iter_messages(event.chat_id, limit=count):
            messages_to_delete.append(msg.id)

    # Delete by type
    elif arg in TYPE_MAP:
        msg_type = TYPE_MAP[arg]
        
        # Confirm "all" immediately
        if msg_type == "all":
            confirm_msg = "⚠️ **Warning:** Are you sure you want to delete **ALL** messages? This action cannot be undone! (yes/no)"
            if not await request_confirmation(confirm_msg):
                return
        
        # Collect messages
        async for msg in client.iter_messages(event.chat_id):
            if msg_type == "all" or getattr(msg, msg_type, None):
                messages_to_delete.append(msg.id)
        
        # Confirm bulk type deletion (excluding "all")
        if msg_type != "all" and len(messages_to_delete) > 10:
            confirm_msg = f"⚠️ Found **{len(messages_to_delete)}** messages. Delete all? (yes/no)"
            if not await request_confirmation(confirm_msg):
                return

    # Unsupported type
    else:
        supported = ', '.join(f"`{k}`" for k in TYPE_MAP.keys())
        return await event.reply(f"❌ Unsupported message type `{arg}`.\n\n**Supported types:**\n{supported}")

    # Execute deletion
    if messages_to_delete:
        await client.delete_messages(event.chat_id, messages_to_delete)
        await event.reply(f"✅ Deleted **{len(messages_to_delete)}** messages.")
    else:
        await event.reply(f"ℹ️ No `{arg}` messages found to delete.")
        
        

async def handle_info(event, *args):
    """Get detailed user information including profile photo/video."""
    
    loading_msg = await event.reply("⏳ Retrieving user info...")

    def format_user_info(user, full_user):
        """Format user information."""
        premium = getattr(user, 'premium', False)
        premium_emoji = "💎" if premium else "🔹"
        
        name = f"{user.first_name or ''} {user.last_name or ''}".strip()
        username = f"@{user.username}" if getattr(user, 'username', None) else "No username"
        
        bio = None
        try:
            bio = getattr(full_user.full_user, 'about', None)
        except AttributeError:
            pass
        
        bio = bio or "No bio"
        premium_status = "Yes" if premium else "No"

        return (
            f"🌟 **User Information** 🌟\n\n"
            f"📌 **Name:** {name}\n"
            f"📌 **Username:** {username}\n"
            f"📌 **User ID:** `{user.id}`\n"
            f"📌 **Biography:** {bio}\n"
            f"📌 **Premium Status:** {premium_status} {premium_emoji}\n"
        )

    try:
        # Determine target user
        if event.is_reply:
            replied_msg = await event.get_reply_message()
            if not replied_msg:
                raise ValueError("No reply message found")
            user_id = replied_msg.sender_id
        elif args:
            target = args[0]
            try:
                user_id = int(target)
            except ValueError:
                user_id = target
        else:
            user_id = 'me'

        # Fetch user info
        user = await client.get_entity(user_id)
        full_user = await client(GetFullUserRequest(user.id))

        # Download profile media
        profile_photo = None
        profile_video = None

        if getattr(user, 'photo', None):
            # Check if profile has video
            if getattr(user.photo, 'has_video', False):
                try:
                    # Download profile video
                    profile_video = await client.download_media(
                        user.photo,
                        file="user_profile_video.mp4"
                    )
                    logger.info(f"Downloaded profile video for user {user.id}")
                except Exception as e:
                    logger.warning(f"Failed to download profile video: {e}")
                    # Fallback to photo if video fails
                    profile_photo = await client.download_profile_photo(
                        user, file="user_profile.jpg", download_big=True
                    )
            else:
                # Download regular profile photo
                profile_photo = await client.download_profile_photo(
                    user, file="user_profile.jpg", download_big=True
                )

        await loading_msg.delete()

        caption = format_user_info(user, full_user)

        # Send profile media with caption
        if profile_video:
            await client.send_file(event.chat_id, profile_video, caption=caption)
            os.remove(profile_video)
            logger.info(f"Sent profile video for user {user.id}")
        elif profile_photo:
            await client.send_file(event.chat_id, profile_photo, caption=caption)
            os.remove(profile_photo)
            logger.info(f"Sent profile photo for user {user.id}")
        else:
            await event.reply(caption)

    except ValueError:
        err_target = args[0] if args else "Unknown"
        await loading_msg.edit(f"❌ Cannot find entity `{err_target}`.")
    except Exception as e:
        await loading_msg.edit(f"❌ Unexpected error: {str(e)}")
        logger.exception("Error in handle_info")
        

async def handle_user_help(event):
    """Display command help."""
    
    help_text = (
        "📋 **Bot Commands**\n\n"
        
        "**Control**\n"
        "`self on/off` - Toggle bot\n\n"
        
        "**Messaging**\n"
        "`spam [msg] [count]` - Spam messages\n"
        "`spamset [delay/limit/cooldown] [value]`\n"
        "`cancel` - Stop spam\n\n"
        
        "**Quick Reply**\n"
        "`qreply set [alias] [msg]` - Create\n"
        "`qreply set [alias]` - From reply\n"
        "`qreply remove [alias]` - Delete\n"
        "`qreply list` - Show all\n"
        "`-[alias]` - Use quick reply\n\n"
        
        "**Delete**\n"
        "`del [count]` - Delete N messages\n"
        "`del [type]` - Delete by type\n\n"
        
        "**Info**\n"
        "`info [user]` - User details\n"
        "`help` - This message\n\n"
        
        "**Weather & Finance**\n"
        "`dw [city]` - Daily forecast\n"
        "`hw [city]` - Hourly forecast\n"
        "`currency` - Live prices\n\n"
        
        "**Downloads**\n"
        "`annas [book]` - Search books\n"
        "`art [article]` - Search articles\n\n"
        
        "**Convert**\n"
        "`tts` - Text to speech (reply)\n"
        "`topdf en/fa [size]` - Text to PDF (reply)\n\n"
        
        "**QR Code**\n"
        "`qr [text]` - Generate QR code\n"
        "`qr [text] [size]` - Custom size\n"
        "`qradv [text] [fg] [bg]` - Custom colors\n"
        "`qrread` - Read QR (reply to image)\n\n"
        
        "**Files**\n"
        "`zipfile [pwd]` - Zip file (reply)\n"
        "`unzip [pwd]` - Extract ZIP (reply)\n"
        "`add` - Add to zip queue (reply)\n"
        "`zipit [pwd]` - Zip queued files\n"
        "`rename [name]` - Rename (reply)\n"
        "`metadata [title - artist]` - Edit tags (reply)\n"
        "`split [start-end]` - Split PDF (reply)\n\n"
        
        "**AI**\n"
        "`gpt [text]` - ChatGPT\n"
        "`gpts [text]` - GPT + web search\n"
        "`gptr [text]` - GPT reasoning\n"
        "`imagine [prompt]` - Generate image\n\n"
        
        "**Admin** (sudo only)\n"
        "`setadmin [id]` - Add admin\n"
        "`remadmin [id]` - Remove admin\n"
        "`adminlist` - List admins\n"
        "`backup` - Backup bot\n"
        "`setreact @channel [emoji]` - Auto-react\n"
        "`remreact @channel` - Remove react\n"
        "`reactlist` - List reactions\n"
        
        "**Dictionary**\n"
        "`dic [word]` - Get definition\n"
        "Shows: English & Persian meanings, examples, pronunciation\n\n"
        
    )

    await event.reply(help_text)

def backup_database(event, args, session):
    """Create database backup as SQL dump."""
    now = datetime.now()
    backup_file = os.path.join(os.path.dirname(__file__), f'database_backup_{now.strftime("%Y%m%d_%H%M%S")}.sql')

    db_user = os.getenv('DB_USER', 'selfnit4_alireza')
    db_password = os.getenv('DB_PASSWORD', '9510290042AlirezA')
    db_host = os.getenv('DB_HOST', 'localhost')
    db_name = os.getenv('DB_NAME', 'selfnit4_selfbot')

    try:
        mysqldump_path = '/usr/bin/mysqldump'
        command = [
            mysqldump_path,
            '--user=' + db_user,
            '--password=' + db_password,
            '--host=' + db_host,
            db_name
        ]

        with open(backup_file, 'w') as output_file:
            subprocess.run(command, stdout=output_file, check=True)

        logger.info(f"Database backup created: {backup_file}")
        return backup_file
        
    except subprocess.CalledProcessError as e:
        logger.error(f"Error backing up database: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return None


@sudo_only
async def handle_db_import(event):
    """Import database backup file."""
    if not event.is_reply:
        return await event.reply("Please reply to the database backup file to import it.")

    replied_msg = await event.get_reply_message()
    if not replied_msg.file:
        return await event.reply("The replied message must be a file (SQL backup file).")

    if not replied_msg.file.name.endswith(".sql"):
        return await event.reply("The backup file must have a .sql extension.")

    temp_file_path = os.path.join(os.path.dirname(__file__), f"temp_backup_{replied_msg.file.name}")
    await event.client.download_media(replied_msg, temp_file_path)

    db_user = os.getenv('DB_USER', 'selfnit4_alireza')
    db_password = os.getenv('DB_PASSWORD', '9510290042AlirezA')
    db_host = os.getenv('DB_HOST', 'localhost')
    db_name = os.getenv('DB_NAME', 'selfnit4_selfbot')

    try:
        mysql_command = [
            "mysql",
            "--user=" + db_user,
            "--password=" + db_password,
            "--host=" + db_host,
            db_name,
            "--default-character-set=utf8mb4",
            "--max_allowed_packet=64M",
            "-e",
            f"source {temp_file_path}"
        ]
        
        subprocess.run(mysql_command, check=True)
        await event.reply("Database successfully updated from the backup file.")
        logger.info(f"Database imported from {temp_file_path}")

    except subprocess.CalledProcessError as e:
        await event.reply(f"Failed to import the database backup. Error: {str(e)}")
        logger.error(f"Error importing database: {str(e)}")

    finally:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)


async def send_source_code(event, args, session):
    """Send the source code file."""
    source_code_file = os.path.join(os.path.dirname(__file__), 'self.py')

    if os.path.exists(source_code_file):
        await event.respond(file=source_code_file)
        logger.info("Source code sent successfully")
    else:
        await event.respond("Source code file not found")
        logger.error("Source code file not found")


@sudo_only
async def handle_backup(event):
    """Backup source code and database."""
    # Send source code
    await send_source_code(event, None, None)

    # Backup and send database
    backup_file = backup_database(event, None, None)
    if backup_file:
        await client.send_file(event.chat_id, backup_file)
        os.remove(backup_file)
    else:
        await event.reply("Failed to create a database backup.")
        

async def get_daily_weather(city_name, lat, lon):
    """Fetch and format daily weather forecast."""
    url = "https://ai-weather-by-meteosource.p.rapidapi.com/daily"
    
    querystring = {"lat": str(lat), "lon": str(lon), "language": "en", "units": "metric"}
    headers = {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": "ai-weather-by-meteosource.p.rapidapi.com"
    }

    try:
        response = requests.get(url, headers=headers, params=querystring, timeout=10)
        response.raise_for_status()
        data = response.json()

        if 'daily' not in data or 'data' not in data['daily']:
            raise ValueError("Invalid response structure")

        google_maps_link = f"[{city_name}](https://www.google.com/maps?q={lat},{lon})"
        forecast = f"⏰ **Daily Weather Forecast for {google_maps_link}** ⏰\n\n"

        for daily in data['daily']['data']:
            day = daily.get('day', 'N/A')
            min_temp = daily.get('temperature_min', 'N/A')
            max_temp = daily.get('temperature_max', 'N/A')
            condition = daily.get('weather', '')
            summary = daily.get('summary', '')
            rain_chance = daily.get('probability', {}).get('precipitation', 0)

            weather_emoji = get_weather_emoji(condition)

            forecast += (
                f"📅 **{day}**\n"
                f"🌡 🔻 {min_temp}°C      🔺 {max_temp}°C\n"
                f"{weather_emoji} {summary}\n"
                f"💧 {rain_chance}%\n"
                "---------------------------------------------------\n"
            )

        return forecast

    except requests.exceptions.RequestException as req_err:
        logger.error(f"HTTP request error: {req_err}")
        return f"⚠️ Error fetching daily weather forecast for {city_name}."
    except ValueError as val_err:
        logger.error(f"Response structure error: {val_err}")
        return f"⚠️ Error parsing weather data for {city_name}."
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return "⚠️ An unexpected error occurred."


def get_hourly_weather(city_name, lat, lon):
    """Fetch and format hourly weather forecast."""
    url = "https://weather-data-api1.p.rapidapi.com/check-forecast"
    
    querystring = {"lat": str(lat), "lon": str(lon)}
    headers = {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": "weather-data-api1.p.rapidapi.com"
    }

    try:
        response = requests.get(url, headers=headers, params=querystring, timeout=10)
        response.raise_for_status()
        data = response.json()

        google_maps_link = f"[{city_name}](https://www.google.com/maps?q={lat},{lon})"
        forecast = f"⏰ **Hourly Weather Forecast for {google_maps_link}** ⏰\n\n"

        if 'list' not in data:
            return f"⚠️ No data available for {city_name}."

        current_time = datetime.now(pytz.timezone('Asia/Tehran'))

        for hourly in data['list']:
            dt_utc = hourly['dt']
            forecast_time = datetime.fromtimestamp(dt_utc, pytz.utc).astimezone(pytz.timezone('Asia/Tehran'))
            
            if forecast_time < current_time:
                continue

            iran_time_str = forecast_time.strftime("%I:%M %p")
            iran_date_str = forecast_time.strftime("%m/%d")
            
            temp = round(hourly['main'].get('temp') - 273.15, 1)
            condition = hourly['weather'][0].get('main', '')
            precipitation = round(hourly.get('pop', 0) * 100)
            weather_emoji = get_weather_emoji(condition)

            forecast += (
                f"🕒 {iran_time_str} ({iran_date_str}) - 🌡 {temp}°C - {weather_emoji} - 💧 {precipitation}%\n"
                "------------------------------------\n"
            )

        return forecast

    except Exception as e:
        logger.error(f"Error fetching hourly weather: {e}")
        return f"⚠️ No data available for {city_name}."


def get_lat_lon(city_name):
    """Get latitude and longitude for a city name."""
    try:
        location = geolocator.geocode(city_name)
        if location:
            return location.latitude, location.longitude
    except Exception as e:
        logger.error(f"Geocoding error for {city_name}: {e}")
    
    return None, None


async def handle_daily_weather(event, *args):
    """Handle daily weather forecast command."""
    if not args:
        return await event.reply("❌ Please provide a city name for the daily weather forecast.")

    city_name = ' '.join(args)
    logger.info(f"Daily weather request for: {city_name}")

    loading_msg = await event.reply("⏳ Getting forecast...")

    try:
        lat, lon = await asyncio.get_event_loop().run_in_executor(None, get_lat_lon, city_name)
        
        if lat is None or lon is None:
            return await loading_msg.edit(f"⚠️ Could not find location for {city_name}.")

        forecast = await get_daily_weather(city_name, lat, lon)
        await loading_msg.delete()
        await send_long_message(event, forecast)

    except Exception as e:
        logger.error(f"Error in handle_daily_weather: {str(e)}")
        await loading_msg.edit("⚠️ Error fetching daily weather.")


async def handle_hourly_weather(event, *args):
    """Handle hourly weather forecast command."""
    if not args:
        return await event.reply("❌ Please provide a city name for the hourly weather forecast.")

    city_name = ' '.join(args)
    logger.info(f"Hourly weather request for: {city_name}")

    loading_msg = await event.reply("⏳ Getting forecast...")

    try:
        lat, lon = await asyncio.get_event_loop().run_in_executor(None, get_lat_lon, city_name)
        
        if lat is None or lon is None:
            return await loading_msg.edit(f"⚠️ Could not find location for {city_name}.")

        forecast = await asyncio.get_event_loop().run_in_executor(
            None, get_hourly_weather, city_name, lat, lon
        )
        
        await loading_msg.delete()
        await send_long_message(event, forecast)

    except Exception as e:
        logger.error(f"Error in handle_hourly_weather: {str(e)}")
        await loading_msg.edit("⚠️ Error fetching hourly weather.")


async def send_long_message(event, text, max_length=4096):
    """Send a message, splitting it if it exceeds max length."""
    if len(text) <= max_length:
        await event.reply(text)
    else:
        for i in range(0, len(text), max_length):
            await event.reply(text[i:i + max_length])


def get_weather_emoji(description):
    """Returns an emoji based on weather condition description."""
    condition = description.lower()
    
    weather_emojis = {
        "sunny": "☀️", "clear": "☀️", "mostly sunny": "🌤️",
        "partly sunny": "⛅", "hazy sunshine": "🌫️",
        "intermittent clouds": "⛅", "clear night": "🌙",
        "cloudy": "☁️", "partly cloudy": "🌥️", "mostly cloudy": "🌥️",
        "overcast": "☁️", "dark clouds": "☁️🌫️",
        "rain": "🌧️", "light rain": "🌦️", "moderate rain": "🌧️",
        "heavy rain": "🌧️", "drizzle": "🌦️", "patchy rain": "🌦️",
        "intermittent rain": "🌦️", "scattered showers": "🌦️",
        "isolated showers": "🌦️", "showers": "🌦️",
        "light rain showers": "🌦️", "heavy rain showers": "🌧️",
        "freezing rain": "🧊", "cold rain": "🌧️❄️", "warm rain": "🌧️🔥",
        "thunderstorm": "⛈️", "isolated thunderstorms": "⛈️",
        "scattered thunderstorms": "⛈️", "severe thunderstorm": "⛈️⚡",
        "thunder": "⛈️", "lightning": "⚡", "thunder and lightning": "⛈️⚡",
        "storm": "⛈️", "violent storm": "🌪️", "supercell": "🌪️",
        "derecho": "🌪️", "tropical storm": "🌀", "hurricane": "🌀",
        "cyclone": "🌀", "typhoon": "🌀",
        "snow": "❄️", "light snow": "🌨️", "moderate snow": "🌨️",
        "heavy snow": "❄️", "snow showers": "🌨️", "sleet": "🌨️",
        "hail": "🌨️", "ice": "🧊", "icy roads": "🧊🚧",
        "frost": "❄️", "blizzard": "🌨️❄️", "wintry mix": "🌨️❄️",
        "patchy snow": "🌨️", "freezing drizzle": "🧊", "cold front": "❄️",
        "windy": "🌬️", "breezy": "🌬️", "gusty winds": "🌬️",
        "strong winds": "🌪️", "tornado": "🌪️", "sandstorm": "🌪️",
        "blowing sand": "🌪️", "dust storm": "🌪️", "haboob": "🌪️",
        "hot and windy": "🔥🌬️", "chilly breeze": "❄️🌬️",
        "fog": "🌫️", "dense fog": "🌫️", "patchy fog": "🌫️",
        "mist": "🌫️", "smoke": "🌫️", "haze": "🌫️", "blowing dust": "🌪️",
        "hot": "🔥", "very hot": "🔥🔥", "scorching": "🔥🔥🔥",
        "cold": "❄️", "freezing": "🧊", "frosty": "❄️",
        "chilly": "🌬️❄️", "warm front": "🔥",
        "heat wave": "🔥🔥", "cold wave": "❄️❄️",
        "rough seas": "🌊", "calm seas": "🌊",
        "storm surge": "🌊🌪️", "tsunami": "🌊🌊🌊"
    }
    
    for key, emoji in weather_emojis.items():
        if key in condition:
            return emoji
    
    return "🌡️"
    
    

async def handle_currency(event):
    """Fetch and display current prices for currency, gold, and coins."""
    url = "https://www.tgju.org/"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                response.raise_for_status()
                html_content = await response.text()

        soup = BeautifulSoup(html_content, 'html.parser')

        def get_price_by_slug(slug):
            """Extract price from data-market-nameslug attribute."""
            try:
                element = soup.find('tr', {'data-market-nameslug': slug})
                if element and element.get('data-price'):
                    return element.get('data-price')
                return None
            except Exception as e:
                logger.error(f"Error extracting price for {slug}: {e}")
                return None

        def convert_to_toman(price_str):
            """Convert IRR price to Toman."""
            if not price_str:
                return None
            try:
                price_irr = int(price_str.replace(',', ''))
                price_toman = price_irr // 10
                return f"{price_toman:,}"
            except (ValueError, AttributeError) as e:
                logger.error(f"Error converting price {price_str}: {e}")
                return None

        def format_item(name, price, symbol=None, unit="Toman"):
            """Format a single item."""
            if not price:
                return f"**{name}:** No data available\n"
            
            symbol_text = f" ({symbol})" if symbol else ""
            return f"**{name}{symbol_text}:** {price} {unit}\n"

        # Currency mapping
        currency_items = {
            "USD": ("price_dollar_rl", "USD"),
            "EUR": ("price_eur", "EUR"),
            "GBP": ("price_gbp", "GBP"),
            "AUD": ("price_aud", "AUD"),
            "CAD": ("price_cad", "CAD")
        }

        # Gold mapping
        gold_items = {
            "18K Gold": "geram18",
            "24K Gold": "geram24"
        }

        # Coin mapping
        coin_items = {
            "Bahar Azadi Coin": "sekeb",
            "Half Coin": "nim",
            "Quarter Coin": "rob"
        }

        # Build message
        message_parts = ["💰 **Currency Prices**\n"]

        # Add currencies
        for name, (slug, symbol) in currency_items.items():
            price_irr = get_price_by_slug(slug)
            price_toman = convert_to_toman(price_irr)
            message_parts.append(format_item(name, price_toman, symbol))

        # Add gold
        message_parts.append("\n🥇 **Gold Prices**\n")
        for name, slug in gold_items.items():
            price_irr = get_price_by_slug(slug)
            price_toman = convert_to_toman(price_irr)
            message_parts.append(format_item(name, price_toman))

        # Add coins
        message_parts.append("\n🪙 **Coin Prices**\n")
        for name, slug in coin_items.items():
            price_irr = get_price_by_slug(slug)
            price_toman = convert_to_toman(price_irr)
            message_parts.append(format_item(name, price_toman))

        await event.reply("".join(message_parts))

    except aiohttp.ClientTimeout:
        await event.reply("❌ Request timeout. Please try again.")
    except aiohttp.ClientError as e:
        logger.error(f"Network error in handle_currency: {e}")
        await event.reply("❌ Network error. Please try again.")
    except Exception as e:
        logger.error(f"Unexpected error in handle_currency: {e}")
        await event.reply(f"❌ Unexpected error: {str(e)}")
        
        
class ProgressTracker:
    """Track download/upload progress with smart update intervals."""
    
    def __init__(self):
        self.last_percent = -1
        self.last_message = None
        self.last_update_time = 0
        self.start_time = time.time()
    
    def should_update(self, percent, min_interval=1.0, min_percent_change=10):
        """Determine if progress should be updated."""
        current_time = time.time()
        time_elapsed = current_time - self.last_update_time
        percent_change = percent - self.last_percent
        
        if percent_change >= min_percent_change or time_elapsed >= min_interval:
            self.last_update_time = current_time
            self.last_percent = percent
            return True
        return False
    
    def get_speed(self, downloaded_bytes):
        """Calculate speed in MB/s."""
        elapsed = time.time() - self.start_time
        if elapsed > 0:
            return downloaded_bytes / elapsed / (1024 * 1024)
        return 0
    
    def get_eta(self, current_bytes, total_bytes):
        """Calculate estimated time remaining."""
        elapsed = time.time() - self.start_time
        if current_bytes > 0 and elapsed > 0:
            speed = current_bytes / elapsed
            remaining_bytes = total_bytes - current_bytes
            eta_seconds = remaining_bytes / speed
            return self.format_time(eta_seconds)
        return "calculating..."
    
    @staticmethod
    def format_time(seconds):
        """Format seconds into human-readable time."""
        if seconds < 60:
            return f"{int(seconds)}s"
        elif seconds < 3600:
            return f"{int(seconds // 60)}m {int(seconds % 60)}s"
        else:
            hours = int(seconds // 3600)
            minutes = int((seconds % 3600) // 60)
            return f"{hours}h {minutes}m"
    
    @staticmethod
    def format_size(bytes_size):
        """Format bytes into human-readable size."""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if bytes_size < 1024.0:
                return f"{bytes_size:.1f} {unit}"
            bytes_size /= 1024.0
        return f"{bytes_size:.1f} TB"


async def download_media(url, file_path, progress_message):
    """Download media file with enhanced progress tracking."""
    tracker = ProgressTracker()
    
    try:
        await safe_edit_message(progress_message, "🔄 Preparing download...")
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        
        response = requests.get(url, stream=True, headers=headers, timeout=30)
        response.raise_for_status()
        
        total_size = int(response.headers.get('content-length', 0))
        downloaded_size = 0
        
        await safe_edit_message(progress_message, f"📦 File size: {tracker.format_size(total_size)}")
        await asyncio.sleep(0.5)
        
        with open(file_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded_size += len(chunk)
                    
                    if total_size > 0:
                        percent = math.floor((downloaded_size / total_size) * 100)
                        
                        if tracker.should_update(percent):
                            speed = tracker.get_speed(downloaded_size)
                            eta = tracker.get_eta(downloaded_size, total_size)
                            progress_bar = create_progress_bar(percent)
                            
                            status_msg = (
                                f"⬇️ **Downloading...**\n\n"
                                f"{progress_bar} {percent}%\n"
                                f"📊 {tracker.format_size(downloaded_size)} / {tracker.format_size(total_size)}\n"
                                f"⚡ Speed: {speed:.2f} MB/s\n"
                                f"⏱ ETA: {eta}"
                            )
                            
                            await safe_edit_message(progress_message, status_msg, tracker)
        
        elapsed = tracker.format_time(time.time() - tracker.start_time)
        avg_speed = tracker.get_speed(downloaded_size)
        
        complete_msg = (
            f"✅ **Download complete!**\n\n"
            f"📦 Size: {tracker.format_size(downloaded_size)}\n"
            f"⚡ Avg speed: {avg_speed:.2f} MB/s\n"
            f"⏱ Total time: {elapsed}"
        )
        
        await safe_edit_message(progress_message, complete_msg)
        logger.info(f"Downloaded {file_path}, size: {tracker.format_size(downloaded_size)}")
        
    except requests.exceptions.Timeout:
        await safe_edit_message(progress_message, "❌ Download timeout. Please try again.")
        raise
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error downloading media: {str(e)}")
        await safe_edit_message(progress_message, f"❌ Network error: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"Error downloading media: {str(e)}")
        await safe_edit_message(progress_message, "❌ Failed to download the file.")
        raise


async def update_upload_progress(current, total, progress_message):
    """Enhanced upload progress with detailed statistics."""
    if not hasattr(update_upload_progress, "tracker"):
        update_upload_progress.tracker = ProgressTracker()
    
    tracker = update_upload_progress.tracker
    percent = math.floor((current / total) * 100)
    
    if tracker.should_update(percent, min_interval=1.5, min_percent_change=5):
        speed = tracker.get_speed(current)
        eta = tracker.get_eta(current, total)
        progress_bar = create_progress_bar(percent)
        
        status_msg = (
            f"⬆️ **Uploading...**\n\n"
            f"{progress_bar} {percent}%\n"
            f"📊 {tracker.format_size(current)} / {tracker.format_size(total)}\n"
            f"⚡ Speed: {speed:.2f} MB/s\n"
            f"⏱ ETA: {eta}"
        )
        
        await safe_edit_message(progress_message, status_msg, tracker)
    
    if current >= total:
        elapsed = tracker.format_time(time.time() - tracker.start_time)
        avg_speed = tracker.get_speed(current)
        
        complete_msg = (
            f"✅ **Upload complete!**\n\n"
            f"📦 Size: {tracker.format_size(total)}\n"
            f"⚡ Avg speed: {avg_speed:.2f} MB/s\n"
            f"⏱ Total time: {elapsed}"
        )
        
        await safe_edit_message(progress_message, complete_msg)
        delattr(update_upload_progress, "tracker")


def create_progress_bar(percent, length=10):
    """Create a visual progress bar."""
    filled = int(length * percent / 100)
    empty = length - filled
    return f"[{'█' * filled}{'░' * empty}]"


async def safe_edit_message(message, text, tracker=None):
    """Safely edit a message, handling MessageNotModifiedError."""
    if tracker and tracker.last_message == text:
        return
    
    try:
        await message.edit(text)
        if tracker:
            tracker.last_message = text
    except MessageNotModifiedError:
        pass
    except Exception as e:
        logger.warning(f"Failed to edit message: {e}")
        
        
async def handle_tts(event):
    """Convert replied text to speech."""
    progress_msg = None
    filename = None
    
    if not event.is_reply:
        return await event.reply("❌ Please reply to a message")

    try:
        replied_msg = await event.get_reply_message()
        text_to_convert = replied_msg.raw_text
        
        if not text_to_convert or not text_to_convert.strip():
            return await event.reply("❌ Message is empty")
        
        max_length = 5000
        if len(text_to_convert) > max_length:
            return await event.reply(f"❌ Text too long (max {max_length} characters)")
        
        progress_msg = await event.reply("🎙️ Generating speech...")
        
        # API configuration
        url = "https://joj-text-to-speech.p.rapidapi.com/"
        payload = {
            "input": {"text": text_to_convert},
            "voice": {
                "languageCode": "en-US",
                "name": "en-US-Journey-F",
                "ssmlGender": "FEMALE"
            },
            "audioConfig": {
                "audioEncoding": "MP3",
                "pitch": 0,
                "speakingRate": 1.0
            }
        }
        
        headers = {
            "x-rapidapi-key": RAPIDAPI_KEY,
            "x-rapidapi-host": "joj-text-to-speech.p.rapidapi.com",
            "Content-Type": "application/json"
        }
        
        response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: requests.post(url, json=payload, headers=headers, timeout=30)
        )
        response.raise_for_status()
        
        response_data = response.json()
        audio_data = response_data.get('audioContent')
        
        if not audio_data:
            raise ValueError("No audio content received from API")
        
        await safe_edit_message(progress_msg, "🎵 Processing audio...")
        
        decoded_audio = base64.b64decode(audio_data)
        filename = f"tts_{event.id}_{int(time.time())}.mp3"
        
        with open(filename, "wb") as f:
            f.write(decoded_audio)
        
        file_size = os.path.getsize(filename)
        if file_size == 0:
            raise ValueError("Generated audio file is empty")
        
        logger.info(f"Generated TTS audio: {filename} ({file_size} bytes)")
        
        await safe_edit_message(progress_msg, "⬆️ Uploading...")
        
        await event.client.send_file(
            event.chat_id,
            filename,
            voice_note=True,
            reply_to=event.reply_to_msg_id,
            attributes=[
                types.DocumentAttributeAudio(
                    duration=0,
                    voice=True,
                    title="Text to Speech",
                    performer="TTS Bot"
                )
            ]
        )
        
        await progress_msg.delete()
    
    except requests.exceptions.Timeout:
        await handle_tts_error(event, progress_msg, "❌ Request timeout. Please try again.")
    except requests.exceptions.RequestException as e:
        logger.error(f"TTS API error: {str(e)}")
        await handle_tts_error(event, progress_msg, f"❌ API connection error: {str(e)}")
    except ValueError as e:
        logger.error(f"TTS value error: {str(e)}")
        await handle_tts_error(event, progress_msg, f"❌ Processing error: {str(e)}")
    except Exception as e:
        logger.exception(f"Unexpected TTS error: {str(e)}")
        await handle_tts_error(event, progress_msg, f"❌ Unexpected error: {str(e)}")
    finally:
        if filename and os.path.exists(filename):
            try:
                os.remove(filename)
            except Exception as e:
                logger.warning(f"Failed to delete temp file {filename}: {e}")


async def handle_tts_error(event, progress_msg, error_message):
    """Handle TTS errors."""
    try:
        if progress_msg:
            await progress_msg.delete()
    except Exception:
        pass
    
    try:
        await event.reply(error_message)
    except Exception as e:
        logger.error(f"Failed to send error reply: {e}")
        

async def handle_zip_command(event, *args):
    """Zip a replied file with optional password protection."""
    
    # Extract password if provided
    zip_password = args[0] if args else None

    if not event.is_reply:
        return await event.reply("❌ Please reply to a message to zip it.")

    reply_message = await event.get_reply_message()
    progress_message = await event.reply("🔄 Zipping...")

    file_path = None
    file_name = None
    zip_file_path = None

    try:
        # Determine file type and download/create
        if reply_message.text:
            file_name = "message.txt"
            file_path = os.path.join(os.getcwd(), f"temp_{event.id}_{file_name}")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(reply_message.text)
        
        elif reply_message.media:
            await safe_edit_message(progress_message, "⬇️ Downloading...")
            
            file_path = await reply_message.download_media()
            
            if reply_message.document:
                file_name = reply_message.file.name or f"file_{event.id}"
            elif reply_message.photo:
                file_name = f"photo_{event.id}.jpg"
            elif reply_message.video:
                file_name = f"video_{event.id}.mp4"
            elif reply_message.audio:
                file_name = f"audio_{event.id}.mp3"
            elif reply_message.voice:
                file_name = f"voice_{event.id}.ogg"
            elif reply_message.video_note:
                file_name = f"video_note_{event.id}.mp4"
            else:
                file_name = f"media_{event.id}"
        
        else:
            return await safe_edit_message(progress_message, "⚠️ This type of message is not supported.")

        if not file_path or not os.path.exists(file_path):
            raise ValueError("File download failed")

        await safe_edit_message(progress_message, "📦 Creating ZIP file...")

        zip_file_path = f"{file_path}.zip"
        
        if zip_password:
            with pyzipper.AESZipFile(zip_file_path, 'w', compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES) as zipf:
                zipf.setpassword(zip_password.encode('utf-8'))
                zipf.write(file_path, file_name)
        else:
            with pyzipper.AESZipFile(zip_file_path, 'w', compression=pyzipper.ZIP_DEFLATED) as zipf:
                zipf.write(file_path, file_name)

        if not os.path.exists(zip_file_path) or os.path.getsize(zip_file_path) == 0:
            raise ValueError("ZIP file creation failed")

        password_info = " 🔒 (password protected)" if zip_password else ""
        await safe_edit_message(progress_message, f"⬆️ Uploading{password_info}...")
        
        await event.client.send_file(
            event.chat_id,
            zip_file_path,
            caption=f"🔒 Password: `{zip_password}`" if zip_password else None
        )

        await progress_message.delete()
        logger.info(f"Zipped file sent: {file_name} (password: {bool(zip_password)})")

    except Exception as e:
        logger.error(f"Error in handle_zip_command: {str(e)}")
        await safe_edit_message(progress_message, f"❌ Error: {str(e)}")
    
    finally:
        for temp_file in [file_path, zip_file_path]:
            if temp_file and os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except Exception as e:
                    logger.warning(f"Failed to delete temp file {temp_file}: {e}")


async def handle_unzip_command(event, *args):
    """Extract a ZIP file with optional password protection."""
    
    zip_password = args[0] if args else None

    if not event.is_reply:
        return await event.reply("❌ Please reply to a ZIP file.")

    reply_message = await event.get_reply_message()

    if not reply_message.document or not reply_message.file.name.endswith(".zip"):
        return await event.reply("❌ Please reply only to ZIP files.")

    progress_message = await event.reply("⬇️ Downloading...")

    zip_path = None
    extract_folder = None

    try:
        zip_path = await reply_message.download_media()
        
        if not zip_path or not os.path.exists(zip_path):
            raise ValueError("Failed to download ZIP file")

        await safe_edit_message(progress_message, "🔍 Checking file...")

        # Check if password protected
        password_protected = False
        try:
            with pyzipper.AESZipFile(zip_path, 'r') as zipf:
                zipf.testzip()
        except RuntimeError:
            password_protected = True

        if password_protected and not zip_password:
            return await safe_edit_message(
                progress_message,
                "🔒 This ZIP is password protected. Send command with password:\n`unzip [password]`"
            )

        # Extract files
        extract_folder = zip_path.replace(".zip", "_extracted")
        os.makedirs(extract_folder, exist_ok=True)

        with pyzipper.AESZipFile(zip_path, 'r') as zipf:
            if zip_password:
                zipf.setpassword(zip_password.encode('utf-8'))
            
            zipf.extractall(extract_folder)
            extracted_files = [f for f in zipf.namelist() if not f.endswith('/')]

        total_files = len(extracted_files)
        
        if total_files == 0:
            return await safe_edit_message(progress_message, "⚠️ ZIP file is empty.")

        # Send extracted files
        sent_files = 0
        for file_name in extracted_files:
            extracted_file_path = os.path.join(extract_folder, file_name)
            
            if not os.path.isfile(extracted_file_path):
                continue
            
            sent_files += 1
            await safe_edit_message(progress_message, f"⬆️ Uploading {sent_files}/{total_files}...")

            await event.client.send_file(event.chat_id, extracted_file_path)

        await progress_message.delete()
        logger.info(f"Extracted and sent {sent_files} files from ZIP")

    except Exception as e:
        logger.error(f"Error in handle_unzip_command: {str(e)}")
        await safe_edit_message(progress_message, f"❌ Error: {str(e)}")
    
    finally:
        if zip_path and os.path.exists(zip_path):
            try:
                os.remove(zip_path)
            except Exception as e:
                logger.warning(f"Failed to delete ZIP file: {e}")
        
        if extract_folder and os.path.exists(extract_folder):
            try:
                shutil.rmtree(extract_folder)
            except Exception as e:
                logger.warning(f"Failed to delete extraction folder: {e}")


async def handle_ziplist_command(event):
    """Add a file to the user's ZIP waitlist."""
    
    user_id = event.sender_id
    
    if not event.is_reply:
        return await event.reply("❌ Please reply to a file.")

    reply_message = await event.get_reply_message()

    if not reply_message.document:
        return await event.reply("❌ Please reply to a file.")

    if user_id not in user_waitlists:
        user_waitlists[user_id] = []

    user_waitlists[user_id].append(reply_message)
    
    count = len(user_waitlists[user_id])
    file_name = reply_message.file.name or "Unnamed file"
    
    await event.reply(f"✅ File {count} added: `{file_name}`\n📦 Total: {count} files")


async def handle_zipfolder_command(event, *args):
    """Zip all files in the user's waitlist with optional password protection."""
    
    user_id = event.sender_id
    zip_password = args[0] if args else None

    if user_id not in user_waitlists or len(user_waitlists[user_id]) == 0:
        return await event.reply("❌ List is empty. Use `add` to add files first.")

    total_files = len(user_waitlists[user_id])
    progress_message = await event.reply(f"🔄 Zipping {total_files} files...")

    zip_file_path = f"waitlist_{user_id}_{int(time.time())}.zip"
    downloaded_files = []

    try:
        # Download all files
        for idx, file_message in enumerate(user_waitlists[user_id], 1):
            await safe_edit_message(progress_message, f"⬇️ Downloading {idx}/{total_files}...")
            
            file_path = await file_message.download_media()
            if file_path:
                downloaded_files.append(file_path)

        # Create ZIP
        await safe_edit_message(progress_message, "📦 Creating ZIP file...")
        
        if zip_password:
            with pyzipper.AESZipFile(zip_file_path, 'w', compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES) as zipf:
                zipf.setpassword(zip_password.encode('utf-8'))
                for file_path in downloaded_files:
                    zipf.write(file_path, os.path.basename(file_path))
        else:
            with zipfile.ZipFile(zip_file_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for file_path in downloaded_files:
                    zipf.write(file_path, os.path.basename(file_path))

        zip_size = os.path.getsize(zip_file_path)
        if zip_size == 0:
            raise ValueError("ZIP file creation failed")

        password_info = f" 🔒\nPassword: `{zip_password}`" if zip_password else ""
        await safe_edit_message(progress_message, "⬆️ Uploading...")
        
        await event.client.send_file(
            event.chat_id,
            zip_file_path,
            caption=f"📦 {total_files} files zipped{password_info}"
        )

        # Clear waitlist
        user_waitlists[user_id] = []
        
        await progress_message.delete()
        logger.info(f"Zipped and sent {total_files} files for user {user_id}")

    except Exception as e:
        logger.error(f"Error in handle_zipfolder_command: {str(e)}")
        await safe_edit_message(progress_message, f"❌ Error: {str(e)}")
    
    finally:
        for file_path in downloaded_files:
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception as e:
                    logger.warning(f"Failed to delete temp file {file_path}: {e}")
        
        if os.path.exists(zip_file_path):
            try:
                os.remove(zip_file_path)
            except Exception as e:
                logger.warning(f"Failed to delete ZIP file: {e}")
                

async def handle_rename(event, *args):
    """Rename a replied file while preserving its extension."""
    
    if not event.is_reply:
        return await event.reply("❌ Please reply to a file to rename it.")

    reply_msg = await event.get_reply_message()

    if not reply_msg.file:
        return await event.reply("❌ This command only works with files.")

    if not args:
        return await event.reply("❌ Usage: `rename [new_name]`")

    # Extract new name (remove any extension user might have added)
    new_name = ' '.join(args).split('.')[0].strip()
    
    if not new_name:
        return await event.reply("❌ File name cannot be empty.")

    # Get original file extension
    original_file_name = reply_msg.file.name or "file"
    _, file_extension = os.path.splitext(original_file_name)
    
    if not file_extension:
        file_extension = ".bin"

    new_full_name = new_name + file_extension
    progress_msg = await event.reply("⬇️ Downloading...")

    file_path = None
    new_file_path = None

    try:
        file_path = await reply_msg.download_media()
        
        if not file_path or not os.path.exists(file_path):
            raise ValueError("File download failed")

        await safe_edit_message(progress_msg, "✏️ Renaming...")
        
        new_file_path = os.path.join(os.path.dirname(file_path), new_full_name)
        os.rename(file_path, new_file_path)
        
        if not os.path.exists(new_file_path):
            raise ValueError("File rename failed")

        file_size = os.path.getsize(new_file_path)
        await safe_edit_message(progress_msg, "⬆️ Uploading...")

        caption = f"✅ Renamed to: `{new_full_name}`"
        
        await event.client.send_file(
            event.chat_id,
            new_file_path,
            caption=caption,
            force_document=True
        )

        await progress_msg.delete()
        logger.info(f"File renamed: {original_file_name} → {new_full_name} ({file_size} bytes)")

    except Exception as e:
        logger.error(f"Error in handle_rename: {str(e)}")
        await safe_edit_message(progress_msg, f"❌ Error: {str(e)}")
    
    finally:
        for temp_file in [file_path, new_file_path]:
            if temp_file and os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except Exception as e:
                    logger.warning(f"Failed to delete temp file {temp_file}: {e}")


async def handle_metadata(event, *args):
    """Change metadata (title & artist) of a music file."""
    
    if not event.is_reply:
        return await event.reply("❌ Please reply to a music file.")

    reply_msg = await event.get_reply_message()

    if not reply_msg.audio and not reply_msg.voice:
        return await event.reply("❌ This command only works with audio files.")

    if not args or "-" not in ' '.join(args):
        return await event.reply("❌ Usage: `metadata song_name - artist_name`")

    # Parse input
    input_text = ' '.join(args)
    parts = input_text.split("-", 1)
    
    if len(parts) != 2:
        return await event.reply("❌ Correct format: song_name - artist_name")

    song_name = parts[0].strip()
    artist_name = parts[1].strip()

    if not song_name or not artist_name:
        return await event.reply("❌ Song name and artist cannot be empty.")

    progress_msg = await event.reply("⬇️ Downloading...")

    file_path = None
    new_file_path = None

    try:
        file_path = await reply_msg.download_media()
        
        if not file_path or not os.path.exists(file_path):
            raise ValueError("File download failed")

        await safe_edit_message(progress_msg, "🎵 Editing metadata...")

        file_ext = os.path.splitext(file_path)[1].lower()
        
        if file_ext == '.mp3':
            try:
                audio = MP3(file_path, ID3=EasyID3)
            except:
                audio = MP3(file_path)
                audio.add_tags()
                audio = MP3(file_path, ID3=EasyID3)
            
            audio["title"] = song_name
            audio["artist"] = artist_name
            audio.save()
            
        elif file_ext in ['.m4a', '.mp4']:
            audio = MP4(file_path)
            audio["\xa9nam"] = song_name
            audio["\xa9ART"] = artist_name
            audio.save()
            
        elif file_ext == '.flac':
            audio = FLAC(file_path)
            audio["title"] = song_name
            audio["artist"] = artist_name
            audio.save()
            
        elif file_ext == '.ogg':
            audio = OggVorbis(file_path)
            audio["title"] = song_name
            audio["artist"] = artist_name
            audio.save()
            
        else:
            return await safe_edit_message(
                progress_msg,
                f"⚠️ Format {file_ext} not supported. Only MP3, M4A, FLAC, OGG"
            )

        # Rename file
        new_file_path = os.path.join(os.path.dirname(file_path), f"{song_name}{file_ext}")
        
        if os.path.exists(new_file_path):
            new_file_path = os.path.join(
                os.path.dirname(file_path),
                f"{song_name}_{int(time.time())}{file_ext}"
            )
        
        os.rename(file_path, new_file_path)
        file_path = new_file_path

        await safe_edit_message(progress_msg, "⬆️ Uploading...")

        caption = (
            f"🎵 **Metadata Updated:**\n"
            f"**Title:** `{song_name}`\n"
            f"**Artist:** `{artist_name}`"
        )

        await event.client.send_file(
            event.chat_id,
            new_file_path,
            caption=caption,
            attributes=[
                types.DocumentAttributeAudio(
                    duration=0,
                    title=song_name,
                    performer=artist_name
                )
            ]
        )

        await progress_msg.delete()
        logger.info(f"Metadata updated: {song_name} - {artist_name}")

    except Exception as e:
        logger.error(f"Error in handle_metadata: {str(e)}")
        await safe_edit_message(progress_msg, f"❌ Error: {str(e)}")
    
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                logger.warning(f"Failed to delete temp file: {e}")
                

async def handle_gpt(event, *args, web_access=False, reasoning=False):
    """
    Handle GPT requests.
    - 'gpt'  -> Standard ChatGPT
    - 'gpts' -> ChatGPT with web access
    - 'gptr' -> GPT Reasoning Mode
    """
    if not args:
        return await event.reply("⚠️ Usage: gpt [request], gpts [request], or gptr [request]")

    user_request = ' '.join(args)
    progress_msg = await event.reply("🧠 Processing your request...")

    # Choose API endpoint
    api_url = GPT_REASONING_API_URL if reasoning else GPT_API_URL

    # Prepare API payload
    payload = {
        "messages": [{"role": "user", "content": user_request}],
        "web_access": web_access
    }

    try:
        response = requests.post(api_url, json=payload, headers=GPT_HEADERS, timeout=60)

        if response.status_code == 200:
            data = response.json()
            gpt_response = data.get("result", "⚠️ No response from AI.")
            status = data.get("status", False)

            if status:
                mode = "GPT Reasoning" if reasoning else "GPT Response"
                await progress_msg.edit(f"**🤖 {mode}:**\n{gpt_response}")
            else:
                await progress_msg.edit("⚠️ AI did not return a valid response.")
        else:
            await progress_msg.edit(f"❌ Error: {response.status_code} - {response.text}")

    except requests.exceptions.Timeout:
        await progress_msg.edit("❌ Request timeout. Please try again.")
    except Exception as e:
        logger.error(f"GPT error: {str(e)}")
        await progress_msg.edit(f"❌ Error: {str(e)}")


async def handle_imagine(event, *args):
    """Generate AI image from text prompt."""
    if not args:
        return await event.reply("⚠️ Usage: imagine [prompt]")

    prompt = ' '.join(args)
    progress_msg = await event.reply(f"🎨 Generating image for: {prompt}...")

    payload = {"text": prompt}

    try:
        response = requests.post(IMAGE_GEN_API_URL, json=payload, headers=IMAGE_HEADERS, timeout=60)

        if response.status_code == 200:
            data = response.json()
            image_url = data.get("generated_image")

            if image_url:
                await event.reply(f"🖼 **Generated Image:**\n{image_url}")
                await progress_msg.delete()
            else:
                await progress_msg.edit("⚠️ AI did not return a valid image.")
        else:
            await progress_msg.edit(f"❌ Error: {response.status_code} - {response.text}")

    except requests.exceptions.Timeout:
        await progress_msg.edit("❌ Request timeout. Please try again.")
    except Exception as e:
        logger.error(f"Image generation error: {str(e)}")
        await progress_msg.edit(f"❌ Error: {str(e)}")
        

def _format_book_caption(book):
    """Create a formatted caption for a single book."""
    title = book.get('title', 'Unknown Title')
    author = book.get('author', 'Unknown Author')
    year = book.get('year', 'N/A')
    genre = book.get('genre', 'N/A')
    size = book.get('size', 'N/A')
    md5 = book.get('md5', 'N/A')

    return (
        f"📖 **Title:** {title}\n"
        f"✍️ **Author:** {author}\n"
        f"📅 **Year:** {year}\n"
        f"🗂️ **Genre:** {genre}\n"
        f"💾 **Size:** {size}\n"
        f"📥 **Download:** `dl_{md5}`"
    )


def _format_other_books_list(books):
    """Format a list of books into a single text block."""
    header = "--- Other Results ---\n\n"
    choices = []
    
    for book in books:
        title = book.get('title', 'Unknown Title')
        author = book.get('author', 'Unknown Author')
        size = book.get('size', 'N/A')
        year = book.get('year', 'N/A')
        md5 = book.get('md5', 'N/A')
        
        choices.append(
            f"📖 **{title}** by **{author}**\n"
            f"💾 {size} | 📅 {year}\n"
            f"📥 `dl_{md5}`"
        )
    
    return header + "\n\n".join(choices)


async def handle_book_search(event, *args):
    """Search for books on Anna's Archive."""
    if not args:
        return await event.reply("⚠️ Please provide a book name to search.")
    
    book_query = ' '.join(args)
    logger.info(f"🔍 Book search query: '{book_query}' from user {event.sender_id}")

    progress_message = await event.reply("🔍 Searching for the book...")

    try:
        url = "https://annas-archive-api.p.rapidapi.com/search"
        
        headers = {
            "x-rapidapi-key": RAPIDAPI_KEY,
            "x-rapidapi-host": "annas-archive-api.p.rapidapi.com"
        }
        
        querystring = {
            "q": book_query,
            "skip": "0",
            "limit": "10",
            "ext": "pdf",
            "sort": "mostRelevant",
            "source": "libgenLi, libgenRs, zLibrary, internetArchive, uploads, nexusStc, duxiu, zLibraryChinese, magzDb, sciHub"
        }
        
        logger.debug(f"API request to: {url} with params: {querystring}")
        response = requests.get(url, headers=headers, params=querystring, timeout=20)
        logger.info(f"API response status: {response.status_code}")

        if response.status_code != 200:
            logger.error(f"API request failed: {response.status_code}. Response: {response.text}")
            return await progress_message.edit("❌ Failed to connect to the search service.")

        data = response.json()
        books = data.get('books', [])
        logger.info(f"Found {len(books)} books for query '{book_query}'")

        if not books:
            logger.info(f"No books found for query: '{book_query}'")
            return await progress_message.edit("❌ No books found.")

        # Send first result as media (image + caption)
        first_book = books[0]
        img_url = first_book.get('imgUrl')
        caption = _format_book_caption(first_book)

        if img_url:
            try:
                await event.client.send_file(
                    entity=event.chat_id,
                    file=img_url,
                    caption=caption,
                    link_preview=False
                )
                logger.info(f"Sent first book result as media for query: '{book_query}'")
            except Exception as e:
                logger.warning(f"Failed to send image, sending as text. Error: {e}")
                await event.reply(caption)
        else:
            await event.reply(caption)

        # Send remaining results as text
        if len(books) > 1:
            other_books_text = _format_other_books_list(books[1:])
            await event.reply(other_books_text)
            logger.info(f"Sent {len(books) - 1} other book results as text")

        await progress_message.delete()

    except requests.exceptions.RequestException as e:
        logger.error(f"Network error during book search for '{book_query}': {e}")
        await progress_message.edit("❌ Network error. Please try again later.")
    except ValueError as e:
        logger.error(f"Failed to decode JSON response for query '{book_query}'. Error: {e}")
        await progress_message.edit("❌ Received an invalid response from the search service.")
    except Exception as e:
        logger.exception(f"Unexpected error during book search for '{book_query}': {e}")
        await progress_message.edit("❌ Error searching for the book.")


async def handle_book_download_by_md5(event, book_id):
    """Download book using its MD5 hash."""
    try:
        progress_message = await event.reply("⬇️ Downloading... (0%)")

        # API request
        main_url = "https://annas-archive-api.p.rapidapi.com/download"
        main_querystring = {"md5": book_id}
        main_headers = {
            "x-rapidapi-key": RAPIDAPI_KEY,
            "x-rapidapi-host": "annas-archive-api.p.rapidapi.com"
        }

        response = requests.get(main_url, headers=main_headers, params=main_querystring, timeout=20)
        data = response.json()

        if response.status_code == 200 and data:
            # Get download URL
            download_url = data[1] if len(data) > 1 else data[0] if data else None

            if not download_url or not download_url.startswith("http"):
                raise ValueError("Invalid download URL")

            media_name = f"book_{book_id}.pdf"

            # Download
            await download_media(download_url, media_name, progress_message)

            # Send file
            await event.client.send_file(
                event.chat_id,
                media_name,
                progress_callback=lambda current, total: update_upload_progress(
                    current, total, progress_message
                ),
            )

            # Cleanup
            if os.path.exists(media_name):
                os.remove(media_name)

            await progress_message.delete()

        else:
            await progress_message.edit("❌ Failed to get valid response.")

    except Exception as e:
        logger.error(f"Download failed: {str(e)}")
        await progress_message.edit(f"❌ Download failed. {str(e)}")


async def handle_article_search(event, *args):
    """Search for academic articles."""
    if not args:
        return await event.reply("⚠️ Please provide a search query.")
    
    article_query = ' '.join(args)
    progress_message = await event.reply("🔍 Searching for articles...")

    try:
        url = "https://annas-archive-api.p.rapidapi.com/search/journal"
        
        headers = {
            'x-rapidapi-key': RAPIDAPI_KEY,
            'x-rapidapi-host': "annas-archive-api.p.rapidapi.com"
        }
        
        querystring = {
            "q": article_query,
            "skip": "0",
            "limit": "10",
            "ext": "pdf",
            "sort": "mostRelevant",
            "source": "libgenLi, libgenRs, zLibrary, internetArchive, uploads, nexusStc, duxiu, zLibraryChinese, magzDb, sciHub"
        }

        response = requests.get(url, headers=headers, params=querystring, timeout=20)
        data = response.json()

        if response.status_code == 200 and data.get("total", 0) > 0:
            books = data.get('books', [])
            choices = [
                f"📖 **{book.get('title', 'Unknown')}** by **{book.get('author', 'Unknown Author')}**\n`dl_{book.get('md5', 'N/A')}`"
                for book in books
            ]
            reply_text = "✨ **Which article do you want to download?** ✨\n\n"
            await event.reply(reply_text + "\n\n".join(choices))
        else:
            await progress_message.edit("❌ No articles found.")
            
    except Exception as e:
        logger.error(f"Failed to search for articles: {str(e)}")
        await progress_message.edit("❌ Error while searching.")
        
        
async def handle_split(event, *args):
    """Split PDF pages by range (e.g., split 2-5)."""
    
    if not args:
        return await event.reply("❌ Usage: split start_page-end_page (e.g., 'split 2-5')")

    page_range_str = args[0]

    # Validate format
    match = re.match(r'^(\d+)-(\d+)$', page_range_str)
    if not match:
        return await event.reply(f"❌ Invalid page range format: {page_range_str}. Use 'start_page-end_page' (e.g., '2-5').")

    start, end = map(int, match.groups())
    if start < 1 or start > end:
        return await event.reply("❌ Invalid page range. Ensure start_page <= end_page and both >= 1.")

    if not event.is_reply:
        return await event.reply("❌ Please reply to a PDF file to split it.")

    reply = await event.get_reply_message()
    if not reply.document or reply.document.mime_type != 'application/pdf':
        return await event.reply("❌ Please reply to a valid PDF file.")

    # Progress tracking
    last_download_update = 0
    
    async def download_progress(current, total):
        nonlocal last_download_update
        percent = int(current * 100 / total) if total else 0
        if percent - last_download_update >= 5:
            last_download_update = percent
            try:
                await progress_message.edit(f"⬇️ Downloading PDF... {percent}%")
            except Exception:
                pass

    progress_message = await event.reply("⬇️ Downloading PDF... 0%")

    # Download PDF to memory
    input_stream = io.BytesIO()
    await event.client.download_media(reply, file=input_stream, progress_callback=download_progress)
    input_stream.seek(0)

    await progress_message.edit("✂️ Splitting PDF...")

    try:
        loop = asyncio.get_event_loop()
        
        def split_bytes(data_bytes):
            reader = PyPDF2.PdfReader(io.BytesIO(data_bytes))
            total = len(reader.pages)
            s_idx, e_idx = start - 1, end - 1
            
            if s_idx < 0 or e_idx >= total:
                raise ValueError(f"Pages {start}-{end} out of bounds (1-{total})")
            
            writer = PyPDF2.PdfWriter()
            for i in range(s_idx, e_idx + 1):
                writer.add_page(reader.pages[i])
            
            out = io.BytesIO()
            writer.write(out)
            out.seek(0)
            return out

        output_stream = await loop.run_in_executor(None, split_bytes, input_stream.read())

        filename = f"split_{start}_{end}_{uuid.uuid4().hex}.pdf"
        output_stream.name = filename

        # Upload progress
        last_upload_update = 0
        
        async def upload_progress(current, total):
            nonlocal last_upload_update
            percent = int(current * 100 / total) if total else 0
            if percent - last_upload_update >= 5:
                last_upload_update = percent
                try:
                    await progress_message.edit(f"⬆️ Uploading split PDF... {percent}%")
                except Exception:
                    pass

        await event.client.send_file(
            event.chat_id, 
            output_stream, 
            caption=f"✅ PDF has been split successfully! (Pages {start}-{end})", 
            progress_callback=upload_progress
        )
        
        await progress_message.delete()

    except ValueError as e:
        logger.error(f"PDF split error: {e}")
        await progress_message.edit(f"❌ Error: {str(e)}")
    except Exception as e:
        logger.exception("Error splitting PDF")
        await progress_message.edit(f"❌ An error occurred: {str(e)}")
        
        
@sudo_only
async def handle_setemoji(event, *args):
    """Set auto-reaction emoji for a channel."""
    if len(args) < 2:
        return await event.reply("❌ Usage: setreact @channelusername [emoji]")

    channel_username = args[0].lstrip('@')
    emoji = args[1]

    try:
        with get_db_cursor() as cur:
            cur.execute(
                "REPLACE INTO channel_emojis (channel_username, emoji) VALUES (%s, %s)", 
                (channel_username, emoji)
            )
        
        await event.reply(f"✅ Emoji '{emoji}' set for channel @{channel_username}.")
        logger.info(f"Emoji '{emoji}' set for @{channel_username}")
        
    except Exception as e:
        logger.exception("Error setting emoji for channel")
        await event.reply(f"❌ Error: {e}")


@sudo_only
async def handle_remreact(event, *args):
    """Remove auto-reaction for a channel."""
    if len(args) < 1:
        return await event.reply("❌ Usage: remreact @channelusername")

    channel_username = args[0].lstrip('@')

    try:
        with get_db_cursor() as cur:
            cur.execute("DELETE FROM channel_emojis WHERE channel_username = %s", (channel_username,))
        
        await event.reply(f"✅ Emoji removed for channel @{channel_username}.")
        logger.info(f"Emoji removed for @{channel_username}")
        
    except Exception as e:
        logger.exception("Error removing emoji for channel")
        await event.reply(f"❌ Error: {e}")


@sudo_only
async def handle_reactlist(event, *args):
    """List all configured auto-reactions."""
    try:
        with get_db_cursor() as cur:
            cur.execute("SELECT channel_username, emoji FROM channel_emojis")
            channels = cur.fetchall()

        if channels:
            channels_info = "\n".join([f"@{ch['channel_username']} - {ch['emoji']}" for ch in channels])
            await event.reply(f"✅ **Channel Emojis:**\n{channels_info}")
        else:
            await event.reply("ℹ️ No channels with emojis set.")
    
    except Exception as e:
        logger.exception("Error fetching react list")
        await event.reply(f"❌ Error: {e}")


@client.on(events.NewMessage(chats=None))
async def apply_emoji(event):
    """Automatically apply emoji reactions to channel messages."""
    if event.chat and event.chat.username:
        channel_username = event.chat.username
        
        with get_db_cursor() as cur:
            cur.execute("SELECT emoji FROM channel_emojis WHERE channel_username = %s", (channel_username,))
            result = cur.fetchone()

        if result:
            emoji = result['emoji']
            try:
                await client(functions.messages.SendReactionRequest(
                    peer=event.chat_id,
                    msg_id=event.message.id,
                    reaction=[types.ReactionEmoji(emoticon=emoji)]
                ))
                logger.debug(f"✅ Emoji '{emoji}' applied to message in @{channel_username}")
                
            except ChatWriteForbiddenError:
                logger.error(f"❌ Bot lacks permissions to react in @{channel_username}")
            except Exception as e:
                logger.error(f"❌ Failed to apply emoji: {e}")
                
                
def find_vazirmatn_font():
    """Find Vazirmatn font file for Persian support."""
    font_paths = [
        'fonts/Vazirmatn-Regular.ttf',
        'fonts/Vazirmatn.ttf',
        './Vazirmatn-Regular.ttf',
        '/usr/share/fonts/truetype/vazirmatn/Vazirmatn-Regular.ttf',
        os.path.join(os.path.dirname(__file__), 'fonts', 'Vazirmatn-Regular.ttf'),
    ]
    
    for font_path in font_paths:
        if os.path.exists(font_path):
            logger.info(f"✅ Found Vazirmatn font: {font_path}")
            return font_path
    
    logger.warning("⚠️ Vazirmatn font not found")
    return None


def register_persian_font():
    """Register Vazirmatn font for Persian text."""
    font_path = find_vazirmatn_font()
    if font_path:
        try:
            pdfmetrics.registerFont(TTFont('Vazirmatn', font_path))
            logger.info("✅ Persian font registered successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to register Persian font: {e}")
            return False
    return False


def split_text_to_lines(text, canvas_obj, font_name, font_size, max_width):
    """
    Split text into lines that fit within max_width.
    Works with both English and Persian text.
    """
    words = text.split()
    lines = []
    current_line = []
    
    for word in words:
        test_words = current_line + [word]
        test_line = ' '.join(test_words)
        
        # Measure width
        width = canvas_obj.stringWidth(test_line, font_name, font_size)
        
        if width <= max_width:
            current_line.append(word)
        else:
            if current_line:
                lines.append(' '.join(current_line))
                current_line = [word]
            else:
                # Single word too long - force it
                lines.append(word)
    
    if current_line:
        lines.append(' '.join(current_line))
    
    return lines


def process_persian_text(text):
    """Process Persian text with proper BiDi algorithm."""
    try:
        reshaped = arabic_reshaper.reshape(text)
        bidi_text = get_display(reshaped)
        return bidi_text
    except ImportError:
        logger.warning("arabic_reshaper or python-bidi not installed")
        return text
    except Exception as e:
        logger.warning(f"BiDi processing failed: {e}")
        return text


def has_persian_characters(text):
    """Check if text contains Persian/Arabic characters."""
    return any('\u0600' <= ch <= '\u06FF' for ch in text)


def create_pdf_from_text(text, filename, language="en", font_size=12):
    """
    Create PDF from text with English or Persian support.
    
    Args:
        text: Text content to convert
        filename: Output PDF filename
        language: "en" or "fa"
        font_size: Font size (default: 12)
    """
    
    # Determine font and alignment
    if language == "fa":
        has_font = register_persian_font()
        font_name = 'Vazirmatn' if has_font else 'Helvetica'
        alignment = "rtl"
        
        if not has_font:
            logger.warning("⚠️ Persian font not available, falling back to Helvetica")
    else:
        font_name = 'Helvetica'
        alignment = "ltr"
    
    # Create canvas
    c = canvas.Canvas(filename, pagesize=A4)
    width, height = A4
    
    # Settings
    line_spacing = font_size + 8
    
    # Margins
    margin_left = 50
    margin_right = 50
    margin_top = 50
    margin_bottom = 50
    
    # Calculate usable width
    usable_width = width - margin_left - margin_right
    
    # Starting Y position (from top)
    y = height - margin_top
    
    # Set font
    c.setFont(font_name, font_size)
    
    # Split text into paragraphs
    paragraphs = text.split('\n')
    
    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        
        if not paragraph:
            # Empty line - add space
            y -= line_spacing / 2
            if y < margin_bottom:
                c.showPage()
                c.setFont(font_name, font_size)
                y = height - margin_top
            continue
        
        # Process text based on language
        if language == "fa" and has_persian_characters(paragraph):
            # Process Persian text with BiDi
            processed_text = process_persian_text(paragraph)
            lines = split_text_to_lines(processed_text, c, font_name, font_size, usable_width)
            
            # Draw lines (RTL - from right)
            for line in lines:
                if y < margin_bottom:
                    c.showPage()
                    c.setFont(font_name, font_size)
                    y = height - margin_top
                
                c.drawRightString(width - margin_right, y, line)
                y -= line_spacing
        else:
            # English/LTR text
            lines = split_text_to_lines(paragraph, c, font_name, font_size, usable_width)
            
            # Draw lines (LTR - from left)
            for line in lines:
                if y < margin_bottom:
                    c.showPage()
                    c.setFont(font_name, font_size)
                    y = height - margin_top
                
                c.drawString(margin_left, y, line)
                y -= line_spacing
    
    # Save
    c.save()
    logger.info(f"✅ PDF created: {filename} (language: {language}, font: {font_name}, size: {font_size})")


async def handle_text_to_pdf(event, *args):
    """
    Convert replied text to PDF with English or Persian support.
    
    Usage:
      - topdf en [size]  - English PDF (default font size: 12)
      - topdf fa [size]  - Persian PDF (default font size: 12)
      - topdf [size]     - Auto-detect language
    
    Examples:
      - topdf en
      - topdf fa
      - topdf en 14
      - topdf fa 16
      - topdf
    """
    
    # Validation
    if not event.is_reply:
        return await event.reply("❌ Please reply to a text message.")

    reply_msg = await event.get_reply_message()
    text_content = reply_msg.raw_text
    
    if not text_content or not text_content.strip():
        return await event.reply("❌ Message is empty.")

    # Parse arguments
    language = None
    font_size = 12
    
    if args:
        # Check first argument
        first_arg = args[0].lower()
        
        if first_arg in ["en", "english"]:
            language = "en"
            # Check for font size
            if len(args) > 1:
                try:
                    font_size = int(args[1])
                except ValueError:
                    return await event.reply("❌ Invalid font size. Please provide a number.")
        
        elif first_arg in ["fa", "persian"]:
            language = "fa"
            # Check for font size
            if len(args) > 1:
                try:
                    font_size = int(args[1])
                except ValueError:
                    return await event.reply("❌ Invalid font size. Please provide a number.")
        
        else:
            # First argument might be font size (auto-detect language)
            try:
                font_size = int(first_arg)
            except ValueError:
                return await event.reply("❌ Usage: `topdf en/fa [size]` or `topdf [size]`")
    
    # Validate font size
    if font_size < 6 or font_size > 72:
        return await event.reply("❌ Font size must be between 6 and 72.")
    
    # Auto-detect language if not specified
    if language is None:
        language = "fa" if has_persian_characters(text_content[:100]) else "en"

    progress_msg = await event.reply("📄 Creating PDF...")
    pdf_filename = f"text_{event.id}_{int(time.time())}.pdf"

    try:
        # Create PDF
        await asyncio.get_event_loop().run_in_executor(
            None,
            create_pdf_from_text,
            text_content,
            pdf_filename,
            language,
            font_size
        )

        # Verify file was created
        if not os.path.exists(pdf_filename) or os.path.getsize(pdf_filename) == 0:
            raise ValueError("PDF creation failed")

        # Send PDF
        await safe_edit_message(progress_msg, "⬆️ Uploading...")
        
        # Create caption
        lang_name = "Persian" if language == "fa" else "English"
        font_name = "Vazirmatn" if language == "fa" else "Helvetica"
        
        caption = (
            f"📄 **PDF Created**\n"
            f"🌐 **Language:** {lang_name}\n"
            f"🔤 **Font:** {font_name}\n"
            f"📏 **Size:** {font_size}pt\n"
            f"📊 **Length:** {len(text_content)} characters"
        )
        
        await event.client.send_file(
            event.chat_id,
            pdf_filename,
            caption=caption
        )

        await progress_msg.delete()
        logger.info(f"PDF sent: {pdf_filename} (language: {language}, font: {font_name}, size: {font_size})")

    except Exception as e:
        logger.error(f"Error in handle_text_to_pdf: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        await safe_edit_message(progress_msg, f"❌ Error: {str(e)}")
    
    finally:
        # Cleanup
        if os.path.exists(pdf_filename):
            try:
                os.remove(pdf_filename)
            except Exception as e:
                logger.warning(f"Failed to delete temp PDF: {e}")
           

async def handle_qr_generate(event, *args):
    """
    Generate QR code from text.
    
    Usage:
      - qr [text] - Generate QR code
      - qr [text] [size] - Generate with custom size (default: 10)
    
    Examples:
      - qr https://google.com
      - qr Hello World
      - qr https://example.com 15
    """
    
    if not args:
        return await event.reply("❌ Usage: `qr [text]` or `qr [text] [size]`")
    
    # Parse arguments
    size = 10  # Default size
    
    # Check if last argument is a number (size)
    try:
        if args[-1].isdigit():
            size = int(args[-1])
            text_to_encode = ' '.join(args[:-1])
        else:
            text_to_encode = ' '.join(args)
    except:
        text_to_encode = ' '.join(args)
    
    # Validate size
    if size < 1 or size > 40:
        return await event.reply("❌ Size must be between 1 and 40.")
    
    if not text_to_encode or not text_to_encode.strip():
        return await event.reply("❌ Text cannot be empty.")
    
    progress_msg = await event.reply("🔲 Generating QR code...")
    
    qr_filename = f"qr_{event.id}_{int(time.time())}.png"
    
    try:
        # Create QR code
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=size,
            border=4,
        )
        
        qr.add_data(text_to_encode)
        qr.make(fit=True)
        
        # Create image
        img = qr.make_image(fill_color="black", back_color="white")
        img.save(qr_filename)
        
        # Verify file
        if not os.path.exists(qr_filename) or os.path.getsize(qr_filename) == 0:
            raise ValueError("QR code generation failed")
        
        await safe_edit_message(progress_msg, "⬆️ Uploading...")
        
        # Create caption
        caption = (
            f"🔲 **QR Code Generated**\n"
            f"📝 **Text:** `{text_to_encode[:100]}{'...' if len(text_to_encode) > 100 else ''}`\n"
            f"📏 **Size:** {size}\n"
            f"📊 **Length:** {len(text_to_encode)} characters"
        )
        
        # Send QR code
        await event.client.send_file(
            event.chat_id,
            qr_filename,
            caption=caption
        )
        
        await progress_msg.delete()
        logger.info(f"QR code generated: {qr_filename} (size: {size})")
    
    except Exception as e:
        logger.error(f"Error generating QR code: {str(e)}")
        await safe_edit_message(progress_msg, f"❌ Error: {str(e)}")
    
    finally:
        # Cleanup
        if os.path.exists(qr_filename):
            try:
                os.remove(qr_filename)
            except Exception as e:
                logger.warning(f"Failed to delete QR file: {e}")


async def handle_qr_advanced(event, *args):
    """
    Generate QR code with custom colors.
    
    Usage:
      - qradv [text] - Generate with default colors
      - qradv [text] [fg_color] [bg_color] - Custom colors
    
    Colors: black, white, red, blue, green, yellow, purple, orange
    
    Examples:
      - qradv https://example.com
      - qradv Hello red white
      - qradv My Text blue yellow
    """
    
    if not args:
        return await event.reply("❌ Usage: `qradv [text]` or `qradv [text] [fg_color] [bg_color]`")
    
    # Color map
    color_map = {
        'black': '#000000',
        'white': '#FFFFFF',
        'red': '#FF0000',
        'blue': '#0000FF',
        'green': '#00FF00',
        'yellow': '#FFFF00',
        'purple': '#800080',
        'orange': '#FFA500',
        'pink': '#FFC0CB',
        'cyan': '#00FFFF',
    }
    
    # Parse arguments
    fg_color = "black"
    bg_color = "white"
    
    # Check if last two arguments are colors
    if len(args) >= 3 and args[-2].lower() in color_map and args[-1].lower() in color_map:
        fg_color = args[-2].lower()
        bg_color = args[-1].lower()
        text_to_encode = ' '.join(args[:-2])
    elif len(args) >= 2 and args[-1].lower() in color_map:
        fg_color = args[-1].lower()
        text_to_encode = ' '.join(args[:-1])
    else:
        text_to_encode = ' '.join(args)
    
    if not text_to_encode or not text_to_encode.strip():
        return await event.reply("❌ Text cannot be empty.")
    
    progress_msg = await event.reply("🎨 Generating custom QR code...")
    
    qr_filename = f"qr_adv_{event.id}_{int(time.time())}.png"
    
    try:
        # Create QR code
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=10,
            border=4,
        )
        
        qr.add_data(text_to_encode)
        qr.make(fit=True)
        
        # Create image with custom colors
        img = qr.make_image(
            fill_color=color_map[fg_color],
            back_color=color_map[bg_color]
        )
        img.save(qr_filename)
        
        # Verify file
        if not os.path.exists(qr_filename) or os.path.getsize(qr_filename) == 0:
            raise ValueError("QR code generation failed")
        
        await safe_edit_message(progress_msg, "⬆️ Uploading...")
        
        # Create caption
        caption = (
            f"🎨 **Custom QR Code**\n"
            f"📝 **Text:** `{text_to_encode[:100]}{'...' if len(text_to_encode) > 100 else ''}`\n"
            f"🎨 **Colors:** {fg_color.title()} on {bg_color.title()}\n"
            f"📊 **Length:** {len(text_to_encode)} characters"
        )
        
        # Send QR code
        await event.client.send_file(
            event.chat_id,
            qr_filename,
            caption=caption
        )
        
        await progress_msg.delete()
        logger.info(f"Custom QR code generated: {fg_color}/{bg_color}")
    
    except Exception as e:
        logger.error(f"Error generating custom QR code: {str(e)}")
        await safe_edit_message(progress_msg, f"❌ Error: {str(e)}")
    
    finally:
        # Cleanup
        if os.path.exists(qr_filename):
            try:
                os.remove(qr_filename)
            except Exception as e:
                logger.warning(f"Failed to delete QR file: {e}")


async def handle_qr_read_api(event):
    """Read QR code using online API (no dependencies)."""
    
    if not event.is_reply:
        return await event.reply("❌ Please reply to an image containing a QR code.")
    
    reply_msg = await event.get_reply_message()
    
    if not (reply_msg.photo or reply_msg.document):
        return await event.reply("❌ Please reply to an image.")
    
    progress_msg = await event.reply("🔍 Reading QR code...")
    
    temp_image_path = None
    
    try:
        await safe_edit_message(progress_msg, "⬇️ Downloading image...")
        temp_image_path = await reply_msg.download_media()
        
        if not temp_image_path or not os.path.exists(temp_image_path):
            raise ValueError("Failed to download image")
        
        # Upload to API for decoding
        await safe_edit_message(progress_msg, "🔍 Decoding via API...")
        
        url = "https://api.qrserver.com/v1/read-qr-code/"
        
        with open(temp_image_path, 'rb') as f:
            files = {'file': f}
            response = requests.post(url, files=files, timeout=10)
        
        data = response.json()
        
        if data and len(data) > 0 and data[0]['symbol'][0]['data']:
            qr_data = data[0]['symbol'][0]['data']
            
            response_text = (
                f"✅ **QR Code Decoded**\n\n"
                f"📝 **Data:** `{qr_data}`\n"
                f"📊 **Length:** {len(qr_data)} characters"
            )
            
            await safe_edit_message(progress_msg, response_text)
            logger.info("QR code decoded successfully via API")
        else:
            await safe_edit_message(progress_msg, "❌ No QR code found in this image.")
    
    except requests.exceptions.RequestException as e:
        logger.error(f"API request error: {str(e)}")
        await safe_edit_message(progress_msg, "❌ Failed to connect to QR reading service.")
    except Exception as e:
        logger.error(f"Error reading QR code: {str(e)}")
        await safe_edit_message(progress_msg, f"❌ Error: {str(e)}")
    
    finally:
        if temp_image_path and os.path.exists(temp_image_path):
            try:
                os.remove(temp_image_path)
            except Exception as e:
                logger.warning(f"Failed to delete temp image: {e}")


async def handle_qreply_set(event, *args):
    """
    Set a quick reply alias.
    
    Usage:
      - qreply set [alias] [message] - Set quick reply with message
      - qreply set [alias] - Set from replied message (reply to any message)
    
    Examples:
      - qreply set email myemail@example.com
      - (Reply to a message) qreply set greeting
    """
    
    if len(args) < 2 or args[0].lower() != 'set':
        return await event.reply(
            "❌ Usage:\n"
            "`qreply set [alias] [message]`\n"
            "or reply to a message and use:\n"
            "`qreply set [alias]`"
        )
    
    user_id = event.sender_id
    alias = args[1].lower()
    
    # Validate alias
    if not alias.isalnum():
        return await event.reply("❌ Alias must contain only letters and numbers.")
    
    if len(alias) > 50:
        return await event.reply("❌ Alias must be 50 characters or less.")
    
    # Get message content
    message_content = None
    
    # Method 1: Message provided directly in command
    if len(args) > 2:
        message_content = ' '.join(args[2:])
    
    # Method 2: Get from replied message
    elif event.is_reply:
        replied_msg = await event.get_reply_message()
        
        # Get text content
        if replied_msg.raw_text:
            message_content = replied_msg.raw_text
        else:
            return await event.reply("❌ Replied message has no text content.")
    
    # No message found
    else:
        return await event.reply(
            "❌ Please provide a message or reply to a message.\n\n"
            "**Examples:**\n"
            "`qreply set email myemail@example.com`\n"
            "or reply to a message and use:\n"
            "`qreply set greeting`"
        )
    
    # Validate message
    if not message_content or not message_content.strip():
        return await event.reply("❌ Message cannot be empty.")
    
    if len(message_content) > 4000:
        return await event.reply("❌ Message is too long (max 4000 characters).")
    
    try:
        with get_db_cursor() as cur:
            cur.execute(
                "INSERT INTO quick_replies (user_id, alias, message) VALUES (%s, %s, %s) "
                "ON DUPLICATE KEY UPDATE message = %s",
                (user_id, alias, message_content, message_content)
            )
        
        # Show preview
        preview = message_content[:100] + ('...' if len(message_content) > 100 else '')
        
        # Show different messages based on how it was set
        if event.is_reply and len(args) == 2:
            source = "from replied message"
        else:
            source = "with custom message"
        
        await event.reply(
            f"✅ **Quick reply set {source}!**\n\n"
            f"**Alias:** `-{alias}`\n"
            f"**Message:** `{preview}`\n\n"
            f"Type `-{alias}` to use it."
        )
        
        logger.info(f"Quick reply set: {user_id} -> -{alias} ({source})")
    
    except Exception as e:
        logger.error(f"Error setting quick reply: {str(e)}")
        await event.reply(f"❌ Error: {str(e)}")


async def handle_qreply_remove(event, *args):
    """Remove a quick reply alias."""
    
    if len(args) < 2 or args[0].lower() not in ['remove', 'rm', 'del', 'delete']:
        return await event.reply("❌ Usage: `qreply remove [alias]`")
    
    user_id = event.sender_id
    alias = args[1].lower()
    
    try:
        with get_db_cursor() as cur:
            cur.execute(
                "DELETE FROM quick_replies WHERE user_id = %s AND alias = %s",
                (user_id, alias)
            )
            
            if cur.rowcount > 0:
                await event.reply(f"✅ Quick reply `-{alias}` removed.")
                logger.info(f"Quick reply removed: {user_id} -> -{alias}")
            else:
                await event.reply(f"❌ No quick reply found for `-{alias}`.")
    
    except Exception as e:
        logger.error(f"Error removing quick reply: {str(e)}")
        await event.reply(f"❌ Error: {str(e)}")


async def handle_qreply_list(event, *args):
    """List all quick replies for the user."""
    
    user_id = event.sender_id
    
    try:
        with get_db_cursor() as cur:
            cur.execute(
                "SELECT alias, message FROM quick_replies WHERE user_id = %s ORDER BY alias",
                (user_id,)
            )
            replies = cur.fetchall()
        
        if not replies:
            return await event.reply(
                "ℹ️ You have no quick replies set.\n\n"
                "Use `qreply set [alias] [message]` to create one."
            )
        
        # Build message
        message_lines = [f"📝 **Your Quick Replies** ({len(replies)}):\n"]
        
        for reply in replies:
            alias = reply['alias']
            msg = reply['message']
            preview = msg[:50] + ('...' if len(msg) > 50 else '')
            
            message_lines.append(f"• `-{alias}` → `{preview}`")
        
        message_lines.append(f"\n💡 Type `-[alias]` to use")
        
        await event.reply("\n".join(message_lines))
    
    except Exception as e:
        logger.error(f"Error listing quick replies: {str(e)}")
        await event.reply(f"❌ Error: {str(e)}")


async def handle_qreply_info(event, *args):
    """Show full message for a quick reply alias."""
    
    if len(args) < 2 or args[0].lower() != 'info':
        return await event.reply("❌ Usage: `qreply info [alias]`")
    
    user_id = event.sender_id
    alias = args[1].lower()
    
    try:
        with get_db_cursor() as cur:
            cur.execute(
                "SELECT message, created_at FROM quick_replies WHERE user_id = %s AND alias = %s",
                (user_id, alias)
            )
            result = cur.fetchone()
        
        if not result:
            return await event.reply(f"❌ No quick reply found for `-{alias}`.")
        
        message = result['message']
        created = result['created_at']
        
        await event.reply(
            f"📝 **Quick Reply Info**\n\n"
            f"**Alias:** `-{alias}`\n"
            f"**Created:** {created}\n"
            f"**Length:** {len(message)} characters\n\n"
            f"**Message:**\n{message}"
        )
    
    except Exception as e:
        logger.error(f"Error getting quick reply info: {str(e)}")
        await event.reply(f"❌ Error: {str(e)}")


async def handle_qreply_main(event, *args):
    """Main quick reply handler."""
    
    if not args:
        return await event.reply(
            "📝 **Quick Reply Commands**\n\n"
            "`qreply set [alias] [message]` - Create\n"
            "`qreply set [alias]` - Create from reply\n"
            "`qreply remove [alias]` - Delete\n"
            "`qreply list` - Show all\n"
            "`qreply info [alias]` - View details\n\n"
            "**Usage:** Type `-[alias]` to use"
        )
    
    subcommand = args[0].lower()
    
    if subcommand == 'set':
        await handle_qreply_set(event, *args)
    elif subcommand in ['remove', 'rm', 'del', 'delete']:
        await handle_qreply_remove(event, *args)
    elif subcommand in ['list', 'ls']:
        await handle_qreply_list(event, *args)
    elif subcommand == 'info':
        await handle_qreply_info(event, *args)
    else:
        await event.reply("❌ Unknown command. Use `qreply` to see available commands.")


async def handle_quick_reply_trigger(event):
    """
    Handle quick reply triggers - ONLY for YOUR messages.
    When YOU type -alias, bot edits YOUR message to the saved content.
    """
    
    # IMPORTANT: Only process YOUR OWN messages
    if not event.message.out:
        return False
    
    text = event.raw_text.strip()
    
    # Check if message starts with -
    if not text.startswith('-'):
        return False
    
    # Extract alias (remove the - prefix)
    alias = text[1:].strip().lower()
    
    # Don't process if empty or contains spaces
    if not alias or ' ' in alias:
        return False
    
    user_id = event.sender_id
    
    try:
        with get_db_cursor() as cur:
            cur.execute(
                "SELECT message FROM quick_replies WHERE user_id = %s AND alias = %s",
                (user_id, alias)
            )
            result = cur.fetchone()
        
        if result:
            message_content = result['message']
            
            # Edit YOUR message to the quick reply content
            try:
                await event.edit(message_content)
                logger.info(f"Quick reply triggered: -{alias}")
                return True
            except Exception as e:
                logger.error(f"Failed to edit message: {e}")
                return False
        
        return False
    
    except Exception as e:
        logger.error(f"Error handling quick reply trigger: {str(e)}")
        return False


async def handle_define(event, *args):
    """
    Get word definition with English/Persian meanings and pronunciation.
    
    Usage:
      - dic [word]
    
    Examples:
      - dic water
      - dic computer
      - dic beautiful
    """
    
    if not args:
        return await event.reply("❌ Usage: <code>dic [word]</code>", parse_mode='html')
    
    word = ' '.join(args).strip().lower()
    
    if not word:
        return await event.reply("❌ Please provide a word to define.")
    
    # Validate word (only letters, hyphens, apostrophes)
    if not all(c.isalpha() or c in ['-', "'", ' '] for c in word):
        return await event.reply("❌ Invalid word format.")
    
    progress_msg = await event.reply(f"📖 Looking up '{word}'...")
    
    try:
        # Get definitions from Free Dictionary API
        dict_url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{word}"
        
        response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: requests.get(dict_url, timeout=10)
        )
        
        if response.status_code == 404:
            return await safe_edit_message(
                progress_msg,
                f"❌ Word '{word}' not found in dictionary.\n\n"
                f"Please check the spelling and try again."
            )
        
        response.raise_for_status()
        data = response.json()
        
        if not data or len(data) == 0:
            return await safe_edit_message(progress_msg, f"❌ No definition found for '{word}'.")
        
        # Get Persian translation
        await safe_edit_message(progress_msg, f"📖 Translating '{word}' to Persian...")
        
        persian_word = None
        try:
            persian_url = "https://translate.googleapis.com/translate_a/single"
            params = {
                'client': 'gtx',
                'sl': 'en',
                'tl': 'fa',
                'dt': 't',
                'q': word
            }
            
            trans_response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: requests.get(persian_url, params=params, timeout=5)
            )
            
            if trans_response.status_code == 200:
                trans_data = trans_response.json()
                if trans_data and len(trans_data) > 0 and trans_data[0]:
                    persian_word = trans_data[0][0][0]
        except Exception as e:
            logger.warning(f"Persian translation failed: {e}")
        
        # Process all entries from API (multiple word forms)
        all_meanings = {}  # Group by part of speech
        word_title = word
        phonetic = ""
        audio_url = None
        
        for entry in data:
            # Get word title and phonetic from first entry
            if not word_title or word_title == word:
                word_title = entry.get('word', word)
            
            # Get phonetic (prefer one with text)
            if not phonetic:
                entry_phonetic = entry.get('phonetic', '')
                if entry_phonetic:
                    phonetic = entry_phonetic
            
            # Get audio URL (prefer US pronunciation)
            if not audio_url:
                phonetics_list = entry.get('phonetics', [])
                for p in phonetics_list:
                    if p.get('audio'):
                        audio_url = p['audio']
                        # Prefer US pronunciation
                        if 'us' in audio_url.lower():
                            break
            
            # Collect all meanings
            meanings = entry.get('meanings', [])
            for meaning in meanings:
                part_of_speech = meaning.get('partOfSpeech', 'unknown')
                definitions = meaning.get('definitions', [])
                
                # Group definitions by part of speech
                if part_of_speech not in all_meanings:
                    all_meanings[part_of_speech] = []
                
                all_meanings[part_of_speech].extend(definitions)
        
        # Build definition message with HTML formatting
        message_parts = [f"📖 <b>{word_title.title()}</b>"]
        
        if phonetic:
            message_parts.append(f"🔊 <code>{phonetic}</code>")
        
        message_parts.append("")  # Blank line
        
        if persian_word:
            message_parts.append(f"🇮🇷 <b>Persian:</b> {persian_word}")
            message_parts.append("")
        
        # Part of speech emojis
        pos_emojis = {
            'noun': '🟦',
            'verb': '🟥',
            'adjective': '🟩',
            'adverb': '🟨',
            'pronoun': '🟪',
            'preposition': '🟧',
            'conjunction': '🟫',
            'interjection': '⬜',
            'exclamation': '⬜'
        }
        
        # Number emojis for definitions
        number_emojis = ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣', '6️⃣', '7️⃣', '8️⃣', '9️⃣', '🔟']
        
        # Add all meanings grouped by part of speech
        for part_of_speech, definitions in all_meanings.items():
            pos_emoji = pos_emojis.get(part_of_speech.lower(), '🟥')
            message_parts.append(f"{pos_emoji} <b>{part_of_speech.title()}:</b>")
            
            # Add up to 10 definitions per part of speech
            for idx, definition in enumerate(definitions[:10]):
                def_text = definition.get('definition', '')
                example = definition.get('example', '')
                synonyms = definition.get('synonyms', [])
                
                # Use emoji number instead of plain number
                emoji_num = number_emojis[idx] if idx < len(number_emojis) else f"{idx+1}."
                message_parts.append(f"{emoji_num} {def_text}")
                
                if example:
                    message_parts.append(f"      <i>🔸 Example: \"{example}\"</i>")
                
                # Add synonyms if available (limit to 3)
                if synonyms and len(synonyms) > 0:
                    syn_list = ', '.join(synonyms[:3])
                    message_parts.append(f"      <i>💡 Synonyms: {syn_list}</i>")
            
            message_parts.append("")  # Blank line between parts of speech
        
        if len(all_meanings) == 0:
            message_parts.append("No detailed definitions available.")
        
        # Send definition message with HTML formatting
        definition_text = "\n".join(message_parts)
        
        try:
            definition_msg = await event.reply(definition_text, parse_mode='html')
        except Exception as e:
            # If HTML fails, send without formatting
            logger.warning(f"HTML formatting failed: {e}")
            definition_msg = await event.reply(definition_text)
        
        # Delete the "Looking up..." progress message
        await progress_msg.delete()
        
        # Download and send pronunciation audio
        if audio_url:
            await download_and_send_pronunciation(event, word_title, audio_url)
        else:
            logger.warning(f"No audio URL found for word: {word_title}")
    
    except requests.exceptions.Timeout:
        await safe_edit_message(progress_msg, "❌ Request timeout. Please try again.")
    except requests.exceptions.RequestException as e:
        logger.error(f"Dictionary API error: {e}")
        await safe_edit_message(progress_msg, f"❌ Failed to fetch definition. Please try again.")
    except Exception as e:
        logger.error(f"Error in define command: {str(e)}")
        await safe_edit_message(progress_msg, f"❌ An error occurred: {str(e)}")


async def download_and_send_pronunciation(event, word, audio_url):
    """
    Download pronunciation audio from the API and send as voice message.
    """
    
    filename = None
    pronunciation_msg = None
    
    try:
        # Show pronunciation progress
        pronunciation_msg = await event.reply("🎙️ Downloading pronunciation...")
        
        # Download audio file
        response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: requests.get(audio_url, timeout=15)
        )
        response.raise_for_status()
        
        # Save audio file
        filename = f"pronunciation_{word}_{int(time.time())}.mp3"
        
        with open(filename, "wb") as f:
            f.write(response.content)
        
        file_size = os.path.getsize(filename)
        if file_size == 0:
            raise ValueError("Downloaded audio file is empty")
        
        logger.info(f"Downloaded pronunciation for '{word}': {filename} ({file_size} bytes)")
        
        # Send as voice message
        await event.client.send_file(
            event.chat_id,
            filename,
            voice_note=True,
            attributes=[
                types.DocumentAttributeAudio(
                    duration=0,
                    voice=True,
                    title=f"Pronunciation: {word}",
                    performer="Dictionary API"
                )
            ]
        )
        
        # Delete the "Downloading pronunciation..." message
        await pronunciation_msg.delete()
        
        logger.info(f"Sent pronunciation for: {word}")
    
    except requests.exceptions.Timeout:
        if pronunciation_msg:
            await pronunciation_msg.edit("⚠️ Pronunciation download timeout")
    except Exception as e:
        logger.error(f"Error downloading pronunciation: {e}")
        if pronunciation_msg:
            try:
                await pronunciation_msg.delete()
            except:
                pass
    
    finally:
        # Cleanup
        if filename and os.path.exists(filename):
            try:
                os.remove(filename)
            except Exception as e:
                logger.warning(f"Failed to delete pronunciation file: {e}")
              
##-------------------------------------COMMANDS--------------------------------------##

command_map = {
    # Bot Control
    'self': handle_self_command,
    
    # Messaging
    'spam': handle_spam,
    'spamset': handle_spamset,
    'cancel': handle_spam_cancel,
    
    # Message Management
    'del': handle_del,
    
    # Information
    'info': handle_info,
    'help': handle_user_help,
    
    # Backup & Database
    'backup': handle_backup,
    'dbupdate': handle_db_import,
    
    # Weather
    'dw': handle_daily_weather,
    'hw': handle_hourly_weather,
    
    # Finance
    'currency': handle_currency,
    
    # Conversions
    'tts': handle_tts,
    
    # File Operations
    'zipfile': handle_zip_command,
    'unzip': handle_unzip_command,
    'add': handle_ziplist_command,
    'zipit': handle_zipfolder_command,
    'rename': handle_rename,
    'metadata': handle_metadata,
    'split': handle_split,
    
    # AI
    'gpt': lambda event, *args: handle_gpt(event, *args, web_access=False, reasoning=False),
    'gpts': lambda event, *args: handle_gpt(event, *args, web_access=True, reasoning=False),
    'gptr': lambda event, *args: handle_gpt(event, *args, web_access=False, reasoning=True),
    'imagine': handle_imagine,
    
    # Books & Articles
    'annas': handle_book_search,
    'art': handle_article_search,
    'dl': handle_book_download_by_md5,
    
    # Admin
    'setadmin': handle_setadmin,
    'remadmin': handle_remadmin,
    'adminlist': handle_adminlist,
    
    'topdf': handle_text_to_pdf,
    
    'qr': handle_qr_generate,
    'qradv': handle_qr_advanced,
    'qrread': handle_qr_read_api,
    
    'qreply': handle_qreply_main,
    'dic': handle_define,
    
    'setreact': handle_setemoji,
    'remreact': handle_remreact,
    'reactlist': handle_reactlist
}

##-------------------------------------HANDLE_NEW_MESSAGE--------------------------------------##

@client.on(events.NewMessage())
async def handle_new_message(event):
    """Main message handler."""
    global bot_active

    text = event.raw_text.strip()
    sender_id = event.sender_id

    # ===== HANDLE YOUR OWN MESSAGES =====
    if event.message.out:
        # Check for quick reply trigger FIRST (only for you)
        if text.startswith('-') and await handle_quick_reply_trigger(event):
            return  # Quick reply was handled, stop processing

        # Allow only "self on" if bot is inactive
        if not bot_active and text.lower() != "self on":
            return

        parts = text.split()
        if not parts:
            return

        command = parts[0].lower()
        args = parts[1:]

        # Execute command
        handler = command_map.get(command)
        if handler:
            try:
                await handler(event, *args)
            except Exception as e:
                logger.exception(f"Error executing command '{command}': {e}")
                await event.reply(f"❌ Error executing command: {str(e)}")
        
        # Handle book download by MD5
        elif text.startswith("dl_"):
            book_id = text[3:]
            if len(book_id) == 32:
                await handle_book_download_by_md5(event, book_id)
            else:
                await event.reply("❌ Invalid book ID format")
    
    # ===== HANDLE MESSAGES FROM ADMINS =====
    else:
        # Check authorization
        if not is_authorized(event):
            return
        
        # Bot must be active for admin commands
        if not bot_active:
            return
        
        parts = text.split()
        if not parts:
            return

        command = parts[0].lower()
        args = parts[1:]

        # Execute command for admins
        handler = command_map.get(command)
        if handler:
            try:
                await handler(event, *args)
            except Exception as e:
                logger.exception(f"Error executing command '{command}' for admin {sender_id}: {e}")
                await event.reply(f"❌ Error executing command: {str(e)}")
        
        # Handle book download by MD5 for admins
        elif text.startswith("dl_"):
            book_id = text[3:]
            if len(book_id) == 32:
                await handle_book_download_by_md5(event, book_id)
            else:
                await event.reply("❌ Invalid book ID format")
        
async def start_bot():
    """Initialize bot on startup."""
    logger.info("Bot is starting...")
    
    startup_msg = (
        "🤖 **Self-Bot Online**\n\n"
        f"✅ Commands active: {len(command_map)}\n"
        f"📊 Database connected\n"
        f"⚡ Ready to use!\n\n"
        f"Type `help` for available commands."
    )
    
    await client.send_message('me', startup_msg)
    logger.info("Bot startup complete.")


async def main():
    """Main entry point."""
    try:
        await client.start(phone=TELEGRAM_CONFIG['phone_number'])
        logger.info("Client started successfully")

        await start_bot()

        await client.run_until_disconnected()
        logger.info("Bot is running...")

    except KeyboardInterrupt:
        logger.info("Bot shutdown requested")
    except Exception as e:
        logger.error(f"Error during bot startup: {str(e)}")
        raise
    finally:
        logger.info("Bot shutting down")


if __name__ == "__main__":
    logger.info("Starting bot script...")
    asyncio.run(main())