# Changelog

All notable changes to this project will be documented in this file.

## [1.2.0] – 2026-08-22

### Added
- **`1x` copy option** — explicitly resets the copy count to one. Previously only `2x`–`4x` existed, so there was no way to undo a multi-copy setting without waiting out the 30-minute session.
- **Blank-page padding in half mode** — a single file printed with `half` now gets a blank second page appended to the merged PDF, so CUPS applies a true 2-up layout on one physical sheet instead of letting the driver scale the content to a full page.
- **`test_bot.py`** — a dependency-free self-check (`python3 test_bot.py`) covering option parsing, the preference persistence round-trip, the half-mode extension gate, and version consistency between `bot.py`, `docker-compose.yml`, `README.md` and this changelog. It also covers the shared cooldown helper and asserts the preference cap is a constant rather than an environment variable.

### Fixed — Security
- **`/start` and `/preferences` were unrestricted.** The preference wizard's entry points carried no chat filter, so any Telegram user could reach it and consume one of the `MAX_PREFERENCES` slots (default 10) — locking legitimate users out with "the preference store is full". Both entry points now use the same `ALLOWED_CHAT_IDS` filter as `/jobs`, `/cancel`, `/clean`, and the file handlers.

### Fixed
- **Half mode silently ignored unmergeable file types.** `.txt`, `.doc`, `.docx`, `.odt` and `.ps` are printable but cannot be merged into a PDF, so a `.docx` sent in half mode fell back to a plain multi-file `lp` job and printed full-page — contradicting the 1.1.1 entry below. Such files are now rejected at queue time with a message pointing at `normal` mode, and `print_file` raises rather than falling through. The mergeable set is a module constant (`MERGEABLE_EXTENSIONS`) instead of being redefined inside `print_file`.
- **`docker-compose.yml` overrode `.env`.** `CUPS_SERVER`, `PRINTER_NAME` and `TZ` were hardcoded under `environment:`, which takes precedence over `env_file:` — so following the README's "fill in TOKEN, CUPS_SERVER, PRINTER_NAME" had no effect on the Compose path. The block is removed; all configuration now comes from `.env`.
- **`.dockerignore` referenced `pimnext.service`**, a filename that has not existed since the 1.1.1 rename. The rule was dead and the systemd unit was being copied into the image; it now correctly excludes `notanext.service`.
- **`/cancel` collision is no longer silent.** The command is bound both as the wizard's fallback and as the print-queue canceller; while the wizard is open the fallback wins. The cancellation reply now says to send `/cancel` again to reach the print-queue meaning.
- **README corrections** — the print-options table was missing `color`, `1x`, `2up`, `normal`, `full`, `single`, `1up` and `print`; the `half` row described "A5 content on A4 paper" when `half` actually sets `number-up=2` and queues files; and the session-expiry note claimed options "reset to defaults (colour, 1 copy, A4)" when they have fallen back to the chat's *saved* defaults since 1.1.2.

### Changed
- **`PrintOptions` and `HalfQueueEntry` dataclasses** replace the bare dicts that were passed between the wizard, the session store, the persistence layer, the half-mode queue and `print_file`. `print_file(paths, opts)` takes one object instead of four positional parameters, and the colour/sheet-mode labels used across every reply live on the type as properties.
- **Shared CUPS query helper** — `/status`, `/jobs` and `/cancel` were three copies of the same binary check, `-h <server>` wiring, timeout handling and stderr truncation; they now share `run_cups_query()`. Their failure messages are correspondingly more uniform.
- **Shared print helper** — the normal and half-mode paths were two copies of the same print → notify → reply → clean-up block, so a fix to one could miss the other. Both now call `_print_and_reply()`.
- **Reusable HTTP client & pre-built keyboards** — Home Assistant notification reuses a persistent `httpx.AsyncClient` connection pool, and inline keyboards are instantiated once at module load to reduce allocations and resource consumption.
- **`set_print_options` renamed to `handle_text_message`** — it is the text handler, and it also flushes the print queue, so the old name described neither. Keyword parsing is split out into the pure `parse_option_tokens()`, which is what the new test exercises.
- **`print_file` no longer returns its command string** — no caller used it. The string is still attached to raised errors as `.cmd` for the Telegram reply.
- **Reusable HTTP client and pre-built keyboards** — Home Assistant notifications now share one persistent `httpx.AsyncClient` (and its connection pool) instead of building and tearing one down per print, and the three wizard keyboards are constructed once at import instead of on every callback.
- **Shared cooldown helper** — the normal and half-mode paths each carried their own copy of the elapsed/remaining rate-limit arithmetic; both now call `cooldown_remaining()`.
- **`perform_cleanup()` uses `os.scandir()`** instead of `os.listdir()` plus a per-entry `os.path.isfile()` stat.
- **`merge_to_pdf()` no longer leaks a file handle per PDF input.** `PdfReader(path)` kept the handle open for the process's lifetime; the reader now runs inside a `with open(...)` block, which is safe because `PdfWriter.add_page()` clones eagerly. The image branch also stopped rebinding the name bound by `with Image.open(...)`, which read as if the converted copy were the one being closed.
- **The shared Home Assistant client is closed on shutdown** via a `post_shutdown` hook, instead of leaving its connection pool to process exit.

### Removed
- **`MAX_PREFERENCES` and `DOCKER_IMAGE` environment variables.** The preference cap is a safety bound, not a tuning knob — it is now the module constant `MAX_PREFERENCES = 10`, and the `get_preferences_limit()` parser, its validation warning and the "raise the limit" hint in the full-store reply are gone. `DOCKER_IMAGE` only wrapped `docker-compose.yml`'s `image:` in a `${VAR:-default}`; the tag is now written directly, and `docker compose up -d --build` still builds it locally.
- **`PRINT_OPTIONS_TTL` and `HALF_QUEUE_TTL`** — both had become bare aliases of `SESSION_TTL` with no remaining references.
- **The `LOG_LEVEL` validation block** collapses to one `getattr` with a fallback. An unrecognised value still defaults to `INFO`, but no longer prints a warning to stderr before logging is configured.

---

## [1.1.3] – 2026-04-23

### Changed
- Version bump to `v1.1.3` across runtime constant, Docker tag defaults, and documentation references.

## [1.1.2] – 2026-04-23

### Added
- **Persistent default print preferences** via `/preferences` (and `/start` wizard): users can now save per-chat defaults for color mode, sheet mode (normal/half), and paper size (A4/A5).

### Changed
- **Telegram command registration updated**: bot command menu is now kept in sync at startup using `set_my_commands`, including `/preferences` in the registered command list.
- **Documentation refresh**: README command table and usage sections now document `/preferences`, default-vs-temporary option behavior, and command menu registration.

## [1.1.1] – 2026-04-16

### Changed
- **Project renamed from PimNext to NotaNext**: updated all references across the codebase and configuration files.

### Fixed
- **Half mode multi-file printing**: Files sent in half mode are now mathematically merged into a single PDF before passing to CUPS. This reliably forces CUPS to place exactly 2 distinct submissions side-by-side on exactly half of one physical sheet. Previously, CUPS would automatically separate them and place them on individual pages.

---

## [1.1.0] – 2026-04-16

### Changed
- **Project renamed from PrintBot to PimNext**: updated all references across `README.md`, `docker-compose.yml`, `Dockerfile`, `.env.example`, `bot.py`, and the systemd unit file.
- **Docker image**: new image path is `ghcr.io/zr0aces/pimnext` (previously `ghcr.io/zr0aces/printbot`).
- **Systemd unit**: `printbot.service` renamed to `pimnext.service`; default working directory updated to `/home/pi/pimnext`.
- **Home Assistant event**: the webhook event fired after each successful print is now `pimnext_job_sent` (previously `printbot_job_sent`). Update any existing HA automations accordingly.
- **Logger name**: internal Python logger renamed from `printbot` to `pimnext`.
- **Bot messages**: welcome and help messages updated to reference PimNext.

---

## [1.0.9] – 2026-04-15

### Fixed
- **Grayscale printing on Canon printers**: The bot now sends both standard `ColorModel=Gray` and Canon-proprietary `CNColorMode=mono` flags. This ensures grayscale printing works correctly on Canon UFRII LT printers (like the LBP7110Cw) which ignore standard CUPS grayscale commands.

---

## [1.0.8] – 2026-04-15

### Added
- Added support for A5 printing (`a5` option) and printing A5 content on A4 paper (`half` option).
- Print settings (color, copies, paper size) now persist for 30 minutes instead of resetting after the first print.
- Added `a4` option to explicitly reset paper size to A4.

---

## [1.0.7] – 2026-04-15

### Fixed
- **`asyncio.get_event_loop()` deprecation**: `perform_cleanup_async()` now uses `asyncio.get_running_loop()` (introduced in Python 3.7, preferred from 3.10+), eliminating the `DeprecationWarning` emitted by newer Python runtimes when `get_event_loop()` is called with no running loop.

### Improved
- **Configurable Docker image via environment variable**: `docker-compose.yml` now reads the image name from the `DOCKER_IMAGE` env var (default: `ghcr.io/zr0aces/printbot:1.0.7`). This makes it easy to switch between registry, local, and custom builds without editing the Compose file. Set `DOCKER_IMAGE=local/printbot:dev` in `.env` to use a locally built image.

---

## [1.0.6] – 2026-04-15

### Fixed — Bugs
- **Subprocess leak on timeout** (`run_cups_command`): child process is now killed and reaped with `process.kill()` + `await process.wait()` before `TimeoutError` is re-raised, preventing zombie processes and open-pipe fd leaks.
- **Subprocess leak on timeout** (`print_file`): same kill/wait fix applied to the `lp` subprocess — previously the process was abandoned silently on the 30-second timeout.
- **Filename collision under concurrent prints**: switched from `int(time.time())` (1-second resolution) to `uuid.uuid4().hex` — eliminates race conditions when two users send a file in the same second.
- **Home Assistant notify in `finally` block**: moved `notify_homeassistant()` into the `try` block (after confirmed print, before the Telegram reply) to make the intent explicit and remove dependency on the `success` flag.
- **Silent `LOG_LEVEL` fallback**: invalid values (e.g. `VERBOSE`) now print a warning before defaulting to `INFO` instead of failing silently.
- **CUPS HTTP probe unreliable**: `docker-entrypoint.sh` now uses a raw TCP connect (`/dev/tcp/$CUPS_SERVER/631`) instead of `curl http://...`, which works even when the CUPS web UI is disabled.

### Fixed — Security
- **No file size limit**: file size is checked against `MAX_FILE_BYTES` (20 MB) from message metadata _before_ `download_to_drive` is called — prevents disk fill attacks.
- **No rate limiting**: added `PRINT_COOLDOWN = 10` seconds per chat with a `last_print_time` dict; users who send too fast receive a friendly message with the remaining wait time.
- **No file type whitelist**: documents are now validated against `PRINTABLE_EXTENSIONS` (`{.pdf, .ps, .jpg, .jpeg, .png, .gif, .txt, .doc, .docx, .odt}`) before download.
- **Blocking HTTP on event loop**: `notify_homeassistant` was using `urllib.request.urlopen` (a blocking call that stalled the entire event loop up to 3 s). Replaced with `async def` + `httpx.AsyncClient`.
- **`.env` file permissions**: added `chmod 600 .env` to the setup instructions in `README.md`.

### Improved — Performance
- **Cached `shutil.which` calls**: `LP_BIN`, `LPSTAT_BIN`, and `CANCEL_BIN` are now resolved once at import time instead of on every command invocation.
- **`print_options` memory leak**: `cleanup_task` now sweeps and evicts expired entries that were set but never consumed (e.g. user sets `bw` and never sends a file).
- **Blocking I/O in async context**: `perform_cleanup` (uses `os.listdir` / `os.remove`) is no longer called directly from async handlers. A new `perform_cleanup_async()` wrapper offloads the work to a thread pool via `run_in_executor`.

### Improved — Code Quality
- **Type hints on all handler functions**: every command and message handler now carries `(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None` signatures.
- **`docker-compose.yml` `image:` tag**: added `image: ghcr.io/zr0aces/printbot:1.0.6` alongside `build:` so `docker compose up -d` pulls the pre-built image without requiring a local build.
- **Docker log rotation**: added `logging: driver: json-file, max-size: 10m, max-file: 3` to prevent unbounded log growth on long-running hosts.
- **`docker-entrypoint.sh` shell strictness**: changed to `set -euo pipefail` to catch undefined variable references and pipeline failures.
- **`printbot.service` hardening**: added `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, `ReadWritePaths`, and `ProtectHome` systemd directives.
- **`requirements.txt`**: added `httpx` as an explicit direct dependency (was previously only a transitive dep of `python-telegram-bot`).
- **Supply-chain guidance**: added `pip-compile --generate-hashes` instructions to `README.md` Tech Stack section.

---

## [1.0.5] – 2026-04-15

### Changed
- Bumped `VERSION` constant in `bot.py` to `1.0.5`
- Updated `docker-compose.yml` build arg to `1.0.5`
- Updated `README.md`: corrected Docker base image reference (`ubuntu:22.04`), updated pinned pull example to `1.0.5`, refreshed troubleshooting table
- Updated `.env.example`: improved inline comments and ensured all variables are documented
- Updated `.gitignore`: added `.DS_Store`, `*.egg-info/`, `dist/`, and `*.log` patterns
- Updated `.dockerignore`: added `*.md` exclusion for `CONTRIBUTING.md` and improved comments

---

## [1.0.4] – 2026-04-14

### Added
- Pre-built multi-arch Docker images published to GitHub Container Registry (`ghcr.io/zr0aces/printbot`) — no local build required
- Health check improvements: `HEALTHCHECK` in `Dockerfile` now uses `pgrep -f bot.py`

### Changed
- `LOG_LEVEL` environment variable supported — set to `DEBUG`, `INFO`, `WARNING`, etc. (default: `INFO`)
- Noisy `httpx` and `telegram` library loggers suppressed to `WARNING` level at startup
- `docker-entrypoint.sh` startup wait loop now retries up to 10 times with 2-second intervals before proceeding

---

## [1.0.3] – 2026-04-13

### Added
- All CUPS commands (`lp`, `lpstat`, `cancel`) now explicitly pass `-h <CUPS_SERVER>` for reliable remote server targeting
- `print_file()` explicitly passes `-d <PRINTER_NAME>` to `lp` — eliminates "No default destination" errors
- `print_msg()` attaches the shell command string to `RuntimeError` for inclusion in Telegram error replies
- `run_cups_command()` helper extracts shared async subprocess logic for `status`, `jobs`, and `cancel` handlers
- Async timeout (30 s) on the `lp` subprocess inside `print_file()`

### Changed
- `CUPS_SERVER` and `PRINTER_NAME` are now **required** at runtime; missing values raise `RuntimeError` with a clear message
- `get_cups_server()` and `get_printer_name()` helper functions added for centralised env-var access
- Error replies in Telegram now include truncated CUPS `stderr` (max 300 chars) via `MAX_STDERR_LENGTH` constant
- `requirements.txt` pinned to `python-telegram-bot==22.7` (CUPS 2.4.1 compatible)

---

## [1.0.2] – 2026-04-14

### Fixed
- Corrected Canon PPD filename to `CNRCUPSLBP7110CZNK.ppd` across `Dockerfile`, `docker-compose.yml`, `docker-entrypoint.sh`, and `README.md`
- `PRINTER_NAME` default aligned to `Canon_LBP7110Cw` in all configuration files and examples

---

## [1.0.1] – 2026-04-14

### Added
- `docker-entrypoint.sh`: dynamically writes `/etc/cups/client.conf` (`ServerName`) from `CUPS_SERVER` env var
- `docker-entrypoint.sh`: waits for CUPS server reachability (HTTP probe on port `631`) before setting the default printer
- `lpoptions -d <PRINTER_NAME>` call in entrypoint to set a system-wide default printer inside the container

### Changed
- `docker-entrypoint.sh` falls back to `cups` if `CUPS_SERVER` is not set

---

## [1.0.0] – 2026-04-11

### Added
- Print options via message prefix (`bw`, `gray`, `2x`–`4x`) — set per chat, applied to next file, then reset
- `/jobs` command — shows the current CUPS print queue via `lpstat -o`
- `/cancel` command — cancels all pending jobs via `cancel -a`
- Home Assistant webhook integration — fires `printbot_job_sent` event after each successful print (`HA_URL` + `HA_TOKEN` env vars)
- `VERSION` constant in `bot.py`, logged at startup
- OCI image labels in `Dockerfile` (`org.opencontainers.image.*`)
- `.dockerignore` to minimise image build context
- GitHub Actions workflow to build and push multi-arch Docker images on version tags
- `CHANGELOG.md`

### Improved
- Print errors now include CUPS stderr output in the Telegram reply
- Auto-cleanup: downloaded file is removed immediately after a successful print
- `HELP_TEXT` updated to document all commands and print options
- `docker-compose.yml` uses a versioned image name

### Removed
- `Pipfile` (redundant alongside `requirements.txt`)
- `.vscode/settings.json` (machine-specific local path)

### Initial feature set
- Telegram bot using python-telegram-bot v22.7
- CUPS integration via `lp` subprocess
- Access control via `ALLOWED_CHAT_IDS` (numeric chat IDs)
- `/start`, `/help`, `/status`, `/clean` commands
- Docker Compose and systemd deployment options
