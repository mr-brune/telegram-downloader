import asyncio
import logging
import math
import os
import platform
import shutil
import traceback

from telegram import Update
from telegram.ext import ContextTypes, filters

from ..middlewares.auth import auth_required
from ..middlewares.handlers import (
    command_handler,
    message_handler,
)
from ..models import DownloadFile, downloading_files
from ..utils import check_file_exists, env, get_file

logger = logging.getLogger(__name__)

# Environment variables
BOT_TOKEN = env.BOT_TOKEN
BOT_API_DIR = env.BOT_API_DIR
DOWNLOAD_TO_DIR = env.DOWNLOAD_TO_DIR

# Replacing colons with a different character for Windows
TOKEN_SUB_DIR = BOT_TOKEN.replace(":", "") if os.name == "nt" else BOT_TOKEN

_download_lock = asyncio.Lock()

_PROGRESS_BAR_LEN = 20
_POLL_INTERVAL = 2
_STORAGE_SUBDIRS = ["temp", "videos", "documents", "audio", "photos", "video_notes", "voice"]


def _format_size(size_bytes: int) -> str:
    if size_bytes >= 1024 ** 3:
        return f"{size_bytes / (1024 ** 3):.1f} GB"
    if size_bytes >= 1024 ** 2:
        return f"{size_bytes / (1024 ** 2):.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def _progress_bar(progress: float) -> str:
    filled = round(_PROGRESS_BAR_LEN * progress)
    return "█" * filled + "░" * (_PROGRESS_BAR_LEN - filled)


def _get_media_info(message) -> tuple[str, str, int]:
    if message.document:
        return message.document.file_id, message.document.file_name, getattr(message.document, "file_size", 0)
    elif message.video:
        media = message.video
        file_name = getattr(media, "file_name", None) or f"video_{media.file_id}.mp4"
        return media.file_id, file_name, getattr(media, "file_size", 0)
    elif message.photo:
        media = message.photo[-1]
        return media.file_id, f"photo_{media.file_id}.jpg", getattr(media, "file_size", 0)
    return None, None, 0


def _snapshot_files(token_dir: str) -> set[str]:
    result = set()
    for subdir in _STORAGE_SUBDIRS:
        path = os.path.join(token_dir, subdir)
        if os.path.isdir(path):
            for fname in os.listdir(path):
                result.add(os.path.join(path, fname))
    return result


def _find_new_file(token_dir: str, known_files: set[str]) -> str | None:
    for subdir in _STORAGE_SUBDIRS:
        path = os.path.join(token_dir, subdir)
        if not os.path.isdir(path):
            continue
        for fname in os.listdir(path):
            fpath = os.path.join(path, fname)
            if fpath not in known_files:
                return fpath
    return None


async def _monitor_file_growth(status_msg, total_size: int, get_path_fn, label: str) -> None:
    """Poll a file path and edit status_msg with a progress bar until cancelled."""
    last_size = 0

    while True:
        await asyncio.sleep(_POLL_INTERVAL)

        file_path = get_path_fn()
        if file_path is None:
            continue

        try:
            current_size = os.path.getsize(file_path)
        except OSError:
            continue

        progress = min(current_size / total_size, 1.0) if total_size > 0 else 0.0
        bar = _progress_bar(progress)
        speed = max(0, (current_size - last_size) / _POLL_INTERVAL)
        last_size = current_size
        speed_str = f"{_format_size(int(speed))}/s" if speed > 0 else "…"

        try:
            await status_msg.edit_text(
                f"{label}\n"
                f"`{bar}` `{progress * 100:.0f}%`\n"
                f"💾 `{_format_size(current_size)} / {_format_size(total_size)}`\n"
                f"⚡ `{speed_str}`",
                parse_mode="MarkdownV2",
            )
        except Exception:
            pass


def _copy_file(src: str, dst: str) -> None:
    shutil.copy2(src, dst)
    os.unlink(src)


async def _perform_download(bot, message, download_file: DownloadFile) -> None:
    file_id = download_file.file_id
    total_size = download_file.file_size
    token_dir = os.path.join(BOT_API_DIR, TOKEN_SUB_DIR)

    try:
        status_msg = await message.reply_text(
            f"⬇️ Downloading from Telegram\\.\\.\\.\n"
            f"`{'░' * _PROGRESS_BAR_LEN}` `0%`\n"
            f"💾 `0 B / {_format_size(total_size)}`",
            parse_mode="MarkdownV2",
        )
    except Exception as e:
        logger.warning(f"Failed to send status message: {e}")
        status_msg = None

    async def _edit(text: str):
        try:
            if status_msg:
                await status_msg.edit_text(text, parse_mode="MarkdownV2")
            else:
                await message.reply_text(text, parse_mode="MarkdownV2")
        except Exception:
            try:
                await message.reply_text(text, parse_mode="MarkdownV2")
            except Exception:
                pass

    # Phase 1 Monitoring: Download to the local Bot API server
    known_files = _snapshot_files(token_dir)
    found_file: list[str | None] = [None]

    def _get_temp_path() -> str | None:
        if found_file[0]:
            return found_file[0]
        f = _find_new_file(token_dir, known_files)
        if f:
            found_file[0] = f
        return found_file[0]

    monitor = asyncio.create_task(
        _monitor_file_growth(
            status_msg, total_size, _get_temp_path,
            "⬇️ Downloading from Telegram\\.\\.\\."
        )
    ) if status_msg else None

    try:
        new_file = await get_file(bot, download_file)
    except Exception as e:
        if monitor:
            monitor.cancel()
        logger.error(f"Error getting file: {e}")
        traceback.print_exc()
        if file_id in downloading_files:
            downloading_files.pop(file_id)
        await _edit(
            f"⛔ Error during download\n"
            f"> 📄 *File name:*   `{download_file.file_name}`\n"
            f"```\n{e}```"
        )
        return
    else:
        download_file.download_complete()
    finally:
        if monitor:
            monitor.cancel()
            try:
                await monitor
            except asyncio.CancelledError:
                pass

    current_file_path = os.path.join(BOT_API_DIR, TOKEN_SUB_DIR, new_file.file_path)
    dst_path = f"{DOWNLOAD_TO_DIR}{download_file.file_name}"
    os.makedirs(DOWNLOAD_TO_DIR, exist_ok=True)

    # Try atomic rename if on the same disk
    try:
        os.rename(current_file_path, dst_path)
        download_file.move_complete()
        if file_id in downloading_files:
            downloading_files.pop(file_id)
        
        if platform.system() == "Linux":
            try:
                os.chown(dst_path, 1000, 1000)
            except Exception:
                pass

        await _edit(
            f"✅ Download complete\\.\n\n"
            f"> 📄 *File name:*   `{download_file.file_name}`\n"
            f"> 💾 *File size:*   `{download_file.file_size_mb}`\n"
            f"> ⏱ *Total:*   `{download_file.total_duration}`"
        )
        return
    except OSError:
        pass  # Cross-filesystem rename error (will start copy)

    # Phase 2 Monitoring: Copy to a different disk/filesystem
    await _edit(
        f"📦 Copying\\.\\.\\.\n"
        f"`{'░' * _PROGRESS_BAR_LEN}` `0%`\n"
        f"💾 `0 B / {_format_size(total_size)}`"
    )

    copy_task = asyncio.create_task(asyncio.to_thread(_copy_file, current_file_path, dst_path))

    copy_monitor = asyncio.create_task(
        _monitor_file_growth(
            status_msg, total_size, lambda: dst_path,
            "📦 Copying\\.\\.\\."
        )
    ) if status_msg else None

    try:
        await copy_task
    except Exception as copy_error:
        logger.error(f"Error copying file: {copy_error}")
        if file_id in downloading_files:
            downloading_files.pop(file_id)
        await _edit(
            f"⛔ File copy error\n"
            f"```\n{copy_error}```"
        )
        return
    finally:
        if copy_monitor:
            copy_monitor.cancel()
            try:
                await copy_monitor
            except asyncio.CancelledError:
                pass

    download_file.move_complete()
    if file_id in downloading_files:
        downloading_files.pop(file_id)

    if platform.system() == "Linux":
        try:
            os.chown(dst_path, 1000, 1000)
        except Exception:
            pass

    await _edit(
        f"✅ Download complete\\.\n\n"
        f"> 📄 *File name:*   `{download_file.file_name}`\n"
        f"> 💾 *File size:*   `{download_file.file_size_mb}`\n"
        f"> ⏱ *Download:*   `{download_file.download_duration}`\n"
        f"> ⏱ *Total:*   `{download_file.total_duration}`"
    )


@command_handler("status")
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send downloading files status to the user."""
    if not downloading_files:
        await update.message.reply_text("No files are being downloaded at the moment.")
        return

    status_message = "*Downloading files status:*\nPage 1\n"

    for i, file in enumerate(downloading_files.values(), start=1):
        file_status = (
            f"> 📄 *File name:*   `{file.file_name}`\n"
            f"> 💾 *File size:*   `{file.file_size_mb}`\n"
            f"> ⏰ *Start time:*   `{file.start_datetime}`\n"
            f"> ⏱ *Duration:*   `{file.current_download_duration}`\n"
            f"> 🔻 *Retries:*   `{file.download_retries}`\n"
            f"> 🔄 *Status:*   `{file.status}`\n\n"
        )
        status_message += file_status

        if i % 2 == 0 or i == len(downloading_files):
            # Add page number
            if i > 2:
                status_message = f"Page {math.ceil(i / 2)}\n" + status_message

            await context.bot.send_message(
                chat_id=update.message.chat_id,
                text=status_message,
                parse_mode="MarkdownV2",
            )
            status_message = ""
            await asyncio.sleep(0.3)


@message_handler(filters.VIDEO | filters.Document.ALL | filters.PHOTO)
@auth_required
async def download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Queue and download the file sent by the user."""
    logger.info("Download request received")

    file_id, file_name, file_size_bytes = _get_media_info(update.message)

    if not file_id:
        await update.message.reply_text("Could not detect a valid file, video, or photo.")
        return

    try:
        check_file_exists(file_id, file_name)
    except Exception as e:
        logger.error(f"Error checking file exists: {e}")
        await update.message.reply_text(
            f"⛔ File already exists\\!\nError:```\n{e}```",
            parse_mode="MarkdownV2",
        )
        return

    download_file = DownloadFile(file_id, file_name, file_size_bytes)
    downloading_files[file_id] = download_file

    message = update.message
    bot = context.bot

    async def _enqueue():
        if _download_lock.locked():
            try:
                await message.reply_text(
                    f"⏳ In queue\\.\n> 📄 *File:*   `{file_name}`",
                    parse_mode="MarkdownV2",
                )
            except Exception as e:
                logger.warning(f"Failed to send queue notification: {e}")
        
        async with _download_lock:
            await _perform_download(bot, message, download_file)

    asyncio.create_task(_enqueue())