import logging
import os
import shutil

from telegram import Update
from telegram.ext import ContextTypes

from ..middlewares.handlers import command_handler
from ..utils import env

logger = logging.getLogger(__name__)

DOWNLOAD_TO_DIR = env.DOWNLOAD_TO_DIR

# List of available commands
commands = {
    "/start": "Start the bot",
    "/help": "Get help",
    "/info": "Get user and chat info",
    "/storage": "Get available storage information",
    "/status": "Get downloading files status",
}


@command_handler("help")
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a list of available commands to the user."""
    commands_list = "The following commands are available:\n" + "\n".join(
        [f"{key} - {value}" for key, value in commands.items()]
    )
    await update.message.reply_text(
        f"{commands_list}\n\nSend me a file and I'll download it to `{DOWNLOAD_TO_DIR}`.",
        parse_mode="markdown",
    )


@command_handler("start")
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a start message to the user."""
    user = update.effective_user
    await update.message.reply_html(
        f"Hi {user.mention_html()}! I'm a bot that can download files for you. "
        "Send me a file and I'll download it for you.\n\n"
        "Use /help to see available commands."
    )


@command_handler("info")
async def info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send user and chat IDs to the user."""
    user = update.effective_user
    await update.message.reply_text(
        f"*User ID*: {user.id}\n*Chat ID*: {update.effective_chat.id}",
        parse_mode="markdown",
    )

@command_handler("storage")
async def storage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send available storage information and list files in the download folder."""
    if not os.path.exists(DOWNLOAD_TO_DIR):
        await update.message.reply_text("The specified folder does not exist.")
        return

    # Disk usage info
    total, used, free = shutil.disk_usage(DOWNLOAD_TO_DIR)
    
    # List files and their sizes
    try:
        files = os.listdir(DOWNLOAD_TO_DIR)
        file_list_str = ""
        if files:
            file_list_str = "\n\n*Files in directory:*\n"
            # Sort files by name
            files.sort()
            for f in files:
                full_path = os.path.join(DOWNLOAD_TO_DIR, f)
                if os.path.isfile(full_path):
                    size_bytes = os.path.getsize(full_path)
                    # Convert size to readable format
                    if size_bytes >= 1024**3:
                        size_str = f"{size_bytes / (1024**3):.2f} GB"
                    elif size_bytes >= 1024**2:
                        size_str = f"{size_bytes / (1024**2):.2f} MB"
                    elif size_bytes >= 1024:
                        size_str = f"{size_bytes / 1024:.2f} KB"
                    else:
                        size_str = f"{size_bytes} B"
                    
                    file_list_str += f"📄 `{f}` \\- `{size_str}`\n"
        else:
            # FIX 1: Escape parentheses for MarkdownV2 using double backslashes
            file_list_str = "\n\n*Files in directory:*\n\\(Empty\\)"

    except Exception as e:
        logger.error(f"Error listing files: {e}")
        # FIX 2: Wrap the exception in backticks so unescaped special chars in the error don't break the parser
        file_list_str = f"\n\n*Error listing files:*\n`{e}`"

    response_msg = (
        f"📂 *Folder*:   `{DOWNLOAD_TO_DIR}`\n"
        f"🟣 *Total Space*:   `{total // (2**30)} GB`\n"
        f"🟠 *Used Space*:   `{used // (2**30)} GB`\n"
        f"🟢 *Free Space*:    `{free // (2**30)} GB`"
        f"{file_list_str}"
    )

    # Split message if it is too long (Telegram limit is 4096 chars)
    if len(response_msg) > 4000:
        # Send summary first
        await update.message.reply_text(
            f"📂 *Folder*:   `{DOWNLOAD_TO_DIR}`\n"
            f"🟣 *Total Space*:   `{total // (2**30)} GB`\n"
            f"🟠 *Used Space*:   `{used // (2**30)} GB`\n"
            f"🟢 *Free Space*:    `{free // (2**30)} GB`\n"
            # FIX 3: Escape periods for MarkdownV2
            f"\nFile list is too long to display completely, showing partial list\\.\\.\\.",
            parse_mode="MarkdownV2",
        )
        # Send as much of the file list as possible, or just the first chunk
        await update.message.reply_text(file_list_str[:4000], parse_mode="MarkdownV2")
    else:
        await update.message.reply_text(response_msg, parse_mode="MarkdownV2")