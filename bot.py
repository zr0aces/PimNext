import asyncio
import json
import os
import shutil
import time
import logging
import urllib.request

from telegram import BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

# Set log level from environment variable (default INFO)
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=getattr(logging, log_level, logging.INFO),
)

# Suppress noisy library loggers — only show WARNING and above from these
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

logger = logging.getLogger("printbot")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VERSION = "1.0.0"
DATA_DIR = "data"

# Maximum characters of stderr to include in error replies
MAX_STDERR_LENGTH = 300

# Per-chat print options: {chat_id: {"color": bool, "copies": int, "ts": float}}
# Entries expire after PRINT_OPTIONS_TTL seconds.
print_options: dict = {}
PRINT_OPTIONS_TTL = 600  # 10 minutes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_cups_server() -> str:
    """Return the configured CUPS server host. Raises if not set."""
    server = os.getenv("CUPS_SERVER")
    if not server:
        raise RuntimeError("CUPS_SERVER environment variable is not set")
    return server


def get_printer_name() -> str:
    """Return the configured printer name. Raises if not set."""
    printer = os.getenv("PRINTER_NAME")
    if not printer:
        raise RuntimeError("PRINTER_NAME environment variable is not set")
    return printer


def get_allowed_chat_ids() -> list[int]:
    """Parse allowed chat IDs from environment variable (comma or space separated).

    Chat IDs are permanent numeric identifiers assigned by Telegram and cannot
    be changed or spoofed, making them reliable for access control.
    """
    raw = os.getenv("ALLOWED_CHAT_IDS", "")
    ids = []
    for part in raw.replace(",", " ").split():
        part = part.strip()
        if part:
            try:
                ids.append(int(part))
            except ValueError:
                logger.warning("Ignoring non-integer value in ALLOWED_CHAT_IDS: %r", part)
    return ids


def pop_print_options(chat_id: int) -> dict:
    """Return and remove print options for a chat, respecting the TTL.

    Returns defaults if no options are set or the options have expired.
    """
    entry = print_options.pop(chat_id, None)
    if entry and (time.monotonic() - entry.get("ts", 0)) < PRINT_OPTIONS_TTL:
        return entry
    return {"color": True, "copies": 1}


async def run_cups_command(cmd: list[str], timeout: int = 5) -> tuple[str, str, int]:
    """Run a CUPS CLI command and return (stdout, stderr, returncode)."""
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    return stdout.decode(), stderr.decode(), process.returncode


# ---------------------------------------------------------------------------
# Bot text constants
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "📠 *PrintBot Commands*\n\n"
    "/start — Show the welcome message\n"
    "/help — Show this help message\n"
    "/status — Check printer availability\n"
    "/jobs — Show the print queue\n"
    "/cancel — Cancel all print jobs\n"
    "/clean — Delete cached files (allowed users only)\n\n"
    "Send a *photo* or *document* to print it.\n\n"
    "*Print options* — send before your file:\n"
    "  `bw` or `gray` — black & white\n"
    "  `2x`, `3x`, `4x` — multiple copies\n"
    "  `bw 2x` — combine options"
)

COPY_OPTIONS = {"2x": 2, "3x": 3, "4x": 4}


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def start(update, context):
    await update.effective_message.reply_text(
        "👋 Welcome to *PrintBot*!\n\n"
        "Send me a photo or document and I'll print it for you.\n\n"
        "Use /help to see all available commands.",
        parse_mode="Markdown",
    )


async def help_command(update, context):
    await update.effective_message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def status(update, context):
    """Report whether the configured printer is reachable via CUPS."""
    lpstat = shutil.which("lpstat")
    if not lpstat:
        await update.effective_message.reply_text(
            "⚠️ CUPS client tools (`lpstat`) not found on this system.",
        )
        return

    try:
        server = get_cups_server()
        cmd = [lpstat, "-h", server, "-p"]
        stdout, stderr, returncode = await run_cups_command(cmd, timeout=5)

        if returncode == 0:
            if stdout.strip():
                msg = f"🟢 Printer is available:\n```\n{stdout.strip()}\n```"
            else:
                msg = "🟡 No printers are currently registered on the server."
        else:
            err = stderr.strip()[:MAX_STDERR_LENGTH]
            msg = f"🔴 Could not reach printer server:\n`{err or 'Unknown error'}`"

    except asyncio.TimeoutError:
        msg = "⚠️ Printer status check timed out."
    except RuntimeError as e:
        msg = f"⚠️ Configuration error: {e}"

    await update.effective_message.reply_text(msg, parse_mode="Markdown")


async def jobs_command(update, context):
    """Show the current CUPS print queue."""
    lpstat = shutil.which("lpstat")
    if not lpstat:
        await update.effective_message.reply_text(
            "⚠️ CUPS client tools (`lpstat`) not found on this system.",
        )
        return

    try:
        server = get_cups_server()
        cmd = [lpstat, "-h", server, "-o"]
        stdout, stderr, returncode = await run_cups_command(cmd, timeout=5)

        if returncode == 0:
            msg = (
                f"🖨️ Print queue:\n```\n{stdout.strip()}\n```"
                if stdout.strip()
                else "📭 No jobs in queue"
            )
        else:
            err = stderr.strip()[:MAX_STDERR_LENGTH]
            msg = f"⚠️ Could not fetch queue: {err or 'Unknown error'}"

    except asyncio.TimeoutError:
        msg = "⚠️ Print queue check timed out."
    except RuntimeError as e:
        msg = f"⚠️ Configuration error: {e}"

    await update.effective_message.reply_text(msg, parse_mode="Markdown")


async def cancel_command(update, context):
    """Cancel all pending print jobs."""
    cancel_bin = shutil.which("cancel")
    if not cancel_bin:
        await update.effective_message.reply_text(
            "⚠️ CUPS client tools (`cancel`) not found on this system.",
        )
        return

    try:
        server = get_cups_server()
        cmd = [cancel_bin, "-h", server, "-a"]
        _, stderr, returncode = await run_cups_command(cmd, timeout=5)

        if returncode == 0:
            msg = "🗑️ All print jobs cancelled"
        else:
            err = stderr.strip()[:MAX_STDERR_LENGTH]
            msg = f"⚠️ Could not cancel jobs: {err or 'Unknown error'}"

    except asyncio.TimeoutError:
        msg = "⚠️ Cancel command timed out."
    except RuntimeError as e:
        msg = f"⚠️ Configuration error: {e}"

    await update.effective_message.reply_text(msg)


async def clean(update, context):
    removed = perform_cleanup()
    await update.effective_message.reply_text(
        f"🗑️ Cleaned up {removed} cached file(s) from the data folder."
    )


async def set_print_options(update, context):
    """Parse print option keywords from a text message."""
    chat_id = update.effective_chat.id
    text = update.effective_message.text.strip().lower()

    color = True
    copies = 1
    valid = True

    tokens = text.split()
    for token in tokens:
        if token in ("bw", "gray"):
            color = False
        elif token in COPY_OPTIONS:
            copies = COPY_OPTIONS[token]
        else:
            valid = False

    if not valid or not tokens:
        await update.effective_message.reply_text(
            "❓ Unknown option. Use: `bw`, `2x`, `3x`, `4x`, `bw 2x`",
            parse_mode="Markdown",
        )
        return

    print_options[chat_id] = {"color": color, "copies": copies, "ts": time.monotonic()}

    mode = "B&W" if not color else "Color"
    count = f"{copies} copies" if copies > 1 else "1 copy"
    await update.effective_message.reply_text(f"⚙️ Next print: {mode}, {count}")


async def print_msg(update, context):
    """Handle incoming photo or document and send it to the printer."""
    user = update.effective_user
    logger.info(
        "Received message from user_id=%s username=%s",
        user.id,
        user.username or "N/A",
    )

    # Determine file and original extension
    file = None
    orig_ext = ""
    if update.effective_message.photo:
        photo = max(update.effective_message.photo, key=lambda x: x.file_size)
        file = await photo.get_file()
        orig_ext = ".jpg"  # Telegram photos are always JPEG
    elif update.effective_message.document:
        doc = update.effective_message.document
        orig_ext = os.path.splitext(doc.file_name)[1] if doc.file_name else ""
        file = await doc.get_file()

    if file is None:
        await update.effective_message.reply_text(
            "❌ Could not find a printable file in your message."
        )
        return

    os.makedirs(DATA_DIR, exist_ok=True)
    file_path = os.path.join(DATA_DIR, f"{int(time.time())}{orig_ext}")
    await file.download_to_drive(file_path)
    logger.info("File saved at %s", file_path)

    chat_id = update.effective_chat.id
    opts = pop_print_options(chat_id)
    color = opts["color"]
    copies = opts["copies"]

    success = False
    try:
        await print_file(file_path, color=color, copies=copies)
        success = True
        await update.effective_message.reply_text("✅ Sent to printer!")

    except RuntimeError as e:
        logger.error("Print failed: %s", e)
        cmd_used = getattr(e, "cmd", None)
        msg = f"❌ Print failed: {e}"
        if cmd_used:
            msg += f"\n\nCommand used:\n{cmd_used}"
        await update.effective_message.reply_text(msg)

    except Exception as e:
        logger.exception("Unexpected error during print: %s", e)
        await update.effective_message.reply_text(
            f"❌ Unexpected error: {e}"
        )

    finally:
        try:
            os.remove(file_path)
            logger.info("Cleaned up %s", file_path)
        except OSError as e:
            logger.warning("Could not remove %s: %s", file_path, e)

        if success:
            notify_homeassistant(
                file_name=os.path.basename(file_path),
                chat_id=chat_id,
                copies=copies,
                color=color,
            )


# ---------------------------------------------------------------------------
# Core print logic
# ---------------------------------------------------------------------------

async def print_file(file_path: str, color: bool = True, copies: int = 1) -> str:
    """Send a file to the printer using lp.

    Always passes -h <CUPS_SERVER> and -d <PRINTER_NAME> explicitly.
    Both environment variables are required — raises RuntimeError if missing.

    Returns the shell command string that was executed.
    """
    lp = shutil.which("lp")
    if not lp:
        raise RuntimeError("lp command not found — is cups-client installed?")

    server = get_cups_server()
    printer = get_printer_name()

    # Build: lp -h <server> -d <printer> -o fit-to-page -o media=A4 [options] <file>
    cmd = [lp, "-h", server, "-d", printer, "-o", "fit-to-page", "-o", "media=A4"]

    if not color:
        cmd += ["-o", "ColorModel=Gray"]
    if copies > 1:
        cmd += ["-n", str(copies)]
    cmd.append(file_path)

    cmd_str = " ".join(cmd)
    logger.info("Shell command: %s", cmd_str)

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    except asyncio.TimeoutError:
        ex = RuntimeError("lp command timed out after 30 seconds")
        ex.cmd = cmd_str
        raise ex

    if process.returncode != 0:
        err_str = stderr.decode().strip()[:MAX_STDERR_LENGTH]
        logger.error(
            "lp failed (returncode=%s) stderr: %s",
            process.returncode,
            stderr.decode(),
        )
        ex = RuntimeError(err_str or "Print command failed")
        ex.cmd = cmd_str
        raise ex

    logger.info("lp stdout: %s", stdout.decode().strip())
    return cmd_str


# ---------------------------------------------------------------------------
# Home Assistant integration
# ---------------------------------------------------------------------------

def notify_homeassistant(file_name: str, chat_id: int, copies: int, color: bool):
    """Fire a Home Assistant event after a successful print (best-effort)."""
    ha_url = os.getenv("HA_URL")
    ha_token = os.getenv("HA_TOKEN")
    if not ha_url or not ha_token:
        return

    url = f"{ha_url}/api/events/printbot_job_sent"
    payload = json.dumps({
        "file_name": file_name,
        "chat_id": chat_id,
        "copies": copies,
        "color": color,
    }).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {ha_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=3).close()
        logger.info("Home Assistant notified: %s", url)
    except Exception as e:
        logger.warning("Home Assistant notification failed: %s", e)


# ---------------------------------------------------------------------------
# Startup / lifecycle
# ---------------------------------------------------------------------------

async def post_init(application):
    """Register bot commands and start background tasks."""
    await application.bot.set_my_commands([
        BotCommand("start", "Show the welcome message"),
        BotCommand("help", "Show available commands"),
        BotCommand("status", "Check printer availability"),
        BotCommand("jobs", "Show the print queue"),
        BotCommand("cancel", "Cancel all print jobs"),
        BotCommand("clean", "Delete cached files (allowed users only)"),
    ])

    # Start periodic cleanup task — track it so exceptions are not silently lost
    task = asyncio.create_task(cleanup_task())
    task.add_done_callback(_on_cleanup_task_done)
    logger.info("Periodic cleanup task started (6h interval)")


def _on_cleanup_task_done(task: asyncio.Task):
    """Log if the background cleanup task crashes unexpectedly."""
    if not task.cancelled() and task.exception():
        logger.error("Cleanup task crashed: %s", task.exception())


async def cleanup_task():
    """Background task to periodically clean up the data directory."""
    while True:
        await asyncio.sleep(6 * 3600)  # every 6 hours
        logger.info("Running periodic data cleanup...")
        try:
            removed = perform_cleanup()
            logger.info("Periodic cleanup removed %s file(s)", removed)
        except Exception as e:
            logger.error("Periodic cleanup failed: %s", e)


def perform_cleanup() -> int:
    """Delete all cached files from the data directory. Returns count removed."""
    removed = 0
    if os.path.exists(DATA_DIR):
        for filename in os.listdir(DATA_DIR):
            filepath = os.path.join(DATA_DIR, filename)
            try:
                if os.path.isfile(filepath):
                    os.remove(filepath)
                    removed += 1
            except OSError as e:
                logger.error("Error removing %s: %s", filepath, e)
    return removed


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    token = os.getenv("TOKEN")
    if not token:
        logger.error("TOKEN environment variable is not set")
        raise SystemExit("TOKEN environment variable is required")

    allowed_chat_ids = get_allowed_chat_ids()

    # Log configuration at startup for easy debugging
    logger.info("PrintBot v%s starting...", VERSION)
    logger.info(
        "Configuration: CUPS_SERVER=%s  PRINTER_NAME=%s",
        os.getenv("CUPS_SERVER", "(not set)"),
        os.getenv("PRINTER_NAME", "(not set)"),
    )
    logger.info("Allowed chat IDs: %s", allowed_chat_ids or "ALL")

    # Clean up any stale files from a previous run
    logger.info("Performing startup cleanup...")
    perform_cleanup()

    application = (
        ApplicationBuilder()
        .token(token)
        .post_init(post_init)
        .build()
    )

    # Public commands (no access restriction)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status))

    # Restricted commands and message handlers
    if allowed_chat_ids:
        chat_id_filter = filters.Chat(chat_id=allowed_chat_ids)
    else:
        logger.warning(
            "ALLOWED_CHAT_IDS is not set — all Telegram users can print. "
            "Set this variable to restrict access."
        )
        chat_id_filter = filters.ALL

    application.add_handler(CommandHandler("jobs", jobs_command, filters=chat_id_filter))
    application.add_handler(CommandHandler("cancel", cancel_command, filters=chat_id_filter))
    application.add_handler(CommandHandler("clean", clean, filters=chat_id_filter))

    application.add_handler(
        MessageHandler(
            chat_id_filter
            & (filters.PHOTO | filters.Document.ALL)
            & (~filters.COMMAND),
            print_msg,
        )
    )

    application.add_handler(
        MessageHandler(
            chat_id_filter
            & filters.TEXT
            & (~filters.COMMAND),
            set_print_options,
        )
    )

    application.run_polling()


if __name__ == "__main__":
    main()
