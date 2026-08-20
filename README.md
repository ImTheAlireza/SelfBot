<div align="center">

# SelfBot

**An asynchronous, plugin-based Telegram self-bot built on Telethon.**

[![Tests](https://img.shields.io/badge/tests-passing-brightgreen)](tests/)
[![Ruff](https://img.shields.io/badge/lint-ruff%20clean-brightgreen)](pyproject.toml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

</div>

> [!WARNING]
> **Self-bots violate Telegram's Terms of Service.** Automating a user account
> can get it limited or permanently banned. This project is for education and
> personal experimentation. You accept all risk.

---

## What it does

44 commands for AI, file manipulation, timers, stickers, QR codes, weather,
dictionaries and chat automation — all driven from your own Telegram account by
typing commands into any chat.

| | |
|---|---|
| 🧠 **AI** | Ask GPT-4o (or any AnyAPI model) with `gpt <prompt>`. |
| 📁 **Files** | Zip/unzip with AES passwords, batch queues, rename, audio tag editing, PDF page extraction. |
| ⏰ **Timers** | Live-updating countdowns that survive restarts. |
| 🎨 **Stickers** | Render text to stickers and manage packs via a helper bot. |
| 🔲 **Utilities** | QR generate/decode, text→PDF (English + Persian), weather, dictionary with audio, IRR exchange rates. |
| ⚡ **Automation** | Quick-reply shortcuts, per-channel auto-reactions, per-chat auto-replies, per-chat welcome messages, controlled bulk deletion. |

---

## Quick start

> **First time here?** [`SETUP.md`](SETUP.md) walks through everything step by
> step, including rotating the credentials leaked by v1.

```bash
git clone https://github.com/ImTheAlireza/SelfBot.git
cd SelfBot

python -m venv .venv && source .venv/bin/activate
pip install -e ".[full]"

cp .env.example .env
$EDITOR .env             # add TELEGRAM_API_ID and TELEGRAM_API_HASH

python -m selfbot --login   # sign in; fills in SUDO_USER_ID for you
python -m selfbot           # start the bot
```

Get `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` from [my.telegram.org](https://my.telegram.org)
→ *API development tools*. `--login` discovers your user ID and writes
`SUDO_USER_ID` into `.env`, so you never have to look it up.

Verify anytime with `python -m selfbot --check`, which validates the config and
command registry without connecting.

### Docker

```bash
cp .env.example .env && $EDITOR .env
docker compose run --rm selfbot   # interactive first login
docker compose up -d              # then run detached
```

The `selfbot-data` volume holds your `.session` file and database. **Back it
up** — losing it means re-authenticating.

---

## Configuration

Telegram, storage, logging, and process settings are environment-driven. See
[`.env.example`](.env.example) for the full annotated list.

| Variable | Required | Default | Purpose |
|---|:---:|---|---|
| `TELEGRAM_API_ID` | ✅ | — | From my.telegram.org |
| `TELEGRAM_API_HASH` | ✅ | — | From my.telegram.org |
| `SUDO_USER_ID` | ✅ | — | Your user ID; grants owner rights |
| `DATABASE_URL` | | `sqlite+aiosqlite:///./data/selfbot.db` | SQLite or MySQL |
| `COMMAND_PREFIX` | | *(none)* | Set to `.` to require `.help` |
| `STARTUP_NOTIFY` | | `me` | Online message target: `me`, `off`, or a chat ID |
| `LOG_CHANNEL_ID` | | — | Mirror warnings/errors to a private channel |
| `ANYAPI_KEY` | | — | AnyAPI key for the `gpt` command (anyapi.ai) |
| `SUPERVISOR_PROCESS` | | `selfbot` | Enables supervisor-backed status and logs |
| `MAX_FILE_SIZE_MB` | | `512` | Ceiling on downloads and uploads |

<details>
<summary><b>Using MySQL instead of SQLite</b></summary>

```bash
pip install -e ".[full,mysql]"
```
```env
DATABASE_URL=mysql+aiomysql://user:password@localhost/selfbot
```
Tables are created automatically on first run.
</details>

---

## Commands

Commands are typed as plain messages from your own account. Set
`COMMAND_PREFIX=.` if you'd rather write `.help`.

### Core
| Command | Description |
|---|---|
| `help [command]` | Command list, or detail for one command |
| `ping` | Round-trip latency |
| `whoami` | Your user ID, role and the current chat ID |
| `status` | Uptime and runtime counters |
| `self on\|off\|restart\|status\|logs\|diag` | 👑 Process control and supervisor troubleshooting |

`self restart` directly replaces the current Python process while preserving
its PID and environment. It deliberately does not call `supervisorctl restart`,
which can deadlock when invoked by the process being restarted.

### Admin 👑
| Command | Description |
|---|---|
| `setadmin <id\|@user>` | Let another user run commands |
| `remadmin <id>` | Revoke access |
| `adminlist` | List authorised users |

### AI
| Command | Description |
|---|---|
| `gpt <prompt>` | Ask any AnyAPI model (default `anthropic/claude-sonnet-5`) |

Set `ANYAPI_KEY` in `.env` to enable the `gpt` command (OpenAI-compatible,
`https://api.anyapi.ai/v1`). Pick the model with `ANYAPI_MODEL` — default
`anthropic/claude-sonnet-5`. If AnyAPI is rate-limited or unavailable and a legacy
`RAPIDAPI_KEY` is still configured, the command retries through RapidAPI.

### Messaging
| Command | Description |
|---|---|
| `spam <message> <count>` | Repeat a message, rate-limit aware |
| `cancel` | Stop your running spam task |
| `del <count\|type> [-me]` | 👑 Delete messages in the current chat; `-me` limits it to yours |
| `info [user]` | User details and profile photo (reply, mention, or yourself) |
| `qreply set\|remove\|list\|info` | Manage `-alias` shortcuts |
| `-<alias>` | Expand a quick reply in place |

`del` always operates on the chat where the command was sent and can never
select another chat. Without `-me`, it targets messages from everyone that your
account is allowed to delete. Append `-me` to target only your messages, for
example `del 400 -me`, `del photos -me`, or `del all -me`.

Types: `photos`, `videos`, `voices`, `videomsgs`, `musics`, `files`, `stickers`,
`gifs`, `links`, `all`.

### Files
| Command | Description |
|---|---|
| `zip [password]` | Compress a replied file (AES when a password is given) |
| `unzip [password]` | Extract an archive — traversal and zip-bomb safe |
| `add` / `zipqueue` / `zipclear` / `zipit [password]` | Batch multiple files into one archive |
| `rename <name>` | Re-upload under a new name |
| `metadata <title> - <artist>` | Rewrite audio tags |
| `split <start>-<end>` | Extract a PDF page range |

### Timers
| Command | Description |
|---|---|
| `settimer <duration> <title>` | Live countdown; survives restarts |
| `activetimers` | List running timers |
| `dismiss <hash>` / `resend <hash>` | Cancel or repost a timer |

Durations: `90`, `15:30`, `1:15:30`, `2:12:15:30`, or `1h30m`.

### Utilities
| Command | Description |
|---|---|
| `qr <text> [--size N] [--fg c] [--bg c]` | Generate a QR code |
| `qrread` | Decode a QR from a replied image |
| `topdf [en\|fa] [size]` | Convert replied text to PDF |
| `weather <city>` / `hourly <city>` | Forecasts — no API key needed |
| `dic <word>` | Definitions, examples and pronunciation audio |
| `currency` | Live IRR rates, gold and coins |
| `emojinfo` | Inspect replied message for custom/premium emoji IDs & HTML tags |
| `html <text>` | Send an HTML message supporting custom/premium emojis |

### Automation & Stickers
| Command | Description |
|---|---|
| `setautoreply <contain\|match> "input" "reply"` | 👑 Auto-reply in the current chat only (`contain` is whole-word aware) |
| `remautoreply <contain\|match> "input"` / `autoreplylist` | 👑 Manage per-chat auto-replies |
| `selfwlc <set\|on\|off\|list\|clear> [-all]` | 👑 Welcome new members with a saved per-chat message (`[name]`, `[nametag]`, `[username]` and `[[username]/[nametag]]` tags; Persian supported) |
| `setreact <@channel> <emoji>` | 👑 Auto-react to new posts |
| `remreact <@channel>` / `reactlist` | 👑 Manage auto-reactions |
| `startchallenge [count] [delay]` | 👑 Tag active members on a replied challenge message (default 1/msg, collision-aware) |
| `stopchallenge` / `challengestatus` | 👑 Halt challenge tagging & clear memory, or view live status |
| `stick [-save] <text>` | Render text to a sticker |
| `stickerpack create\|open\|list\|close\|delete` | Manage packs |

👑 = owner only.

---

## Architecture

```
src/selfbot/
├── __main__.py        CLI entry point, signal handling
├── bot.py             Client lifecycle, routing, shared state
├── config.py          Typed config loaded from the environment
├── db.py              Async SQLite/MySQL layer + repositories
├── registry.py        Command registry, validation, dispatch
├── errors.py          Exception hierarchy
├── logging_setup.py   Console, rotating file and Telegram sinks
├── services/          Supervisor integration
├── utils/             HTTP client, text helpers, filesystem safety
└── plugins/           One module per feature area
```

**Adding a command** — drop a module in `plugins/`; it is auto-discovered:

```python
from ..registry import Context, command

@command("hello", category="Fun", usage="hello <name>", min_args=1)
async def cmd_hello(ctx: Context) -> None:
    """Greet someone."""
    await ctx.reply(f"Hello, {ctx.args[0]}!")
```

Argument rules (`min_args`, `max_args`, `requires_reply`, `sudo_only`) are
enforced by the dispatcher before your handler runs, so a misuse produces a
usage hint rather than a traceback.

---

## Development

```bash
pip install -e ".[dev,full]"

pytest                      # 291 tests
pytest --cov=selfbot        # with coverage
ruff check src tests        # lint
mypy src/selfbot            # type check
python -m selfbot --check   # validate config + registry
```

**CI:** the workflow lives at `ci/github-actions.yml`; see [`ci/README.md`](ci/README.md)
to enable it (one `git mv`).

`tests/test_regressions.py` pins the specific bugs fixed in v2 so they cannot
come back.

---

## Upgrading from v1

The original single-file `self.py` was replaced by the `src/selfbot/` package.

1. **Rotate every credential** that was hardcoded in v1 — the Telegram API hash,
   RapidAPI key, CoinMarketCap key, MySQL password and sticker bot token were
   committed to git and must be considered compromised.
2. Move your settings into `.env` (see `.env.example`).
3. Run `python -m selfbot` instead of `python self.py`.
4. Your existing MySQL data still works — set `DATABASE_URL` to point at it.

Renamed commands: `zipfile`→`zip` (alias kept), `dw`→`weather` (alias kept),
`hw`→`hourly` (alias kept), and `qradv` was folded into `qr --fg/--bg`.

### What changed under the hood

| v1 | v2 |
|---|---|
| Secrets hardcoded and committed | Environment-driven, git-scanned in CI |
| 9 commands crashed on arguments | Dispatcher validates arity up front |
| Blocking `requests` in async handlers | Shared async client with retries |
| Blocking MySQL on every message | Async SQLite/MySQL, cached auth |
| `extractall` on untrusted zips | Traversal, symlink and bomb protection |
| Path traversal via `rename` | Filenames sanitised to one component |
| Naive `datetime.now()` vs SQL `NOW()` | UTC end to end |
| 5,293-line single file | 20 focused modules |
| No tests, lint or CI | 153 tests, ruff clean, CI matrix |

---

## Security

- Secrets live only in `.env`, which is gitignored; CI fails on committed credentials.
- Archive extraction blocks path traversal, absolute paths, symlink escapes and zip bombs.
- Filenames from users are reduced to a single sanitised path component.
- All SQL uses bound parameters.
- Downloads are size-capped by `MAX_FILE_SIZE_MB`.
- `.session` files grant full account access — never share or commit them.

Found a vulnerability? Open a private security advisory rather than a public issue.

---

## License

MIT — see [LICENSE](LICENSE).
