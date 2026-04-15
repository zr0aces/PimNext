# Changelog

All notable changes to this project will be documented in this file.

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
