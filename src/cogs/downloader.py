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
    """Download the file sent by the user directly without confirmation."""

    logger.info("Download command received - Starting direct download")

    message = update.message
    media = None
    file_name = None

    if message.document:
        media = message.document
        file_name = media.file_name
    elif message.video:
        media = message.video
        file_name = getattr(media, "file_name", None) or f"video_{media.file_id}.mp4"
    elif message.photo:
        media = message.photo[-1]
        file_name = f"photo_{media.file_id}.jpg"
    
    if not media:
        await message.reply_text("Could not detect a valid file, video, or photo.")
        return

    file_id = media.file_id
    file_name = file_name or f"file_{file_id}"  # Final fallback
    file_size = getattr(media, "file_size", 0)

    try:
        check_file_exists(file_id, file_name)
    except Exception as e:
        logger.error(f"Error checking file exists: {e}")
        await message.reply_text(
            f"⛔ File already exists\!\nError:```\n{e}```",
            parse_mode="MarkdownV2",
        )
        return

    # Add file to downloading_files
    download_file = DownloadFile(
        file_id,
        file_name,
        file_size,
    )
    downloading_files[file_id] = download_file

    # Send downloading message
    status_msg = await message.reply_text("⬇️ Downloading file...")

    try:
        new_file = await get_file(context.bot, download_file)
    except Exception as e:
        logger.error(f"Error downloading file: {e}")
        traceback.print_exc()

        # Remove from current downloading files
        if file_id in downloading_files:
            downloading_files.pop(file_id)

        await status_msg.edit_text(
            (
                f"⛔ Error downloading file\n"
                f"> 📄 *File name:*   `{download_file.file_name}`\n"
                f"> 💾 *File size:*   `{download_file.file_size_mb}`\n"
                f"```\n{e}```"
            ),
            parse_mode="MarkdownV2",
        )
        return
    else:
        download_file.download_complete()

    # Rename the file to the original file name
    # file_path is relative, e.g. "videos/file_0.mp4" or "documents/file_1.txt"
    current_file_path = os.path.join(BOT_API_DIR, TOKEN_SUB_DIR, new_file.file_path)
    move_to_path = f"{DOWNLOAD_TO_DIR}{file_name}"

    # Move the file to the download directory
    try:
        os.makedirs(DOWNLOAD_TO_DIR, exist_ok=True)
        os.rename(current_file_path, move_to_path)
    except Exception as rename_error:
        logger.error(f"Error RENAMING file: {rename_error}")

        # Move the file instead of renaming
        try:
            await asyncio.to_thread(shutil.move, current_file_path, move_to_path)
        except Exception as move_error:
            logger.error(f"Error MOVING file: {move_error}")

            if file_id in downloading_files:
                downloading_files.pop(file_id)
            
            await status_msg.edit_text(
                (
                    f"⛔ Error moving file\n"
                    f"> 📂 *File path:*   `{new_file.file_path}`\n"
                    f"> 📂 *Move to path:*   `{move_to_path}`\n"
                    f"Rename error:\n```\n{rename_error}```\n"
                    f"Move error:\n```\n{move_error}```"
                ),
                parse_mode="MarkdownV2",
            )
            return

    download_file.move_complete()
    if file_id in downloading_files:
        downloading_files.pop(file_id)

    # If linux, give file correct permissions
    if platform.system() == "Linux":
        try:
            os.chown(move_to_path, 1000, 1000)
        except Exception:
            pass

    response_message = (
        f"✅ File downloaded successfully\\.\n\n"
        f"> 📄 *File name:*   `{download_file.file_name}`\n"
        f"> 📂 *File path:*   `{new_file.file_path}`\n"
        f"> 💾 *File size:*   `{download_file.file_size_mb}`\n"
        f"> 🔻 *Retries:*   `{download_file.download_retries}`\n"
        f"> ⏱ *Download Duration:*   `{download_file.download_duration}`\n"
        f"> ⏱ *Moving Duration:*   `{download_file.move_duration}`\n"
        f"> ⏱ *Total Duration:*   `{download_file.total_duration}`"
    )

    await status_msg.edit_text(response_message, parse_mode="MarkdownV2")
