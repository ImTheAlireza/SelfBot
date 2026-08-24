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

57 commands across AI, file manipulation, timers, stickers, QR codes, weather,
dictionaries, search, backups and chat automation — all driven from your own
Telegram account by typing commands into any chat.

| | |
|---|---|
| 🧠 **AI** | Ask any OpenAI-compatible model with `gpt`, with per-chat memory, reply/edit mode, summarization, model selection and provider management. |
| 📁 **Files** | Zip/unzip with AES passwords, batch queues, rename, audio tag editing, PDF page extraction. |
| ⏰ **Timers** | Live-updating countdowns that survive restarts. |
| 🎨 **Stickers** | Render text to stickers and manage packs via a helper bot. |
| 🔲 **Utilities** | QR generate/decode, text→PDF (English + Persian), weather, dictionary with audio, IRR exchange rates. |
| ⚡ **Automation** | Quick-reply shortcuts, per-channel auto-reactions, per-chat auto-replies, per-chat welcome messages, controlled bulk deletion. |
| 🔍 **Search & data** | Search chat history by text/sender/date/media, and export/import backups of settings, quick replies, timers and more. |
| 🧩 **Plugins** | Drop Python modules into a plugins directory to add commands without touching the core. |
| 🩺 **Observability** | In-chat health report (tasks, memory, DB, API failures) and an optional `/healthz` endpoint. |

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
| `ANYAPI_KEY` / `BLUESMINDS_API_KEY` / `RAPIDAPI_KEY` | | — | AI keys seeded into the DB on first run; managed afterward via `ai` |
| `HEALTH_PORT` / `HEALTH_BIND` | | disabled / `127.0.0.1` | Enable the `/healthz` HTTP endpoint |
| `PLUGINS_DIR` | | `DATA_DIR/plugins` | Directory for external plugins |
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
| `health [metrics]` | 👑 Tasks, memory, DB, AI and recent API failures |
| `self on\|off\|restart\|status\|logs\|diag` | 👑 Process control and supervisor troubleshooting |
| `plugin list\|load\|reload\|unload\|enable\|disable\|install\|path` | 👑 Manage external plugins |

`self restart` directly replaces the current Python process while preserving
its PID and environment. It deliberately does not call `supervisorctl restart`,
which can deadlock when invoked by the process being restarted.

### Admin 👑
| Command | Description |
|---|---|
| `setadmin <id\|@user>` | Let another user run commands |
| `remadmin <id>` | Revoke access |
| `adminlist` | List authorised users |
| `backup [-include-secrets]` | 👑 Export all settings/data as JSON (keys redacted by default) |
| `restore [-force]` | 👑 Restore from a replied backup file |

### AI — 4 commands
| Command | Description |
|---|---|
| `gpt <prompt>` | Ask the active model. Reply to a message to feed it as context. Reply with `gpt edit [instruction]` to rewrite one of **your own** messages in place. |
| `memory on\|off\|clear\|turns <n>\|status` | Per-chat conversation memory |
| `summarize [n] [-lang en\|fa] [-brief\|-detailed]` | Summarize a replied message, document or last `n` messages |
| `ai [add\|remove\|default\|enable\|disable\|test\|model\|status]` | 👑 Manage providers and the active model — the **only** place to do so |

There is exactly **one** chat command (`gpt`) and **one** management command
(`ai`). No aliases, no overlapping commands. To add and use a provider:

```
ai add https://api.openai.com/v1 sk-your-key gpt-4o-mini
ai add bai https://api.b.ai/v1 sk-your-key gpt-5.2
ai default openai
ai              # status: active model + all providers
ai model        # show active model
ai model list   # discover models
ai model luna   # set model on the default provider
```

Providers and API keys are stored **in the database** (encrypted at rest),
not in `.env`; the `ai add` message containing the plaintext key is deleted
automatically. Both ordinary OpenAI JSON responses and streamed SSE deltas are
supported. A copied full endpoint such as `/v1/chat/completions` is normalized
to its base URL automatically. GPT response footers distinguish the requested
model from a different model identifier reported by the API. Keys still present in `ANYAPI_KEY` /
`BLUESMINDS_API_KEY` / `RAPIDAPI_KEY` are seeded automatically on first start.
When a provider returns a temporary quota/rate-limit error it is skipped for a
fixed 10 seconds by default (`AI_COOLDOWN_SECONDS`); `ai` shows who is cooling
down.

### Messaging
| Command | Description |
|---|---|
| `spam <message> <count>` | Repeat a message, rate-limit aware |
| `cancel` | Stop your running spam task |
| `del <count\|type> [-me]` | 👑 Delete messages in the current chat; `-me` limits it to yours |
| `info [user]` | User details and profile photo (reply, mention, or yourself) |
| `qreply set\|remove\|list\|info` | Manage `-alias` shortcuts |
| `-<alias>` | Expand a quick reply in place |
| `search <text> [-here] [-from X] [-since D] [-type media]` | Account-wide by default (add `-here` for this chat). Paged results; navigate with `more`, `back`, `page <n>`, `open <n>`, `recent`, `stop`. |

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
| `qr <text> [-size N] [-fg c] [-bg c]`| | Generate a QR code |
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
├── security.py        Fernet encryption for secrets at rest
├── errors.py          Exception hierarchy
├── logging_setup.py   Console, rotating file and Telegram sinks
├── services/
│   ├── ai.py          AIManager: provider routing, cooldown, memory
│   ├── metrics.py     Counters, gauges, API-failure ring buffer
│   ├── plugins.py     External plugin loader/lifecycle
│   └── supervisor.py  Process-manager integration
├── utils/             HTTP client, text helpers, filesystem safety
└── plugins/           One module per feature area (auto-discovered)
```

**Adding a bundled command** — drop a module in `plugins/`; it is auto-discovered:

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

### External plugins

Drop a `.py` file (or a package directory) into `DATA_DIR/plugins` and it is
loaded on startup — no core changes required. Plugins register commands with
the same decorator and may optionally expose metadata and lifecycle hooks:

```python
from selfbot.registry import command, Context

PLUGIN = type("PluginMeta", (), {"name": "hello", "version": "1.0"})()

async def setup(bot):        # optional: spawn background tasks here
    bot._spawn(my_worker(bot), name="hello-worker")

async def teardown(bot):     # optional: clean up on unload/reload
    ...

@command("hello", category="Plugins", usage="hello <name>", min_args=1)
async def cmd_hello(ctx: Context) -> None:
    await ctx.reply(f"Hello, {ctx.args[0]}!")
```

Manage them in chat: `plugin list`, `plugin load <path>`, `plugin reload <name>`,
`plugin enable|disable <name>`, `plugin unload <name>`, and
`plugin install <git-url|pip-spec> -trust`.

> **Warning:** external plugins run with full access to your Telegram account
> and the host. Only install code you have reviewed — `install` requires
> `-trust`.

---

## Development

```bash
pip install -e ".[dev,full]"

pytest                      # 441 tests
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
`hw`→`hourly` (alias kept), and `qradv` was folded into `qr -fg/-bg`.

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
