# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

NotaNext is a Telegram bot that forwards photos and documents to a CUPS-connected printer. Whole application is a single file, `bot.py`, built on `python-telegram-bot` 22.7 in long-polling mode. Everything else in the repo is deployment wrapping (Docker, systemd, GitHub Actions).

## Commands

```bash
pip install -r requirements.txt
cp .env.example .env          # TOKEN, CUPS_SERVER, PRINTER_NAME are required
chmod 600 .env
python3 bot.py                # run directly
```

```bash
docker compose up -d          # pulls ghcr.io/zr0aces/notanext:<version>
docker compose up -d --build  # build locally instead
docker compose logs -f notanext
```

```bash
python3 test_bot.py           # self-check — needs no installed dependencies
```

`test_bot.py` stubs `telegram`, `PIL`, `pypdf` and `httpx` in `sys.modules` before importing `bot`, so it runs on a bare interpreter. It covers only the pure logic: `parse_option_tokens`, the `PrintOptions` persistence round-trip, the half-mode extension gate, `cooldown_remaining()`, the preference cap being a constant, and version consistency across all four files. Anything touching Telegram, CUPS, or the filesystem is out of its reach — keep it that way, and keep new pure logic testable from it. There is no pytest, no linter, and no formatter config in this repo; do not invent commands for them. The only CI is `.github/workflows/docker-release.yml`, which builds multi-arch images on `v*.*.*` tags — nothing runs on push or PR.

CUPS-side debugging (needs `cups-client` installed):

```bash
lpstat -v -h "$CUPS_SERVER"   # list printers — confirms PRINTER_NAME
lpstat -p -h "$CUPS_SERVER"   # printer state, same call /status makes
```

## Architecture

### `PrintOptions` is the unit of settings

One dataclass carries a chat's four print settings (`color`, `copies`, `media`, `number_up`) plus `ts`, the monotonic timestamp of the temporary session. `ts` is deliberately excluded from `to_dict()` — it must never reach disk. Colour and sheet-mode labels are properties on the type, so every user-facing reply words them identically.

It flows through the wizard draft (`context.user_data["pref_draft"]`), the session store, the persistence layer, and `print_file(paths, opts)`. Do not go back to passing the fields individually.

### Chat state lives in four module-level dicts

All keyed by `chat_id`, all in-memory except the last:

| Dict | Lifetime | Role |
|---|---|---|
| `print_options` | `SESSION_TTL` (1800s) **sliding** — `get_print_options()` refreshes `ts` on every read | temporary per-session overrides |
| `last_print_time` | swept at `SESSION_TTL` | 10s (`PRINT_COOLDOWN`) per-chat rate limit, read through `cooldown_remaining()` |
| `half_queue` | `SESSION_TTL` | `HalfQueueEntry` (files + `ts`) awaiting a pairing file |
| `user_preferences` | persistent, keyed `str(chat_id)` | saved profiles, `data/preferences.json` |

Settings resolve in this order: active session options → saved defaults → `PrintOptions()`. `pref_paper_callback` deliberately writes *both* `user_preferences` and `print_options[chat_id]` — without that, a stale 30-minute override keeps shadowing defaults the user just saved.

`get_print_options()` returns the **live** session object, so callers that intend to modify it must `replace()` it first — `handle_text_message` does, so a message with an unknown token cannot leave half-applied settings behind.

Anything you add that mutates chat state must also get an eviction path in `cleanup_task()`, or it leaks for the process's lifetime.

### The print path forks on `number_up`

`print_msg()` is the single entry point for photos and documents:

- **Half mode (`number_up == 2`)** — reject anything outside `MERGEABLE_EXTENSIONS` *before downloading*, then queue, then reply on odd counts and call `_flush_half_queue()` on even counts. The rate-limit check lives inside the flush, and a rate-limited flush leaves the queue intact so the user can retry with the `print` keyword.
- **Normal mode** — rate-limit first, and **claim the slot before the first `await`** so two concurrent messages from one chat cannot both pass the check. The slot is released if `_get_file_info()` returns `None` (not a real print attempt).

Both paths converge on `_print_and_reply()`, which owns print → notify Home Assistant → reply → delete the temporary files. Both also read the rate limit through `cooldown_remaining()` rather than repeating the elapsed/remaining arithmetic. These were once two hand-copied blocks; keep them merged so a fix to one cannot miss the other.

`print_file()` builds the `lp` invocation:

```
lp -h $CUPS_SERVER -d $PRINTER_NAME -o fit-to-page -o media=A4|A5 \
   [-o number-up=2] [-o ColorModel=Gray -o CNColorMode=mono] [-n copies] file...
```

Two non-obvious behaviours here, both hard-won (see `docs/CHANGELOG.md`):

- **Half mode always merges first.** Passing multiple files to `lp` with `number-up=2` does not work — CUPS splits them onto separate sheets. `merge_to_pdf()` (run via `asyncio.to_thread`) builds one PDF. Only images and PDFs can be merged, which is why `MERGEABLE_EXTENSIONS` is narrower than `PRINTABLE_EXTENSIONS`; unmergeable input is rejected at queue time, and `print_file` raises as a backstop rather than silently printing full-page. A *single* file in half mode gets a blank padded second page, otherwise drivers scale it to a full page.
- **Grayscale needs both flags.** Canon UFRII LT printers (the LBP7110Cw this was built for) ignore standard `ColorModel=Gray`; `CNColorMode=mono` is what actually takes effect. Keep both.

### Subprocess and I/O discipline

Every CUPS call goes through `create_subprocess_exec` with an argument list — never a shell string. `run_cups_command()` and `print_file()` both do `kill()` + `await wait()` on timeout to reap the child. The Home Assistant webhook reuses one module-level `httpx.AsyncClient` (`get_ha_client()`), closed by the `post_shutdown` hook; `merge_to_pdf()` opens PDF inputs inside a `with` block because `PdfWriter.add_page()` clones eagerly. `/status`, `/jobs` and `/cancel` share `run_cups_query()`, which returns `(stdout, error_message)` with exactly one set; it owns the binary check, the `-h <server>` wiring, the timeout, and stderr truncation. `LP_BIN` / `LPSTAT_BIN` / `CANCEL_BIN` are resolved by `shutil.which` once at import.

Blocking filesystem work never runs directly in a handler — `perform_cleanup_async()` wraps `perform_cleanup()` in an executor. The sync version exists only for the pre-event-loop startup call in `main()`.

Temporary files are cleaned in two places: the merged PDF in `print_file`'s `finally`, the downloaded originals in `_print_and_reply`'s. `perform_cleanup()` always preserves `PREFERENCES_FILE`, and the periodic sweep also preserves files still sitting in an active `half_queue`.

### Handler registration order matters

`main()` computes `chat_id_filter` first — it degrades to `filters.ALL` when `ALLOWED_CHAT_IDS` is unset, which is the open-to-the-world configuration and logs a warning at startup. Then it registers: the preferences `ConversationHandler`, `/help` and `/status`, the chat-filtered commands, and the two `MessageHandler`s. `post_init` registers the bot command list and starts `cleanup_task()`; `post_shutdown` closes the shared HTTP client.

- Only `/help` and `/status` are unfiltered. **`/start` and `/preferences` are chat-filtered on purpose** — saved profiles are a capped resource (the `MAX_PREFERENCES` constant, 10), so an open wizard lets strangers exhaust it and lock out real users. Do not remove those filters.
- `/cancel` is bound twice: as a `ConversationHandler` fallback (aborts the wizard) and as a top-level command (cancels CUPS jobs). Inside the wizard the fallback wins, so `cancel_preferences` tells the user to send it again for the print-queue meaning.
- The TEXT handler catches every non-command message, so unrecognised text always produces the "Unknown option" reply. `handle_text_message()` treats `print` as an action keyword (flush the half queue) rather than a setting, and delegates keyword parsing to the pure `parse_option_tokens()` — put new keywords there, and add them to the `test_bot.py` documented-keyword list, `HELP_TEXT`, and the README table together.

## Release

`VERSION` is duplicated in four places and a bump must touch all of them: `bot.py` (`VERSION` constant), `docker-compose.yml` (both the `image:` tag and the `VERSION` build arg), the pinned-pull example in `README.md`, and a new entry in `docs/CHANGELOG.md`. `test_bot.py::test_version_matches_changelog` checks all four. Pushing a `v*.*.*` tag is what publishes the image.

The changelog is the project's design record — half-mode merging, the Canon color flags, the subprocess-leak fixes, and the rate-limit rationale are all documented there. Read it before changing print behaviour.

## Deployment notes

`docker-entrypoint.sh` writes `/etc/cups/client.conf` from `CUPS_SERVER`, then TCP-probes port 631 up to ten times (raw `/dev/tcp`, not HTTP — the CUPS web UI is often disabled on headless servers) before calling `lpoptions -d`. The compose file mounts `./data:/app/data`, which is what makes `preferences.json` survive container recreation.

Configuration is deliberately small: `TOKEN`, `CUPS_SERVER`, `PRINTER_NAME`, `ALLOWED_CHAT_IDS`, `LOG_LEVEL`, `TZ`, and the `HA_URL`/`HA_TOKEN` pair. That is the whole surface. The preference cap (`MAX_PREFERENCES`) and the image tag are constants in `bot.py` and `docker-compose.yml`, not environment variables, because they are bounds rather than deployment settings — don't reintroduce a variable for a value that has one correct answer.

`docker-compose.yml` deliberately has **no `environment:` block** — it once hardcoded `CUPS_SERVER`, `PRINTER_NAME` and `TZ`, which silently overrode `env_file:` and made editing `.env` a no-op. All configuration comes from `.env`.

`notanext.service` runs under `ProtectSystem=strict` with `ReadWritePaths=/home/pi/notanext/data`. Any new write path outside `data/` will fail silently under systemd even though it works in Docker.
