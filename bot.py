
import os
import time
import html
import secrets
import logging
import sqlite3
from functools import wraps

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError, BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIGURATION
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = 7365379630
DB_NAME = "bot_data.db"

DELETE_AFTER = 15
MAX_DELIVERIES = 3
COOLDOWN_SECONDS = 6 * 60 * 60

# Your two private-channel invite links
DEFAULT_INVITES = {
    1: "https://t.me/+I3EXMLDZG6wzMWM0",
    2: "https://t.me/+KSoQS4nmyHEwYjY9",
}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing from .env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# =========================================================
# DATABASE
# =========================================================

def db():
    con = sqlite3.connect(DB_NAME, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def setup_db():
    with db() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY
            );

            CREATE TABLE IF NOT EXISTS channels (
                chat_id TEXT PRIMARY KEY,
                username TEXT,
                title TEXT NOT NULL,
                join_url TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pending_invites (
                slot INTEGER PRIMARY KEY,
                invite_url TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS videos (
                token TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                file_type TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS video_channels (
                token TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                PRIMARY KEY (token, chat_id),
                FOREIGN KEY(token) REFERENCES videos(token)
                    ON DELETE CASCADE,
                FOREIGN KEY(chat_id) REFERENCES channels(chat_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS video_usage (
                token TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                deliveries INTEGER NOT NULL DEFAULT 0,
                cooldown_until REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(token, user_id),
                FOREIGN KEY(token) REFERENCES videos(token)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS banned_users (
                user_id INTEGER PRIMARY KEY,
                reason TEXT NOT NULL DEFAULT '',
                banned_at REAL NOT NULL
            );
        """)

        con.execute(
            "INSERT OR IGNORE INTO admins(user_id) VALUES (?)",
            (OWNER_ID,),
        )

        for slot, url in DEFAULT_INVITES.items():
            con.execute(
                """
                INSERT OR IGNORE INTO pending_invites(slot, invite_url)
                VALUES (?, ?)
                """,
                (slot, url),
            )


# =========================================================
# PERMISSIONS
# =========================================================

def is_admin(user_id):
    with db() as con:
        return con.execute(
            "SELECT 1 FROM admins WHERE user_id=?",
            (user_id,),
        ).fetchone() is not None


def is_banned(user_id):
    with db() as con:
        return con.execute(
            "SELECT 1 FROM banned_users WHERE user_id=?",
            (user_id,),
        ).fetchone() is not None


def admin_only(func):
    @wraps(func)
    async def wrapper(update, context):
        user = update.effective_user
        if not user or not is_admin(user.id):
            if update.effective_message:
                await update.effective_message.reply_text(
                    "⛔ Administrators only."
                )
            return
        return await func(update, context)
    return wrapper


def owner_only(func):
    @wraps(func)
    async def wrapper(update, context):
        user = update.effective_user
        if not user or user.id != OWNER_ID:
            if update.effective_message:
                await update.effective_message.reply_text(
                    "👑 Only the bot owner can use this command."
                )
            return
        return await func(update, context)
    return wrapper


# =========================================================
# KEYBOARDS / UI
# =========================================================

def home_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📖 Help", callback_data="ui:help"),
            InlineKeyboardButton("📢 Channels", callback_data="ui:channels"),
        ],
        [
            InlineKeyboardButton(
                "🛠 Admin Dashboard",
                callback_data="ui:admin",
            )
        ],
    ])


def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 View Channels", callback_data="ui:channels")],
        [InlineKeyboardButton("📋 Admin Commands", callback_data="ui:adminhelp")],
        [InlineKeyboardButton("🏠 Home", callback_data="ui:home")],
    ])


def config_keyboard(token, channels, selected):
    rows = []

    for channel in channels:
        cid = channel["chat_id"]
        mark = "✅" if cid in selected else "⬜"
        rows.append([
            InlineKeyboardButton(
                f"{mark} {channel['title'][:30]}",
                callback_data=f"cfg:{token}:toggle:{cid}",
            )
        ])

    rows += [
        [
            InlineKeyboardButton("☑️ Select All", callback_data=f"cfg:{token}:all"),
            InlineKeyboardButton("🧹 Select None", callback_data=f"cfg:{token}:none"),
        ],
        [
            InlineKeyboardButton("💾 Save Settings", callback_data=f"cfg:{token}:save")
        ],
    ]
    return InlineKeyboardMarkup(rows)


def join_keyboard(channels, token):
    rows = [
        [
            InlineKeyboardButton(
                f"📢 Join {channel['title'][:30]}",
                url=channel["join_url"],
            )
        ]
        for channel in channels
    ]
    rows.append([
        InlineKeyboardButton(
            "✅ I've Joined — Verify",
            callback_data=f"check:{token}",
        )
    ])
    return InlineKeyboardMarkup(rows)


# =========================================================
# DATABASE HELPERS
# =========================================================

def get_video(token):
    with db() as con:
        return con.execute(
            "SELECT * FROM videos WHERE token=?",
            (token,),
        ).fetchone()


def get_channels():
    with db() as con:
        return con.execute(
            "SELECT * FROM channels ORDER BY title"
        ).fetchall()


def get_video_channels(token):
    with db() as con:
        return con.execute("""
            SELECT c.*
            FROM channels c
            JOIN video_channels vc ON vc.chat_id=c.chat_id
            WHERE vc.token=?
            ORDER BY c.title
        """, (token,)).fetchall()


def get_selected_ids(token):
    with db() as con:
        rows = con.execute(
            "SELECT chat_id FROM video_channels WHERE token=?",
            (token,),
        ).fetchall()
    return {row["chat_id"] for row in rows}


def create_video(file_id, file_type, user_id):
    while True:
        token = secrets.token_urlsafe(6)
        try:
            with db() as con:
                con.execute("""
                    INSERT INTO videos
                    (token, file_id, file_type, created_by, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (token, file_id, file_type, user_id, time.time()))
            return token
        except sqlite3.IntegrityError:
            continue


async def share_link(context, token):
    bot_info = await context.bot.get_me()
    return f"https://t.me/{bot_info.username}?start=v_{token}"


# =========================================================
# START / HELP
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    if is_banned(user.id):
        await message.reply_text("🚫 Your access to this bot is restricted.")
        return

    if context.args and context.args[0].startswith("v_"):
        await deliver_video(context, user.id, context.args[0][2:])
        return

    await message.reply_text(
        f"✨ <b>WELCOME, {html.escape(user.first_name or 'there')}!</b>\n\n"
        "🎬 <b>Temporary Video Hub</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔗 Open a video link to receive a video.\n"
        "📢 Join required channels when prompted.\n"
        "⏳ Delivered video messages disappear after 15 seconds.\n\n"
        "Choose an option below 👇",
        parse_mode=ParseMode.HTML,
        reply_markup=home_keyboard(),
    )


async def help_command(update, context):
    if is_banned(update.effective_user.id):
        await update.effective_message.reply_text("🚫 Access restricted.")
        return

    await update.effective_message.reply_text(
        "📖 <b>HOW TO USE</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "1️⃣ Open the video link.\n"
        "2️⃣ Join the required channels.\n"
        "3️⃣ Press <b>I've Joined — Verify</b>.\n"
        "4️⃣ Receive your video.\n\n"
        "⏳ Deletion: 15 seconds after delivery.\n"
        "🔢 Limit: 3 successful deliveries per video.\n"
        "🕕 Cooldown: 6 hours after the third delivery.",
        parse_mode=ParseMode.HTML,
        reply_markup=home_keyboard(),
    )


async def ui_callback(update, context):
    q = update.callback_query
    uid = q.from_user.id

    if is_banned(uid):
        await q.answer("Access restricted.", show_alert=True)
        return

    await q.answer()
    action = q.data.split(":", 1)[1]

    if action == "home":
        await q.message.reply_text(
            "🏠 <b>HOME</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=home_keyboard(),
        )

    elif action == "help":
        await q.message.reply_text(
            "📖 Open a video link, join required channels, then verify. "
            "Video messages are deleted after 15 seconds."
        )

    elif action == "channels":
        channels = get_channels()
        text = "📢 <b>CONFIGURED CHANNELS</b>\n━━━━━━━━━━━━━━━━━━\n"

        if not channels:
            text += "No channels registered yet."
        else:
            for channel in channels:
                text += (
                    f"\n• {html.escape(channel['title'])}\n"
                    f"  {html.escape(channel['join_url'])}\n"
                )

        await q.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    elif action in ("admin", "adminhelp"):
        if not is_admin(uid):
            await q.message.reply_text("⛔ Administrators only.")
            return

        await q.message.reply_text(
            "🛠 <b>ADMIN DASHBOARD</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🎬 Send a video to create a share link.\n\n"
            "<b>Channel management</b>\n"
            "/pendingchannels\n"
            "/registerchannel -100CHANNEL_ID SLOT\n"
            "/addchannel -100CHANNEL_ID https://t.me/+INVITE\n"
            "/removechannel -100CHANNEL_ID\n"
            "/channels\n\n"
            "<b>Video settings</b>\n"
            "/setchannels TOKEN\n\n"
            "<b>Owner-only commands</b>\n"
            "/addadmin ID\n/removeadmin ID\n/admins\n"
            "/banuser ID reason\n/unbanuser ID\n/bannedusers",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_keyboard(),
        )


# =========================================================
# PRIVATE CHANNEL REGISTRATION
# =========================================================

@admin_only
async def pending_channels(update, context):
    with db() as con:
        rows = con.execute(
            "SELECT * FROM pending_invites ORDER BY slot"
        ).fetchall()

    text = "🔧 <b>YOUR DEFAULT PRIVATE CHANNELS</b>\n\n"
    for row in rows:
        text += (
            f"<b>Slot {row['slot']}</b>\n"
            f"{html.escape(row['invite_url'])}\n\n"
        )

    text += (
        "Register each channel after adding the bot as administrator:\n\n"
        "<code>/registerchannel -1001234567890 1</code>\n"
        "<code>/registerchannel -1009876543210 2</code>\n\n"
        "Replace the example IDs with the real numeric channel IDs."
    )

    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def save_channel(update, context, chat_id, join_url):
    message = update.effective_message

    if not join_url.startswith("https://t.me/"):
        await message.reply_text("❌ Join link must start with https://t.me/")
        return

    try:
        chat = await context.bot.get_chat(chat_id)
        if chat.type != "channel":
            await message.reply_text("❌ That ID is not a channel.")
            return

        member = await context.bot.get_chat_member(
            chat_id=chat.id,
            user_id=context.bot.id,
        )
        if member.status not in ("administrator", "creator"):
            await message.reply_text(
                "⚠️ Add the bot as an administrator in that channel first."
            )
            return

    except TelegramError:
        await message.reply_text(
            "❌ Cannot access that channel. Verify its numeric ID and "
            "make sure the bot is an administrator."
        )
        return

    with db() as con:
        con.execute("""
            INSERT INTO channels(chat_id, username, title, join_url)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                username=excluded.username,
                title=excluded.title,
                join_url=excluded.join_url
        """, (
            str(chat.id),
            chat.username,
            chat.title or "Private Channel",
            join_url,
        ))

    await message.reply_text(
        "✅ <b>CHANNEL REGISTERED</b>\n\n"
        f"📢 {html.escape(chat.title or 'Private Channel')}\n"
        f"🆔 <code>{chat.id}</code>\n\n"
        "You can now select it in your video settings.",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def register_channel(update, context):
    if (
        len(context.args) != 2
        or not context.args[0].lstrip("-").isdigit()
        or not context.args[1].isdigit()
    ):
        await update.effective_message.reply_text(
            "Usage: /registerchannel -100CHANNEL_ID SLOT\n"
            "Slot is 1 or 2 for your default invite links."
        )
        return

    chat_id = int(context.args[0])
    slot = int(context.args[1])

    with db() as con:
        row = con.execute(
            "SELECT invite_url FROM pending_invites WHERE slot=?",
            (slot,),
        ).fetchone()

    if not row:
        await update.effective_message.reply_text("❌ Invalid slot.")
        return

    await save_channel(update, context, chat_id, row["invite_url"])


@admin_only
async def add_channel(update, context):
    if (
        len(context.args) != 2
        or not context.args[0].lstrip("-").isdigit()
    ):
        await update.effective_message.reply_text(
            "Usage: /addchannel -100CHANNEL_ID https://t.me/+INVITECODE"
        )
        return

    await save_channel(
        update,
        context,
        int(context.args[0]),
        context.args[1],
    )


@admin_only
async def remove_channel(update, context):
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /removechannel -100CHANNEL_ID"
        )
        return

    chat_id = context.args[0]

    with db() as con:
        row = con.execute(
            "SELECT title FROM channels WHERE chat_id=?",
            (chat_id,),
        ).fetchone()

        if not row:
            await update.effective_message.reply_text("❌ Channel not found.")
            return

        con.execute("DELETE FROM video_channels WHERE chat_id=?", (chat_id,))
        con.execute("DELETE FROM channels WHERE chat_id=?", (chat_id,))

    await update.effective_message.reply_text(
        f"✅ Removed <b>{html.escape(row['title'])}</b> from all video "
        "requirements.",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def list_channels(update, context):
    channels = get_channels()

    if not channels:
        await update.effective_message.reply_text(
            "📭 No channels registered. Use /pendingchannels."
        )
        return

    text = "📢 <b>CHANNELS</b>\n━━━━━━━━━━━━━━━━━━\n"
    for ch in channels:
        text += (
            f"\n<b>{html.escape(ch['title'])}</b>\n"
            f"ID: <code>{html.escape(ch['chat_id'])}</code>\n"
            f"{html.escape(ch['join_url'])}\n"
        )

    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


# =========================================================
# VIDEO UPLOAD AND SHARE LINKS
# =========================================================

@admin_only
async def save_video(update, context):
    message = update.effective_message
    user = update.effective_user

    if message.video:
        file_id = message.video.file_id
        file_type = "video"
    elif message.document and message.document.mime_type:
        if not message.document.mime_type.startswith("video/"):
            await message.reply_text("❌ Please send a video file.")
            return
        file_id = message.document.file_id
        file_type = "document"
    else:
        await message.reply_text("❌ Please send a video.")
        return

    token = create_video(file_id, file_type, user.id)
    link = await share_link(context, token)

    await message.reply_text(
        "🎉 <b>VIDEO LINK CREATED!</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🔑 Token: <code>{html.escape(token)}</code>\n\n"
        "🔗 <b>Share link:</b>\n"
        f'<a href="{html.escape(link, quote=True)}">Open Video</a>\n\n'
        "📢 Select which channels users must join, then tap Save Settings.",
        parse_mode=ParseMode.HTML,
        reply_markup=config_keyboard(
            token, get_channels(), get_selected_ids(token)
        ),
        disable_web_page_preview=True,
    )


async def show_video_config(update, context, token):
    link = await share_link(context, token)
    await update.effective_message.reply_text(
        "🛠 <b>VIDEO ACCESS SETTINGS</b>\n\n"
        f"Token: <code>{html.escape(token)}</code>\n"
        f'<a href="{html.escape(link, quote=True)}">Share Link</a>\n\n'
        "Select required channels and save.",
        parse_mode=ParseMode.HTML,
        reply_markup=config_keyboard(
            token, get_channels(), get_selected_ids(token)
        ),
        disable_web_page_preview=True,
    )


@admin_only
async def set_channels_command(update, context):
    if not context.args:
        await update.effective_message.reply_text("Usage: /setchannels TOKEN")
        return

    token = context.args[0].removeprefix("v_")
    if not get_video(token):
        await update.effective_message.reply_text("❌ Video not found.")
        return

    await show_video_config(update, context, token)


async def config_callback(update, context):
    q = update.callback_query
    uid = q.from_user.id

    if is_banned(uid):
        await q.answer("Access restricted.", show_alert=True)
        return

    if not is_admin(uid):
        await q.answer("Admins only.", show_alert=True)
        return

    parts = q.data.split(":", 3)
    if len(parts) < 3:
        await q.answer("Invalid action.", show_alert=True)
        return

    token, action = parts[1], parts[2]

    if not get_video(token):
        await q.answer("Video not found.", show_alert=True)
        return

    if action == "toggle":
        if len(parts) != 4:
            await q.answer("Invalid channel.", show_alert=True)
            return

        cid = parts[3]

        with db() as con:
            if not con.execute(
                "SELECT 1 FROM channels WHERE chat_id=?",
                (cid,),
            ).fetchone():
                await q.answer("Channel not found.", show_alert=True)
                return

            selected = con.execute(
                "SELECT 1 FROM video_channels WHERE token=? AND chat_id=?",
                (token, cid),
            ).fetchone()

            if selected:
                con.execute(
                    "DELETE FROM video_channels WHERE token=? AND chat_id=?",
                    (token, cid),
                )
            else:
                con.execute(
                    "INSERT OR IGNORE INTO video_channels(token, chat_id) VALUES (?, ?)",
                    (token, cid),
                )

        await q.answer("Selection updated.")

    elif action == "all":
        with db() as con:
            con.execute("DELETE FROM video_channels WHERE token=?", (token,))
            con.execute("""
                INSERT OR IGNORE INTO video_channels(token, chat_id)
                SELECT ?, chat_id FROM channels
            """, (token,))
        await q.answer("All channels selected.")

    elif action == "none":
        with db() as con:
            con.execute("DELETE FROM video_channels WHERE token=?", (token,))
        await q.answer("All channels deselected.")

    elif action == "save":
        await q.answer("Settings saved.")
        link = await share_link(context, token)
        selected = get_video_channels(token)

        text = (
            "✅ <b>SETTINGS SAVED</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🔑 Token: <code>{html.escape(token)}</code>\n"
            f'<a href="{html.escape(link, quote=True)}">Share Video Link</a>\n\n'
            "<b>Required channels:</b>\n"
        )

        if selected:
            text += "".join(f"• {html.escape(ch['title'])}\n" for ch in selected)
        else:
            text += "• None\n"

        try:
            await q.edit_message_text(
                text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except BadRequest:
            pass
        return

    else:
        await q.answer("Unknown action.", show_alert=True)
        return

    link = await share_link(context, token)
    try:
        await q.edit_message_text(
            "🛠 <b>VIDEO ACCESS SETTINGS</b>\n\n"
            f"Token: <code>{html.escape(token)}</code>\n"
            f'<a href="{html.escape(link, quote=True)}">Share Link</a>\n\n'
            "Select required channels and save.",
            parse_mode=ParseMode.HTML,
            reply_markup=config_keyboard(
                token, get_channels(), get_selected_ids(token)
            ),
            disable_web_page_preview=True,
        )
    except BadRequest:
        pass


# =========================================================
# FORCE-JOIN VERIFICATION
# =========================================================

async def find_missing_channels(context, user_id, token):
    missing = []

    for ch in get_video_channels(token):
        try:
            member = await context.bot.get_chat_member(
                chat_id=int(ch["chat_id"]),
                user_id=user_id,
            )

            if member.status not in ("member", "administrator", "creator"):
                missing.append(ch)

        except TelegramError:
            logger.exception("Membership verification failed")
            return None

    return missing


async def check_membership(update, context):
    q = update.callback_query
    uid = q.from_user.id

    if is_banned(uid):
        await q.answer("Access restricted.", show_alert=True)
        return

    await q.answer("Checking membership…")
    await deliver_video(context, uid, q.data.split(":", 1)[1])


# =========================================================
# DELIVERY / COOLDOWN / DELETION
# =========================================================

async def delete_message_job(context):
    chat_id, message_id = context.job.data
    try:
        await context.bot.delete_message(
            chat_id=chat_id,
            message_id=message_id,
        )
    except TelegramError:
        logger.info("Temporary message could not be deleted.")


async def deliver_video(context, user_id, token):
    if is_banned(user_id):
        await context.bot.send_message(
            chat_id=user_id,
            text="🚫 Your access to this bot is restricted.",
        )
        return

    video = get_video(token)
    if not video:
        await context.bot.send_message(
            chat_id=user_id,
            text="❌ Invalid or unavailable video link.",
        )
        return

    missing = await find_missing_channels(context, user_id, token)

    if missing is None:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "⚠️ I couldn't verify channel membership. Try again later. "
                "The bot must be an administrator in the required channels."
            ),
        )
        return

    if missing:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "🔒 <b>JOIN REQUIRED CHANNELS</b>\n\n"
                "Join the channels below, then press Verify."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=join_keyboard(missing, token),
        )
        return

    now = time.time()

    with db() as con:
        con.execute(
            """
            INSERT OR IGNORE INTO video_usage
            (token, user_id, deliveries, cooldown_until)
            VALUES (?, ?, 0, 0)
            """,
            (token, user_id),
        )

        usage = con.execute(
            "SELECT * FROM video_usage WHERE token=? AND user_id=?",
            (token, user_id),
        ).fetchone()

        if usage["cooldown_until"] > now:
            remaining = int(usage["cooldown_until"] - now)
            hours, rem = divmod(remaining, 3600)
            minutes = rem // 60

            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "🕕 <b>COOLDOWN ACTIVE</b>\n\n"
                    f"You have received this video {MAX_DELIVERIES} times.\n"
                    f"Try again in about {hours}h {minutes}m."
                ),
                parse_mode=ParseMode.HTML,
            )
            return

        if usage["cooldown_until"] and usage["cooldown_until"] <= now:
            con.execute(
                """
                UPDATE video_usage
                SET deliveries=0, cooldown_until=0
                WHERE token=? AND user_id=?
                """,
                (token, user_id),
            )

    caption = (
        "⚠️ <b>IMPORTANT!</b>\n\n"
        "Share this video to your Saved Messages before it gets "
        f"deleted in {DELETE_AFTER} seconds!"
    )

    try:
        if video["file_type"] == "video":
            sent = await context.bot.send_video(
                chat_id=user_id,
                video=video["file_id"],
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
        else:
            sent = await context.bot.send_document(
                chat_id=user_id,
                document=video["file_id"],
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
    except TelegramError:
        logger.exception("Video delivery failed")
        await context.bot.send_message(
            chat_id=user_id,
            text="❌ Video delivery failed. Please try again later.",
        )
        return

    with db() as con:
        con.execute(
            """
            UPDATE video_usage SET deliveries=deliveries+1
            WHERE token=? AND user_id=?
            """,
            (token, user_id),
        )

        usage = con.execute(
            "SELECT deliveries FROM video_usage WHERE token=? AND user_id=?",
            (token, user_id),
        ).fetchone()

        if usage["deliveries"] >= MAX_DELIVERIES:
            con.execute(
                """
                UPDATE video_usage SET cooldown_until=?
                WHERE token=? AND user_id=?
                """,
                (time.time() + COOLDOWN_SECONDS, token, user_id),
            )

    if context.job_queue:
        context.job_queue.run_once(
            delete_message_job,
            when=DELETE_AFTER,
            data=(sent.chat_id, sent.message_id),
        )
    else:
        logger.error("JobQueue unavailable. Install python-telegram-bot[job-queue].")


# =========================================================
# OWNER-ONLY ADMIN MANAGEMENT
# =========================================================

@owner_only
async def add_admin(update, context):
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /addadmin USER_ID")
        return

    uid = int(context.args[0])
    with db() as con:
        con.execute("INSERT OR IGNORE INTO admins(user_id) VALUES (?)", (uid,))

    await update.effective_message.reply_text(
        f"✅ <code>{uid}</code> added as an administrator.",
        parse_mode=ParseMode.HTML,
    )


@owner_only
async def remove_admin(update, context):
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /removeadmin USER_ID")
        return

    uid = int(context.args[0])
    if uid == OWNER_ID:
        await update.effective_message.reply_text("❌ You cannot remove the owner.")
        return

    with db() as con:
        cur = con.execute("DELETE FROM admins WHERE user_id=?", (uid,))

    await update.effective_message.reply_text(
        "✅ Administrator removed." if cur.rowcount else "ℹ️ Admin not found."
    )


@owner_only
async def list_admins(update, context):
    with db() as con:
        rows = con.execute("SELECT user_id FROM admins ORDER BY user_id").fetchall()

    text = "👑 <b>ADMINISTRATORS</b>\n\n"
    for row in rows:
        uid = row["user_id"]
        suffix = " (Owner)" if uid == OWNER_ID else ""
        text += f"• <code>{uid}</code>{suffix}\n"

    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


# =========================================================
# OWNER-ONLY BANS
# =========================================================

@owner_only
async def ban_user(update, context):
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text(
            "Usage: /banuser USER_ID reason"
        )
        return

    uid = int(context.args[0])
    if uid == OWNER_ID:
        await update.effective_message.reply_text("❌ You cannot ban the owner.")
        return

    reason = " ".join(context.args[1:]).strip() or "No reason provided"

    with db() as con:
        con.execute(
            """
            INSERT INTO banned_users(user_id, reason, banned_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                reason=excluded.reason, banned_at=excluded.banned_at
            """,
            (uid, reason, time.time()),
        )

    await update.effective_message.reply_text(
        "🚫 <b>USER BANNED</b>\n\n"
        f"ID: <code>{uid}</code>\n"
        f"Reason: {html.escape(reason)}",
        parse_mode=ParseMode.HTML,
    )


@owner_only
async def unban_user(update, context):
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /unbanuser USER_ID")
        return

    uid = int(context.args[0])
    with db() as con:
        cur = con.execute("DELETE FROM banned_users WHERE user_id=?", (uid,))

    await update.effective_message.reply_text(
        "✅ User unbanned." if cur.rowcount else "ℹ️ User was not banned."
    )


@owner_only
async def banned_users(update, context):
    with db() as con:
        rows = con.execute(
            "SELECT * FROM banned_users ORDER BY banned_at DESC"
        ).fetchall()

    if not rows:
        await update.effective_message.reply_text("✅ No banned users.")
        return

    text = "🚫 <b>BANNED USERS</b>\n\n"
    for row in rows:
        text += (
            f"🆔 <code>{row['user_id']}</code>\n"
            f"📝 {html.escape(row['reason'])}\n\n"
        )

    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


# =========================================================
# ERRORS / MAIN
# =========================================================

async def error_handler(update, context):
    logger.error("An update caused an exception", exc_info=context.error)


def main():
    setup_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))

    app.add_handler(CommandHandler("pendingchannels", pending_channels))
    app.add_handler(CommandHandler("registerchannel", register_channel))
    app.add_handler(CommandHandler("addchannel", add_channel))
    app.add_handler(CommandHandler("removechannel", remove_channel))
    app.add_handler(CommandHandler("channels", list_channels))
    app.add_handler(CommandHandler("setchannels", set_channels_command))

    app.add_handler(CommandHandler("addadmin", add_admin))
    app.add_handler(CommandHandler("removeadmin", remove_admin))
    app.add_handler(CommandHandler("admins", list_admins))

    app.add_handler(CommandHandler("banuser", ban_user))
    app.add_handler(CommandHandler("unbanuser", unban_user))
    app.add_handler(CommandHandler("bannedusers", banned_users))

    app.add_handler(CallbackQueryHandler(config_callback, pattern=r"^cfg:"))
    app.add_handler(CallbackQueryHandler(check_membership, pattern=r"^check:"))
    app.add_handler(CallbackQueryHandler(ui_callback, pattern=r"^ui:"))

    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & (filters.VIDEO | filters.Document.VIDEO),
            save_video,
        )
    )

    app.add_error_handler(error_handler)

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()