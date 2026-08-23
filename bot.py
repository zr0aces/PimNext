import asyncio
import io
import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field, replace

import httpx
from PIL import Image
from pypdf import PdfReader, PdfWriter
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

# LOG_LEVEL falls back to INFO when unset or unrecognised.
_level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=_level if isinstance(_level, int) else logging.INFO,
)

# Suppress noisy library loggers — only show WARNING and above from these
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

logger = logging.getLogger("notanext")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VERSION = "1.2.0"
DATA_DIR = "data"
PREFERENCES_FILE = os.path.join(DATA_DIR, "preferences.json")

# Maximum characters of stderr to include in error replies
MAX_STDERR_LENGTH = 300

# Maximum file size accepted for printing (20 MB — Telegram bot limit)
MAX_FILE_BYTES = 20 * 1024 * 1024

# Printable document extensions (lowercase). Photos are always accepted.
PRINTABLE_EXTENSIONS = {
    ".pdf", ".ps",
    ".jpg", ".jpeg", ".png", ".gif",
    ".txt",
    ".doc", ".docx", ".odt",
}

# Pre-sorted display string — reused in every unsupported-type error reply
PRINTABLE_EXTENSIONS_DISPLAY = ", ".join(sorted(PRINTABLE_EXTENSIONS))

# Extensions merge_to_pdf() can combine into a single document. Half mode always
# merges before printing, so anything outside this set is rejected up front
# rather than silently falling back to a non-2-up multi-file lp job.
MERGEABLE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".pdf"}
MERGEABLE_EXTENSIONS_DISPLAY = ", ".join(sorted(MERGEABLE_EXTENSIONS))

# Session and rate-limit timeouts
PRINT_COOLDOWN = 10  # minimum seconds between accepted print jobs
SESSION_TTL = 1800  # 30 minutes for temporary overrides & half-queue

# CUPS binary paths — resolved once at startup to avoid repeated filesystem scans
LP_BIN: str | None = shutil.which("lp")
LPSTAT_BIN: str | None = shutil.which("lpstat")
CANCEL_BIN: str | None = shutil.which("cancel")

COPY_OPTIONS: dict[str, int] = {"1x": 1, "2x": 2, "3x": 3, "4x": 4}

# Conversation states for the preference-setting wizard
PREF_COLOR, PREF_MODE, PREF_PAPER = range(3)

# Cap on the number of stored per-chat preference entries. Keeps preferences.json
# bounded and stops an open wizard from being used to exhaust the store.
MAX_PREFERENCES = 10


# ---------------------------------------------------------------------------
# Dataclasses & State
# ---------------------------------------------------------------------------

@dataclass
class PrintOptions:
    """One chat's print settings.

    The four leading fields make up the persisted profile. `ts` is the monotonic
    timestamp of the temporary 30-minute session and is never written to disk.
    """

    color: bool = True
    copies: int = 1
    media: str = "A4"
    number_up: int = 1
    ts: float = 0.0

    @classmethod
    def from_dict(cls, data: dict) -> "PrintOptions":
        """Build a profile from a persisted mapping, filling in missing keys."""
        defaults = cls()
        return cls(
            color=bool(data.get("color", defaults.color)),
            copies=int(data.get("copies", defaults.copies)),
            media=str(data.get("media", defaults.media)),
            number_up=int(data.get("number_up", defaults.number_up)),
        )

    def to_dict(self) -> dict:
        """Serialise the persisted profile, dropping the session timestamp."""
        data = asdict(self)
        data.pop("ts", None)
        return data

    @property
    def color_label(self) -> str:
        """Human-readable colour mode, used in every user-facing reply."""
        return "Color" if self.color else "Gray (B&W)"

    @property
    def mode_label(self) -> str:
        """Human-readable sheet mode, used in every user-facing reply."""
        return "Half sheet" if self.number_up == 2 else "Normal"


@dataclass
class HalfQueueEntry:
    """Queued files and timestamp for a half-mode print session."""

    files: list[str] = field(default_factory=list)
    ts: float = 0.0


# Keyboards pre-built once at startup to avoid runtime allocations
COLOR_KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("🎨 Color", callback_data="pref_color_yes"),
            InlineKeyboardButton("⬛ Gray (B&W)", callback_data="pref_color_no"),
        ]
    ]
)

MODE_KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("📄 Normal (full page)", callback_data="pref_mode_normal"),
            InlineKeyboardButton("📑 Half sheet (2 per page)", callback_data="pref_mode_half"),
        ]
    ]
)

PAPER_KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("A4", callback_data="pref_paper_A4"),
            InlineKeyboardButton("A5", callback_data="pref_paper_A5"),
        ]
    ]
)

# Chat state (in-memory)
print_options: dict[int, PrintOptions] = {}
last_print_time: dict[int, float] = {}
half_queue: dict[int, HalfQueueEntry] = {}
user_preferences: dict[str, PrintOptions] = {}


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


def parse_option_tokens(tokens: list[str], opts: PrintOptions) -> bool:
    """Apply print-option keywords to `opts` in place.

    Returns False if any token was unrecognised — the caller rejects the whole
    message in that case, so a half-applied `opts` is never stored.
    """
    for token in tokens:
        if token in ("bw", "gray"):
            opts.color = False
        elif token == "color":
            opts.color = True
        elif token in COPY_OPTIONS:
            opts.copies = COPY_OPTIONS[token]
        elif token == "a4":
            opts.media = "A4"
        elif token == "a5":
            opts.media = "A5"
        elif token in ("half", "2up"):
            opts.number_up = 2
        elif token in ("normal", "full", "single", "1up"):
            opts.number_up = 1
        else:
            return False
    return True


def cooldown_remaining(chat_id: int) -> int:
    """Seconds left on this chat's print cooldown, or 0 if it may print now."""
    elapsed = time.monotonic() - last_print_time.get(chat_id, 0)
    # Round up so the last fractional second still blocks (and never reports "0s").
    return int(PRINT_COOLDOWN - elapsed) + 1 if elapsed < PRINT_COOLDOWN else 0


def get_print_options(chat_id: int) -> PrintOptions:
    """Return print options for a chat, respecting the TTL and extending it on use.

    Falls back to the chat's saved persistent defaults (or system defaults) if no
    session options are active.
    """
    entry = print_options.get(chat_id)
    if entry and (time.monotonic() - entry.ts) < SESSION_TTL:
        entry.ts = time.monotonic()  # Extend the session
        return entry
    return get_default_preferences(chat_id)


def get_default_preferences(chat_id: int) -> PrintOptions:
    """Return the saved persistent default preferences for a chat, or system defaults."""
    saved = user_preferences.get(str(chat_id))
    if saved:
        return replace(saved, ts=0.0)
    return PrintOptions()


def load_preferences() -> None:
    """Load per-chat persistent preferences from disk into memory.

    Trims the loaded data to MAX_PREFERENCES so an oversized file from an older
    build doesn't exceed the cap at runtime.
    """
    global user_preferences
    try:
        if not os.path.exists(PREFERENCES_FILE):
            return
        with open(PREFERENCES_FILE, "r") as f:
            data = json.load(f)
        if len(data) > MAX_PREFERENCES:
            # Keep only the first MAX_PREFERENCES entries (arbitrary but deterministic)
            data = dict(list(data.items())[:MAX_PREFERENCES])
            logger.warning("Preferences file exceeded cap. Trimmed to %d entries.", len(data))
        loaded: dict[str, PrintOptions] = {}
        for key, value in data.items():
            try:
                loaded[key] = PrintOptions.from_dict(value)
            except (AttributeError, TypeError, ValueError) as e:
                logger.warning("Skipping malformed preference entry %r: %s", key, e)
        user_preferences = loaded
        logger.info("Loaded preferences for %d chat(s).", len(user_preferences))
    except Exception as e:
        logger.warning("Could not load preferences file: %s", e)
        user_preferences = {}


def save_preferences() -> None:
    """Persist the in-memory user_preferences dict to disk atomically."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = PREFERENCES_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({k: v.to_dict() for k, v in user_preferences.items()}, f)
        os.replace(tmp, PREFERENCES_FILE)
        logger.debug("Preferences saved (%d chat(s)).", len(user_preferences))
    except Exception as e:
        logger.error("Could not save preferences: %s", e)


async def run_cups_command(cmd: list[str], timeout: int = 5) -> tuple[str, str, int]:
    """Run a CUPS CLI command and return (stdout, stderr, returncode).

    Guarantees the subprocess is killed and reaped if the timeout expires,
    preventing zombie processes and fd leaks.
    """
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise
    return stdout.decode(), stderr.decode(), process.returncode


async def run_cups_query(
    binary: str | None, tool: str, flags: list[str], action: str
) -> tuple[str | None, str | None]:
    """Run a CUPS query against the configured server, returning (stdout, error).

    Exactly one of the two is not None. Shared by /status, /jobs and /cancel so
    the binary check, `-h <server>` wiring, timeout handling and stderr
    truncation live in one place instead of once per command handler.
    """
    if not binary:
        return None, f"⚠️ CUPS client tools (`{tool}`) not found on this system."

    try:
        server = get_cups_server()
        stdout, stderr, returncode = await run_cups_command([binary, "-h", server, *flags])
    except asyncio.TimeoutError:
        return None, f"⚠️ {action} timed out."
    except RuntimeError as e:
        return None, f"⚠️ Configuration error: {e}"

    if returncode != 0:
        err = stderr.strip()[:MAX_STDERR_LENGTH] or "Unknown error"
        return None, f"⚠️ {action} failed: `{err}`"

    return stdout, None


# ---------------------------------------------------------------------------
# Bot text constants
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "📠 *NotaNext Commands*\n\n"
    "/start — Show the welcome message\n"
    "/help — Show this help message\n"
    "/preferences — Set your default printing preferences\n"
    "/status — Check printer availability\n"
    "/jobs — Show the print queue\n"
    "/cancel — Cancel all print jobs\n"
    "/clean — Delete cached files (allowed users only)\n\n"
    "Send a *photo* or *document* to print it.\n\n"
    "*Print options* — send before your file:\n"
    "  `bw` or `gray` — black & white\n"
    "  `color` — full colour\n"
    "  `1x`, `2x`, `3x`, `4x` — number of copies\n"
    "  `a4`, `a5` — paper size\n"
    "  `half` or `2up` — queue files and print 2 per sheet\n"
    "  `normal`, `full`, `single` or `1up` — one page per sheet\n"
    "  `print` — flush queued half-mode files now\n"
    "  `bw 2x a5` — combine options\n\n"
    "_Per-session settings persist for 30 minutes, then fall back to your saved defaults._"
)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Welcome the user and launch the default-preference wizard."""
    chat_id = update.effective_chat.id
    saved = user_preferences.get(str(chat_id))

    welcome = "👋 Welcome to *NotaNext*!\n\nSend me a photo or document and I'll print it for you.\n\n"
    if saved:
        welcome += (
            f"📌 *Current defaults:* {saved.color_label} · {saved.mode_label} · {saved.media}\n\n"
            "Update your default printing preferences below, or use /help to get started.\n\n"
        )
    else:
        welcome += "Let's set up your default printing preferences.\n\n"

    welcome += "*Color mode?*"

    await update.effective_message.reply_text(
        welcome,
        parse_mode="Markdown",
        reply_markup=COLOR_KEYBOARD,
    )
    return PREF_COLOR


async def preferences_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the default-preference wizard directly."""
    await update.effective_message.reply_text(
        "⚙️ *Set Default Preferences*\n\n*Color mode?*",
        parse_mode="Markdown",
        reply_markup=COLOR_KEYBOARD,
    )
    return PREF_COLOR


async def pref_color_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle color-mode selection and ask for sheet mode."""
    query = update.callback_query
    await query.answer()

    draft = PrintOptions(color=query.data == "pref_color_yes")
    context.user_data["pref_draft"] = draft

    await query.edit_message_text(
        f"✅ Color mode: *{draft.color_label}*\n\n*Sheet mode?*",
        parse_mode="Markdown",
        reply_markup=MODE_KEYBOARD,
    )
    return PREF_MODE


async def pref_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle sheet-mode selection and ask for paper size."""
    query = update.callback_query
    await query.answer()

    draft = context.user_data.get("pref_draft") or PrintOptions()
    draft.number_up = 2 if query.data == "pref_mode_half" else 1
    context.user_data["pref_draft"] = draft

    await query.edit_message_text(
        f"✅ Color mode: *{draft.color_label}*\n✅ Sheet mode: *{draft.mode_label}*\n\n*Paper size?*",
        parse_mode="Markdown",
        reply_markup=PAPER_KEYBOARD,
    )
    return PREF_PAPER


async def pref_paper_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle paper-size selection, save preferences and confirm."""
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    draft = context.user_data.pop("pref_draft", None) or PrintOptions()
    draft.media = "A5" if query.data == "pref_paper_A5" else "A4"

    key = str(chat_id)

    # Enforce the cap: reject new entries (updates to existing entries always pass)
    if key not in user_preferences and len(user_preferences) >= MAX_PREFERENCES:
        await query.edit_message_text(
            f"⚠️ The preference store is full ({MAX_PREFERENCES} chat(s) already saved).",
        )
        return ConversationHandler.END

    user_preferences[key] = replace(draft, ts=0.0)
    save_preferences()

    # Apply the new defaults immediately for this active chat session.
    # Without this, a previously cached 30-minute override can keep using old
    # settings (e.g. normal mode) even though defaults were just saved.
    print_options[chat_id] = replace(draft, ts=time.monotonic())

    await query.edit_message_text(
        "✅ *Default preferences saved!*\n\n"
        f"  🎨 Color mode: *{draft.color_label}*\n"
        f"  📄 Sheet mode: *{draft.mode_label}*\n"
        f"  📐 Paper size: *{draft.media}*\n\n"
        "These will be used every time you print.\n"
        "Send text options like `bw` or `half` to override them temporarily.\n"
        "Use /preferences to change your defaults at any time.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def cancel_preferences(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the preference-setting wizard.

    /cancel is bound both here (as the wizard's fallback) and to cancel_command.
    While the wizard is open this handler wins, so the reply says how to reach
    the print-queue meaning of the command.
    """
    context.user_data.pop("pref_draft", None)
    await update.effective_message.reply_text(
        "❌ Preference setup cancelled.\n"
        "Send /cancel again to cancel pending print jobs."
    )
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the command and print-option reference."""
    await update.effective_message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report whether the configured printer is reachable via CUPS."""
    stdout, error = await run_cups_query(LPSTAT_BIN, "lpstat", ["-p"], "Printer status check")

    if error:
        msg = error
    elif stdout.strip():
        msg = f"🟢 Printer is available:\n```\n{stdout.strip()}\n```"
    else:
        msg = "🟡 No printers are currently registered on the server."

    await update.effective_message.reply_text(msg, parse_mode="Markdown")


async def jobs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the current CUPS print queue."""
    stdout, error = await run_cups_query(LPSTAT_BIN, "lpstat", ["-o"], "Print queue check")

    if error:
        msg = error
    elif stdout.strip():
        msg = f"🖨️ Print queue:\n```\n{stdout.strip()}\n```"
    else:
        msg = "📭 No jobs in queue"

    await update.effective_message.reply_text(msg, parse_mode="Markdown")


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel all pending print jobs."""
    _, error = await run_cups_query(CANCEL_BIN, "cancel", ["-a"], "Cancel command")
    await update.effective_message.reply_text(
        error or "🗑️ All print jobs cancelled",
        parse_mode="Markdown",
    )


async def clean(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete every cached file in the data directory, including queued files."""
    # Clear all half-mode queues — their files will be removed by perform_cleanup below
    half_queue.clear()
    # Offload blocking I/O to a thread pool to avoid stalling the event loop
    removed = await perform_cleanup_async()
    await update.effective_message.reply_text(
        f"🗑️ Cleaned up {removed} cached file(s) from the data folder."
    )


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a plain text message: either a print-option update or the `print` action.

    `print` is an action keyword rather than a setting — it flushes whatever is
    currently queued in half mode. Everything else is parsed as option keywords,
    and a single unrecognised token rejects the whole message.
    """
    chat_id = update.effective_chat.id
    text = update.effective_message.text.strip().lower()

    # "print" is an action keyword — flush the half-mode queue immediately.
    if text == "print":
        entry = half_queue.get(chat_id)
        if not entry or not entry.files:
            await update.effective_message.reply_text(
                "❓ No files queued. Send a file with `half` mode active to queue it.",
                parse_mode="Markdown",
            )
            return
        await _flush_half_queue(update, context, chat_id, get_print_options(chat_id))
        return

    # Start from the options currently in force, as a copy — the live session
    # object must not be mutated until the whole message parses cleanly.
    opts = replace(get_print_options(chat_id), ts=time.monotonic())

    tokens = text.split()
    if not tokens or not parse_option_tokens(tokens, opts):
        await update.effective_message.reply_text(
            "❓ Unknown option. Use: `bw`, `color`, `1x`, `2x`, `3x`, `4x`, "
            "`a4`, `a5`, `half`, `normal`, `print`",
            parse_mode="Markdown",
        )
        return

    print_options[chat_id] = opts

    # If switching away from half mode, discard any pending queued files.
    notice = ""
    if opts.number_up != 2:
        old_entry = half_queue.pop(chat_id, None)
        if old_entry and old_entry.files:
            for fp in old_entry.files:
                try:
                    os.remove(fp)
                    logger.info("Cleared stale half-queue file on option change: %s", fp)
                except OSError as e:
                    logger.warning("Could not remove queued file %s: %s", fp, e)
            notice = f"⚠️ {len(old_entry.files)} queued file(s) cleared (half mode disabled).\n"

    count = f"{opts.copies} copies" if opts.copies > 1 else "1 copy"
    format_str = f"{opts.media} (Half Sheet)" if opts.number_up == 2 else opts.media
    await update.effective_message.reply_text(
        f"{notice}⚙️ Settings updated: {opts.color_label}, {count}, {format_str}\n_(Active for 30m)_",
        parse_mode="Markdown",
    )


async def _get_file_info(update: Update) -> tuple | None:
    """Validate and retrieve a file from an incoming message.

    Returns (file_obj, orig_ext) on success, or None after sending an error reply.
    """
    msg = update.effective_message

    if msg.photo:
        # `if msg.photo:` guarantees a non-empty list here, so [-1] is safe.
        # Telegram sends photos ordered smallest → largest; pick the largest with a known size.
        # Fall back to the last entry (largest by Telegram's ordering) if all sizes are None.
        photos_with_size = [p for p in msg.photo if p.file_size is not None]
        photo = max(photos_with_size, key=lambda x: x.file_size) if photos_with_size else msg.photo[-1]
        if photo.file_size and photo.file_size > MAX_FILE_BYTES:
            await msg.reply_text(
                f"❌ File too large ({photo.file_size // 1024 // 1024} MB). Maximum is 20 MB."
            )
            return None
        file = await photo.get_file()
        return file, ".jpg"

    if msg.document:
        doc = msg.document
        orig_ext = os.path.splitext(doc.file_name)[1].lower() if doc.file_name else ""
        if orig_ext not in PRINTABLE_EXTENSIONS:
            await msg.reply_text(
                f"❌ Unsupported file type `{orig_ext or '(none)'}`. "
                f"Supported: {PRINTABLE_EXTENSIONS_DISPLAY}",
                parse_mode="Markdown",
            )
            return None
        if doc.file_size and doc.file_size > MAX_FILE_BYTES:
            await msg.reply_text(
                f"❌ File too large ({doc.file_size // 1024 // 1024} MB). Maximum is 20 MB."
            )
            return None
        file = await doc.get_file()
        return file, orig_ext

    await msg.reply_text("❌ Could not find a printable file in your message.")
    return None


async def _print_and_reply(
    update: Update,
    chat_id: int,
    file_paths: list[str],
    opts: PrintOptions,
    success_text: str,
) -> None:
    """Print files, notify Home Assistant, reply, and always clean the files up.

    Shared by the normal and half-mode paths so their error handling and
    temporary-file cleanup cannot drift apart.
    """
    try:
        await print_file(file_paths, opts)

        # Fire the HA webhook after a confirmed print, before the Telegram reply
        await notify_homeassistant(
            file_name=", ".join(os.path.basename(fp) for fp in file_paths),
            chat_id=chat_id,
            copies=opts.copies,
            color=opts.color,
        )
        await update.effective_message.reply_text(success_text, parse_mode="Markdown")

    except RuntimeError as e:
        logger.error("Print failed: %s", e)
        cmd_used = getattr(e, "cmd", None)
        msg = f"❌ Print failed: {e}"
        if cmd_used:
            msg += f"\n\nCommand used:\n{cmd_used}"
        await update.effective_message.reply_text(msg)

    except Exception as e:
        logger.exception("Unexpected error during print: %s", e)
        await update.effective_message.reply_text(f"❌ Unexpected error: {e}")

    finally:
        for fp in file_paths:
            try:
                os.remove(fp)
                logger.info("Cleaned up %s", fp)
            except OSError as exc:
                logger.warning("Could not remove %s: %s", fp, exc)


async def _flush_half_queue(
    update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, opts: PrintOptions
) -> None:
    """Rate-limit, then print all files queued for this chat in half mode.

    Clears the queue on success.  Leaves the queue intact when rate-limited so
    the user can retry by sending `print` after the cooldown expires.
    """
    entry = half_queue.get(chat_id)
    if not entry or not entry.files:
        await update.effective_message.reply_text("❓ No files are queued for printing.")
        return

    # Files in the half queue were always queued in half mode; ensure opts reflect that
    # even if the 30-minute TTL has since expired and get_print_options returned defaults.
    if opts.number_up != 2:
        opts = replace(opts, number_up=2)

    # ── Rate limiting ────────────────────────────────────────────────────────
    remaining = cooldown_remaining(chat_id)
    if remaining:
        await update.effective_message.reply_text(
            f"⏳ Please wait {remaining}s before printing. "
            f"Your {len(entry.files)} queued file(s) are ready — send `print` when the cooldown ends.",
            parse_mode="Markdown",
        )
        return

    files = list(entry.files)
    half_queue.pop(chat_id, None)
    last_print_time[chat_id] = time.monotonic()

    file_count = len(files)
    sheet_count = (file_count + 1) // 2

    await _print_and_reply(
        update,
        chat_id,
        files,
        opts,
        f"✅ Sent {file_count} file(s) to printer! "
        f"(~{sheet_count} sheet{'s' if sheet_count != 1 else ''})"
        "\n_(Half mode still active — send your next file when ready.)_",
    )


async def print_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming photo or document and send it to the printer."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    logger.info(
        "Received message from user_id=%s username=%s",
        user.id,
        user.username or "N/A",
    )

    opts = get_print_options(chat_id)

    # ── Half mode: queue files and auto-print every 2 ────────────────────────
    if opts.number_up == 2:
        file_info = await _get_file_info(update)
        if file_info is None:
            return

        file_obj, orig_ext = file_info

        # Half mode prints through a merged PDF, so reject anything unmergeable
        # before downloading it rather than silently printing it full-page.
        if orig_ext not in MERGEABLE_EXTENSIONS:
            await update.effective_message.reply_text(
                f"❌ `{orig_ext}` cannot be combined in half mode "
                f"(supported: {MERGEABLE_EXTENSIONS_DISPLAY}).\n"
                "Send `normal` first to print it as a full page.",
                parse_mode="Markdown",
            )
            return

        os.makedirs(DATA_DIR, exist_ok=True)
        file_path = os.path.join(DATA_DIR, f"{uuid.uuid4().hex}{orig_ext}")
        await file_obj.download_to_drive(file_path)
        logger.info("Half-mode: queued file at %s", file_path)

        entry = half_queue.setdefault(chat_id, HalfQueueEntry())
        entry.files.append(file_path)
        entry.ts = time.monotonic()

        file_count = len(entry.files)
        sheet_count = (file_count + 1) // 2

        if file_count % 2 == 1:
            # Odd count — waiting for a pairing file
            await update.effective_message.reply_text(
                f"📄 File {file_count} queued "
                f"(~{sheet_count} sheet{'s' if sheet_count != 1 else ''} so far). "
                f"Send another file to fill this sheet, or send `print` to print now.",
                parse_mode="Markdown",
            )
        else:
            # Even count — auto-flush the queue
            await _flush_half_queue(update, context, chat_id, opts)
        return

    # ── Normal mode ──────────────────────────────────────────────────────────
    remaining = cooldown_remaining(chat_id)
    if remaining:
        await update.effective_message.reply_text(
            f"⏳ Please wait {remaining}s before sending another print job."
        )
        return

    # Claim the rate-limit slot before yielding to the event loop so a second
    # concurrent message for the same chat cannot slip through the check above.
    last_print_time[chat_id] = time.monotonic()

    file_info = await _get_file_info(update)
    if file_info is None:
        # No valid file — not a real print attempt, release the rate-limit slot.
        last_print_time.pop(chat_id, None)
        return

    file_obj, orig_ext = file_info

    # Use a UUID-based filename to prevent collisions under concurrent prints
    os.makedirs(DATA_DIR, exist_ok=True)
    file_path = os.path.join(DATA_DIR, f"{uuid.uuid4().hex}{orig_ext}")
    await file_obj.download_to_drive(file_path)
    logger.info("File saved at %s", file_path)

    await _print_and_reply(update, chat_id, [file_path], opts, "✅ Sent to printer!")


# ---------------------------------------------------------------------------
# Core print logic
# ---------------------------------------------------------------------------

def merge_to_pdf(file_paths: list[str], output_path: str, pad_for_half: bool = False) -> None:
    """Merge images and PDFs into a single monolithic PDF document.

    If pad_for_half is True and exactly one logical page is produced, append a
    blank page of the same size so CUPS number-up=2 reliably places the content
    on half of a physical sheet (instead of some drivers scaling full-page).
    """
    writer = PdfWriter()
    first_page_width: float | None = None
    first_page_height: float | None = None

    def _add_pages_from_reader(reader: PdfReader) -> None:
        nonlocal first_page_width, first_page_height
        for page in reader.pages:
            if first_page_width is None or first_page_height is None:
                first_page_width = float(page.mediabox.width)
                first_page_height = float(page.mediabox.height)
            writer.add_page(page)

    for fp in file_paths:
        ext = os.path.splitext(fp)[1].lower()
        if ext in ('.jpg', '.jpeg', '.png', '.gif'):
            with Image.open(fp) as img:
                img_pdf = io.BytesIO()
                (img if img.mode == 'RGB' else img.convert('RGB')).save(img_pdf, format='PDF')
            img_pdf.seek(0)
            _add_pages_from_reader(PdfReader(img_pdf))
        elif ext == '.pdf':
            # add_page() clones eagerly, so the handle can close once the loop ends.
            with open(fp, 'rb') as pdf_f:
                _add_pages_from_reader(PdfReader(pdf_f))
        else:
            raise RuntimeError(f"Half mode merging is only supported for Images and PDFs. Found: {ext}")

    if pad_for_half and len(writer.pages) == 1 and first_page_width and first_page_height:
        writer.add_blank_page(width=first_page_width, height=first_page_height)

    with open(output_path, "wb") as f:
        writer.write(f)


async def print_file(file_paths: list[str], opts: PrintOptions) -> None:
    """Send one or more files to the printer using lp.

    In half mode the inputs are merged into a single PDF first: passing several
    files to one lp job makes CUPS place them on separate sheets rather than
    applying the number-up layout.

    Always passes -h <CUPS_SERVER> and -d <PRINTER_NAME> explicitly.
    Both environment variables are required — raises RuntimeError if missing.
    """
    if not LP_BIN:
        raise RuntimeError("lp command not found — is cups-client installed?")

    if not file_paths:
        raise RuntimeError("Internal error: print_file called with empty file list")

    merged_path = None
    print_paths = list(file_paths)
    if opts.number_up > 1:
        unmergeable = [
            os.path.basename(path)
            for path in file_paths
            if os.path.splitext(path)[1].lower() not in MERGEABLE_EXTENSIONS
        ]
        if unmergeable:
            raise RuntimeError(
                f"Half mode cannot combine: {', '.join(unmergeable)}. "
                f"Supported: {MERGEABLE_EXTENSIONS_DISPLAY}"
            )
        merged_path = os.path.join(DATA_DIR, f"{uuid.uuid4().hex}_merged.pdf")
        # For a single input in half mode, pad with a blank 2nd page so CUPS
        # consistently applies a true 2-up layout on one physical sheet.
        pad_for_half = len(file_paths) == 1
        await asyncio.to_thread(merge_to_pdf, file_paths, merged_path, pad_for_half)
        print_paths = [merged_path]

    server = get_cups_server()
    printer = get_printer_name()

    # Build: lp -h <server> -d <printer> -o fit-to-page -o media=<media> [options] <file(s)>
    cmd = [LP_BIN, "-h", server, "-d", printer, "-o", "fit-to-page", "-o", f"media={opts.media}"]

    if opts.number_up > 1:
        cmd += ["-o", f"number-up={opts.number_up}"]
    if not opts.color:
        # ColorModel=Gray is standard CUPS; CNColorMode=mono is Canon UFRII specific
        cmd += ["-o", "ColorModel=Gray", "-o", "CNColorMode=mono"]
    if opts.copies > 1:
        cmd += ["-n", str(opts.copies)]
    cmd.extend(print_paths)

    cmd_str = " ".join(cmd)
    logger.info("Shell command: %s", cmd_str)

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        except asyncio.TimeoutError:
            # Kill and reap the process to prevent zombie / fd leak
            process.kill()
            await process.wait()
            ex = RuntimeError("lp command timed out after 30 seconds")
            ex.cmd = cmd_str  # type: ignore[attr-defined]
            raise ex

        if process.returncode != 0:
            err_str = stderr.decode().strip()[:MAX_STDERR_LENGTH]
            logger.error(
                "lp failed (returncode=%s) stderr: %s",
                process.returncode,
                stderr.decode(),
            )
            ex = RuntimeError(err_str or "Print command failed")
            ex.cmd = cmd_str  # type: ignore[attr-defined]
            raise ex

        logger.info("lp stdout: %s", stdout.decode().strip())
    finally:
        if merged_path:
            try:
                os.remove(merged_path)
                logger.info("Cleaned up merged file %s", merged_path)
            except OSError as e:
                logger.warning("Could not remove merged file %s: %s", merged_path, e)


# ---------------------------------------------------------------------------
# Home Assistant integration
# ---------------------------------------------------------------------------

_ha_client: httpx.AsyncClient | None = None


def get_ha_client() -> httpx.AsyncClient:
    """Return a shared persistent AsyncClient for Home Assistant notifications."""
    global _ha_client
    if _ha_client is None or _ha_client.is_closed:
        _ha_client = httpx.AsyncClient(timeout=3)
    return _ha_client


async def notify_homeassistant(
    file_name: str, chat_id: int, copies: int, color: bool
) -> None:
    """Fire a Home Assistant event after a successful print (best-effort, non-blocking).

    Uses httpx (async) so the event loop is never stalled waiting for the HA response.
    """
    ha_url = os.getenv("HA_URL")
    ha_token = os.getenv("HA_TOKEN")
    if not ha_url or not ha_token:
        return

    url = f"{ha_url}/api/events/notanext_job_sent"
    payload = {
        "file_name": file_name,
        "chat_id": chat_id,
        "copies": copies,
        "color": color,
    }
    try:
        client = get_ha_client()
        response = await client.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {ha_token}"},
        )
        response.raise_for_status()
        logger.info("Home Assistant notified: %s", url)
    except Exception as e:
        logger.warning("Home Assistant notification failed: %s", e)


# ---------------------------------------------------------------------------
# Startup / lifecycle
# ---------------------------------------------------------------------------

async def post_init(application) -> None:
    """Register bot commands and start background tasks."""
    await application.bot.set_my_commands([
        BotCommand("start", "Show the welcome message"),
        BotCommand("help", "Show available commands"),
        BotCommand("preferences", "Set your default printing preferences"),
        BotCommand("status", "Check printer availability"),
        BotCommand("jobs", "Show the print queue"),
        BotCommand("cancel", "Cancel all print jobs"),
        BotCommand("clean", "Delete cached files (allowed users only)"),
    ])

    # Start periodic cleanup task — track it so exceptions are not silently lost
    task = asyncio.create_task(cleanup_task())
    task.add_done_callback(_on_cleanup_task_done)
    logger.info("Periodic cleanup task started (6h interval)")


async def post_shutdown(application) -> None:
    """Close the shared Home Assistant HTTP client on a clean shutdown."""
    global _ha_client
    if _ha_client is not None and not _ha_client.is_closed:
        await _ha_client.aclose()
    _ha_client = None


def _on_cleanup_task_done(task: asyncio.Task) -> None:
    """Log if the background cleanup task crashes unexpectedly."""
    if not task.cancelled() and task.exception():
        logger.error("Cleanup task crashed: %s", task.exception())


async def cleanup_task() -> None:
    """Background task: evict stale print options and remove leftover data files."""
    while True:
        await asyncio.sleep(6 * 3600)  # every 6 hours
        now = time.monotonic()

        # Evict expired print_options entries that were never consumed (leak prevention)
        expired_chats = [
            cid for cid, entry in print_options.items()
            if (now - entry.ts) >= SESSION_TTL
        ]
        for cid in expired_chats:
            print_options.pop(cid, None)
        if expired_chats:
            logger.info("Evicted %d expired print option(s).", len(expired_chats))

        # Evict stale last_print_time entries (prevents unbounded growth)
        stale_rate = [
            cid for cid, ts in last_print_time.items()
            if (now - ts) >= SESSION_TTL
        ]
        for cid in stale_rate:
            last_print_time.pop(cid, None)
        if stale_rate:
            logger.info("Evicted %d stale rate-limit entry(ies).", len(stale_rate))

        # Evict expired half-queue entries and delete their files
        expired_half = [
            cid for cid, entry in half_queue.items()
            if (now - entry.ts) >= SESSION_TTL
        ]
        for cid in expired_half:
            entry = half_queue.pop(cid, None)
            if entry:
                for fp in entry.files:
                    try:
                        os.remove(fp)
                        logger.info("Evicted stale half-queue file: %s", fp)
                    except OSError:
                        pass
        if expired_half:
            logger.info("Evicted %d expired half-queue(s).", len(expired_half))

        # Remove leftover downloaded files, but protect files still in an active queue
        logger.info("Running periodic data cleanup...")
        try:
            active_files: frozenset[str] = frozenset(
                fp
                for entry in half_queue.values()
                for fp in entry.files
            )
            removed = await perform_cleanup_async(active_files)
            logger.info("Periodic cleanup removed %d file(s).", removed)
        except Exception as e:
            logger.error("Periodic cleanup failed: %s", e)


def perform_cleanup(skip_paths: frozenset[str] | None = None) -> int:
    """Delete all cached files from the data directory. Returns count removed.

    skip_paths: Optional set of absolute file paths to preserve (e.g. active
    half-queue files that are still awaiting pairing).

    The persistent preferences file is always preserved regardless of skip_paths.

    Synchronous — safe to call at startup before the event loop starts.
    Use perform_cleanup_async() from async contexts.
    """
    skip_paths = (skip_paths or frozenset()) | {PREFERENCES_FILE}
    removed = 0
    try:
        with os.scandir(DATA_DIR) as entries:
            for entry in entries:
                if entry.path in skip_paths:
                    continue
                try:
                    if entry.is_file():
                        os.remove(entry.path)
                        removed += 1
                except OSError as e:
                    logger.error("Error removing %s: %s", entry.path, e)
    except FileNotFoundError:
        pass
    return removed


async def perform_cleanup_async(skip_paths: frozenset[str] | None = None) -> int:
    """Async wrapper for perform_cleanup — offloads blocking I/O to a thread pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, perform_cleanup, skip_paths)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    token = os.getenv("TOKEN")
    if not token:
        logger.error("TOKEN environment variable is not set")
        raise SystemExit("TOKEN environment variable is required")

    allowed_chat_ids = get_allowed_chat_ids()

    # Log configuration at startup for easy debugging
    logger.info("NotaNext v%s starting...", VERSION)
    logger.info(
        "Configuration: CUPS_SERVER=%s  PRINTER_NAME=%s",
        os.getenv("CUPS_SERVER", "(not set)"),
        os.getenv("PRINTER_NAME", "(not set)"),
    )
    logger.info("Allowed chat IDs: %s", allowed_chat_ids or "ALL")

    # Warn early if any CUPS binary is missing (won't prevent startup but commands will fail)
    if not LP_BIN:
        logger.warning("lp binary not found — printing will not work.")
    if not LPSTAT_BIN:
        logger.warning("lpstat binary not found — /status and /jobs will not work.")
    if not CANCEL_BIN:
        logger.warning("cancel binary not found — /cancel will not work.")

    # Clean up any stale files from a previous run (sync — before event loop starts)
    logger.info("Performing startup cleanup...")
    perform_cleanup()

    # Load persistent per-chat preferences (sync — before event loop starts)
    load_preferences()

    application = (
        ApplicationBuilder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    if allowed_chat_ids:
        chat_id_filter = filters.Chat(chat_id=allowed_chat_ids)
    else:
        logger.warning(
            "ALLOWED_CHAT_IDS is not set — all Telegram users can print. "
            "Set this variable to restrict access."
        )
        chat_id_filter = filters.ALL

    # Preference-setting wizard — handles /start and /preferences.
    # Both entry points are chat-filtered: saved profiles are a capped resource
    # (MAX_PREFERENCES), so unrestricted access would let strangers exhaust it.
    pref_conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start, filters=chat_id_filter),
            CommandHandler("preferences", preferences_command, filters=chat_id_filter),
        ],
        states={
            PREF_COLOR: [CallbackQueryHandler(pref_color_callback, pattern="^pref_color_")],
            PREF_MODE: [CallbackQueryHandler(pref_mode_callback, pattern="^pref_mode_")],
            PREF_PAPER: [CallbackQueryHandler(pref_paper_callback, pattern="^pref_paper_")],
        },
        fallbacks=[CommandHandler("cancel", cancel_preferences)],
    )
    application.add_handler(pref_conv)

    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status))

    # Restricted commands and message handlers
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
            handle_text_message,
        )
    )

    application.run_polling()


if __name__ == "__main__":
    main()
