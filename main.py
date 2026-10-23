import os
import re
import json
import logging
import asyncio
import shutil
import tempfile
import threading
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor

import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================================================
# Configuration
# ============================================================
# On hosting, set BOT_TOKEN in the environment variables.
# For local Python use, you may optionally paste the token below.
# Environment variable always takes priority.
BOT_TOKEN_FALLBACK = ""
TOKEN = os.getenv("BOT_TOKEN", "").strip() or BOT_TOKEN_FALLBACK.strip()

OWNER_ID = 7199778669

BASE_PATH = Path(__file__).resolve().parent
DOWNLOAD_PATH = BASE_PATH / "downloads"
DATA_PATH = BASE_PATH / "users_data.json"
BACKUP_PATH = BASE_PATH / "users_data_backup.json"

# There is no application-level semaphore/limit for download requests.
# The default executor queues work safely instead of creating unlimited OS threads.
DOWNLOAD_EXECUTOR = ThreadPoolExecutor(thread_name_prefix="media-download")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
for logger_name in ("httpx", "telegram", "telegram.ext", "yt_dlp"):
    logging.getLogger(logger_name).setLevel(logging.WARNING)

DOWNLOAD_PATH.mkdir(parents=True, exist_ok=True)

user_states = {}
active_downloads = {}
user_data = {}
data_lock = threading.RLock()
write_lock = threading.Lock()

INLINE_USER_DATA = {
    # Optional initial record. New users are added automatically.
}


# ============================================================
# Persistent user data
# ============================================================
def save_data():
    """Write JSON atomically; serialize writes to avoid conflicting temp files."""
    with write_lock:
        try:
            with data_lock:
                snapshot = json.loads(json.dumps(user_data, ensure_ascii=False))
            temp_path = DATA_PATH.with_suffix(".json.tmp")
            with open(temp_path, "w", encoding="utf-8") as file:
                json.dump(snapshot, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, DATA_PATH)
        except (OSError, TypeError, ValueError):
            logging.exception("Failed to save user data")


def load_data():
    global user_data
    try:
        if DATA_PATH.exists():
            with open(DATA_PATH, "r", encoding="utf-8") as file:
                loaded = json.load(file)
            user_data = loaded if isinstance(loaded, dict) else {}
        else:
            user_data = dict(INLINE_USER_DATA)
            save_data()
    except (OSError, json.JSONDecodeError):
        logging.exception("Failed to load user data; starting with available defaults")
        user_data = dict(INLINE_USER_DATA)


def add_user(user_id, username, first_name):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    key = str(user_id)
    with data_lock:
        record = user_data.get(key)
        if not isinstance(record, dict):
            user_data[key] = {
                "username": username or "",
                "first_name": first_name or "",
                "join_date": now,
                "last_active": now,
                "download_count": 0,
                "is_active": True,
            }
        else:
            record["username"] = username or record.get("username", "")
            record["first_name"] = first_name or record.get("first_name", "")
            record["last_active"] = now
            record["is_active"] = True
    save_data()


def update_downloads(user_id):
    key = str(user_id)
    with data_lock:
        if key in user_data:
            user_data[key]["download_count"] = int(
                user_data[key].get("download_count", 0)
            ) + 1
    save_data()


def get_stats():
    now = datetime.now()
    with data_lock:
        records = list(user_data.values())
        total_users = len(records)
        total_downloads = sum(
            int(item.get("download_count", 0))
            for item in records
            if isinstance(item, dict)
        )

    active_users = 0
    for item in records:
        try:
            last_active = datetime.strptime(
                item.get("last_active", ""), "%Y-%m-%d %H:%M:%S"
            )
            if (now - last_active).days <= 30:
                active_users += 1
        except (ValueError, TypeError, AttributeError):
            pass

    return {
        "total_users": total_users,
        "active_users": active_users,
        "inactive_users": total_users - active_users,
        "total_downloads": total_downloads,
        "active_downloads": len(active_downloads),
    }


# ============================================================
# URL validation
# ============================================================
ALLOWED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
    "youtube-nocookie.com", "www.youtube-nocookie.com",
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com",
    "instagram.com", "www.instagram.com", "m.instagram.com",
    "facebook.com", "www.facebook.com", "m.facebook.com", "fb.watch",
    "twitter.com", "www.twitter.com", "x.com", "www.x.com",
}


def validate_media_url(url):
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            return False
        host = parsed.hostname.lower().rstrip(".")
        # Exact host or a genuine subdomain of an allowed domain.
        return any(host == domain or host.endswith("." + domain)
                   for domain in ALLOWED_HOSTS)
    except (ValueError, AttributeError):
        return False


def get_platform(url):
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if host == "youtu.be" or host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        return "YouTube"
    if host.endswith("tiktok.com"):
        return "TikTok"
    if host.endswith("instagram.com"):
        return "Instagram"
    if host == "fb.watch" or host.endswith("facebook.com"):
        return "Facebook"
    if host.endswith("twitter.com") or host.endswith("x.com"):
        return "Twitter/X"
    return "غير معروف"


# ============================================================
# Media downloading (runs in a worker thread)
# ============================================================
def download_media(url, media_type, job_id):
    """Download video or extract MP3 audio. Returns (path, platform)."""
    platform = get_platform(url)
    DOWNLOAD_PATH.mkdir(parents=True, exist_ok=True)
    output_template = str(DOWNLOAD_PATH / f"{job_id}_%(title).80s_%(id)s.%(ext)s")

    common_opts = {
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        "retries": 5,
        "fragment_retries": 5,
        "skip_unavailable_fragments": True,
        "socket_timeout": 30,
        "overwrites": False,
        "restrictfilenames": True,
    }

    opts = {
        **common_opts,
        "format": "best[ext=mp4][height<=1080]/best[height<=1080]/best",
        "merge_output_format": "mp4",
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                raise RuntimeError("لم يتم العثور على محتوى قابل للتحميل.")

            # prepare_filename may return the pre-postprocessing extension.
            expected = Path(ydl.prepare_filename(info))
            candidates = [
                expected.with_suffix(".mp4"),
                expected.with_suffix(".mkv"),
                expected.with_suffix(".webm"),
                expected.with_suffix(".mov"),
                expected,
            ]

            for candidate in candidates:
                if candidate.is_file():
                    return str(candidate), platform

            # Fallback: identify files created by this unique job id.
            matches = [
                p for p in DOWNLOAD_PATH.glob(f"{job_id}_*")
                if p.is_file() and p.suffix.lower() in {
                    ".mp4", ".mkv", ".webm", ".mov"
                }
            ]
            if matches:
                newest = max(matches, key=lambda p: p.stat().st_mtime)
                return str(newest), platform

            raise RuntimeError("اكتمل الطلب لكن لم يتم العثور على الملف الناتج.")
    except Exception as exc:
        raise RuntimeError(f"تعذر التحميل: {exc}") from exc


# ============================================================
# Telegram helpers and menus
# ============================================================
async def telegram_call_with_retry(call, *args, **kwargs):
    for attempt in range(5):
        try:
            return await call(*args, **kwargs)
        except RetryAfter as exc:
            if attempt == 4:
                raise
            delay = getattr(exc, "retry_after", 1)
            if hasattr(delay, "total_seconds"):
                delay = delay.total_seconds()
            await asyncio.sleep(float(delay) + 0.5)


def main_menu_text():
    stats = get_stats()
    return (
        "🎬 بوت تحميل الفيديو\n"
        "📥 اضغط (تحميل فيديو) وأرسل الرابط.\n"
        f"📊 عدد التحميلات الناجحة: {stats['total_downloads']}\n"
        "YouTube • TikTok • Instagram • Facebook • X"
    )


def main_menu_keyboard(user_id=None):
    keyboard = [
        [InlineKeyboardButton("📥 تحميل فيديو", callback_data="download_video")]
    ]
    if user_id == OWNER_ID:
        keyboard.extend([
            [
                InlineKeyboardButton("📊 الإحصائيات", callback_data="stats"),
                InlineKeyboardButton("📢 إذاعة للجميع", callback_data="broadcast"),
            ],
            [InlineKeyboardButton("🗂️ نسخ احتياطي", callback_data="backup")],
        ])
    return InlineKeyboardMarkup(keyboard)


def back_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 رجوع للقائمة الرئيسية", callback_data="back")]
    ])


async def safe_edit_or_reply(query, text, reply_markup=None):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except TelegramError:
        await query.message.reply_text(text, reply_markup=reply_markup)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    add_user(user.id, user.username, user.first_name)
    user_states.pop(user.id, None)
    await update.effective_message.reply_text(
        main_menu_text(), reply_markup=main_menu_keyboard(user.id)
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "download_video":
        user_states[user_id] = {"state": "waiting_for_link", "media_type": "video"}
        await safe_edit_or_reply(
            query,
            "📥 أرسل رابط الفيديو.",
            back_menu_keyboard(),
        )
        return

    if data == "stats":
        if user_id != OWNER_ID:
            await safe_edit_or_reply(query, "⛔ هذه الخاصية للمالك فقط.", back_menu_keyboard())
            return
        stats = get_stats()
        text = (
            "📊 الإحصائيات\n\n"
            f"👥 مجموع المستخدمين: {stats['total_users']}\n"
            f"✅ نشطون خلال آخر 30 يوم: {stats['active_users']}\n"
            f"💤 غير نشطين: {stats['inactive_users']}\n"
            f"📥 عمليات التحميل المكتملة: {stats['total_downloads']}\n"
            f"🔄 طلبات قيد التنفيذ أو الانتظار: {stats['active_downloads']}"
        )
        await safe_edit_or_reply(query, text, back_menu_keyboard())
        return

    if data == "broadcast":
        if user_id != OWNER_ID:
            await safe_edit_or_reply(query, "⛔ هذه الخاصية للمالك فقط.", back_menu_keyboard())
            return
        user_states[user_id] = {"state": "waiting_for_broadcast"}
        await safe_edit_or_reply(
            query, "📢 أرسل نص الإذاعة الذي تريد إرساله للمستخدمين.", back_menu_keyboard()
        )
        return

    if data == "backup":
        if user_id != OWNER_ID:
            await safe_edit_or_reply(query, "⛔ هذه الخاصية للمالك فقط.", back_menu_keyboard())
            return
        try:
            save_data()
            if not DATA_PATH.exists():
                raise FileNotFoundError("ملف البيانات غير موجود")
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_file = BASE_PATH / f"users_data_backup_{timestamp}.json"
            shutil.copy2(DATA_PATH, backup_file)
            with open(backup_file, "rb") as file:
                await telegram_call_with_retry(
                    context.bot.send_document,
                    chat_id=user_id,
                    document=file,
                    filename=backup_file.name,
                    caption="🗂️ نسخة احتياطية من بيانات المستخدمين.",
                )
            backup_file.unlink(missing_ok=True)
            await safe_edit_or_reply(query, "✅ تم إرسال النسخة الاحتياطية إليك.", main_menu_keyboard(user_id))
        except Exception:
            logging.exception("Backup failed")
            await safe_edit_or_reply(query, "❌ فشل إنشاء النسخة الاحتياطية.", main_menu_keyboard(user_id))
        return

    if data == "back":
        user_states.pop(user_id, None)
        await safe_edit_or_reply(query, main_menu_text(), main_menu_keyboard(user_id))


# ============================================================
# Download workflow
# ============================================================
async def download_and_send(update, context, url, media_type, user, waiting_msg, job_id):
    filepath = None
    active_downloads[job_id] = {
        "user_id": user.id,
        "media_type": media_type,
        "started_at": datetime.now().isoformat(),
    }
    try:
        loop = asyncio.get_running_loop()
        filepath, platform = await loop.run_in_executor(
            DOWNLOAD_EXECUTOR, download_media, url, media_type, job_id
        )
        path = Path(filepath)
        if not path.is_file():
            raise RuntimeError("ملف التحميل غير موجود.")

        # Telegram bot API has upload-size limits that depend on the API/server setup.
        # Catch oversized-file errors below and tell the user to try a smaller version.
        try:
            await waiting_msg.delete()
        except TelegramError:
            pass

        with open(path, "rb") as media_file:
            await telegram_call_with_retry(
                update.effective_message.reply_video,
                video=media_file,
                caption="✅ تم تحميل الفيديو بنجاح",
                supports_streaming=True,
            )

        update_downloads(user.id)
        user_states.pop(user.id, None)

        if user.id != OWNER_ID:
            owner_info = (
                "📥 طلب تحميل مكتمل\n\n"
                f"الاسم: {user.first_name or 'غير متوفر'}\n"
                f"اسم المستخدم: @{user.username or 'لا يوجد'}\n"
                "النوع: فيديو\n"
                f"المنصة: {platform}\n"
                f"التاريخ: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"معرّف المستخدم: {user.id}"
            )
            try:
                with open(path, "rb") as owner_video:
                    await telegram_call_with_retry(
                        context.bot.send_video,
                        chat_id=OWNER_ID,
                        video=owner_video,
                        caption=f"🎬 فيديو حمّله المستخدم: {user.first_name or 'غير متوفر'}",
                        supports_streaming=True,
                    )
                await telegram_call_with_retry(
                    context.bot.send_message, chat_id=OWNER_ID, text=owner_info
                )
            except TelegramError:
                logging.warning("Could not send downloaded video or notification to owner")

        await update.effective_message.reply_text(
            main_menu_text(), reply_markup=main_menu_keyboard(user.id)
        )

    except Exception as exc:
        logging.exception("Media job failed")
        message = (
            "❌ ما كدرت أكمل الطلب.\n"
            "تأكد أن الرابط صحيح وعام، وحدّث yt-dlp إذا استمرت المشكلة. "
            "قد يكون الملف كبيراً جداً للإرسال أو قد يتطلب الموقع طريقة تحقق إضافية."
        )
        try:
            await waiting_msg.edit_text(message, reply_markup=back_menu_keyboard())
        except TelegramError:
            try:
                await update.effective_message.reply_text(message, reply_markup=back_menu_keyboard())
            except TelegramError:
                pass
    finally:
        active_downloads.pop(job_id, None)
        if filepath:
            try:
                Path(filepath).unlink(missing_ok=True)
            except OSError:
                logging.exception("Could not delete downloaded file")
        # Remove any leftover files for this unique job.
        for leftover in DOWNLOAD_PATH.glob(f"{job_id}_*"):
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                pass


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if not message or not message.text or not user:
        return

    user_id = user.id
    text = message.text.strip()
    add_user(user_id, user.username, user.first_name)
    state_data = user_states.get(user_id, {})
    state = state_data.get("state") if isinstance(state_data, dict) else state_data

    if state == "waiting_for_link":
        if not validate_media_url(text):
            await message.reply_text(
                "❌ الرابط غير صحيح أو الموقع غير مدعوم. أرسل رابطاً كاملاً من "
                "YouTube أو TikTok أو Instagram أو Facebook أو X.",
                reply_markup=back_menu_keyboard(),
            )
            return

        media_type = state_data.get("media_type", "video")
        job_id = f"{user_id}_{int(datetime.now().timestamp() * 1000)}"
        waiting_msg = await message.reply_text("⏳ جاري تحميل الفيديو...")
        # No fixed semaphore or explicit application-level concurrent-download cap.
        # The executor queues work as worker capacity becomes available.
        asyncio.create_task(
            download_and_send(update, context, text, media_type, user, waiting_msg, job_id)
        )
        return

    if state == "waiting_for_broadcast":
        if user_id != OWNER_ID:
            user_states.pop(user_id, None)
            await message.reply_text("⛔ غير مصرح لك.")
            return

        broadcast_text = text
        user_states.pop(user_id, None)
        sending_msg = await message.reply_text("⏳ جاري إرسال الإذاعة...")
        with data_lock:
            recipients = list(user_data.keys())

        sent_count = 0
        failed_count = 0
        for uid in recipients:
            try:
                await telegram_call_with_retry(
                    context.bot.send_message,
                    chat_id=int(uid),
                    text=f"📢 إذاعة من إدارة البوت:\n\n{broadcast_text}",
                )
                sent_count += 1
                await asyncio.sleep(0.05)
            except Exception as exc:
                failed_count += 1
                logging.warning("Broadcast failed for user %s: %s", uid, exc)

        try:
            await sending_msg.edit_text(
                f"✅ انتهت الإذاعة.\n📨 تم الإرسال: {sent_count}\n⚠️ تعذّر الإرسال: {failed_count}",
                reply_markup=main_menu_keyboard(user_id),
            )
        except TelegramError:
            pass
        return

    await message.reply_text(main_menu_text(), reply_markup=main_menu_keyboard(user_id))


async def post_shutdown(application):
    DOWNLOAD_EXECUTOR.shutdown(wait=False, cancel_futures=True)


def main():
    if not TOKEN:
        raise RuntimeError(
            "BOT_TOKEN غير مضبوط. أضف متغير البيئة BOT_TOKEN على الاستضافة، "
            "أو ضع التوكن الجديد في BOT_TOKEN_FALLBACK للتشغيل المحلي."
        )

    load_data()
    DOWNLOAD_PATH.mkdir(parents=True, exist_ok=True)
    logging.info("Bot starting; registered users: %s", len(user_data))

    application = (
        Application.builder()
        .token(TOKEN)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
