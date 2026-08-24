# Implementation Plan — AI Memory, Provider System & Platform Features

This document plans all ten requested features against the **current v2 codebase**
(`src/selfbot/`, plugin-based, async SQLite/MySQL, Telethon). It maps each feature
to concrete files, tables, commands and tests, sequences the work to avoid
rework, and calls out backward-compatibility and risk.

---

## 0. Current architecture (what we build on)

- **`bot.py`** — `SelfBot` owns the Telethon client, `Database`, `CommandRegistry`,
  HTTP client, runtime task dicts and event handlers. New cross-cutting services
  (AI manager, metrics, plugin manager) attach here.
- **`registry.py`** — `@command(...)` decorator + `Context`. Dispatch validates
  `min_args`/`max_args`/`requires_reply`/`sudo_only`. Plugins are auto-discovered
  by `plugins/__init__.load_all()`.
- **`db.py`** — One `Database` with hand-written SQLite/MySQL dual DDL and a
  repository API. **No migration framework** — additive `ALTER TABLE` checks run
  after `CREATE TABLE IF NOT EXISTS`. We extend this same pattern.
- **`plugins/ai.py`** — Stateless functions: `_anyapi_completion`,
  `_rapidapi_completion`, `_backup_rapidapi_completion`, `_extract_answer`,
  `_format_provider_error`. The `gpt` command hard-codes a 3-tier fallback chain
  reading from `config.ai` (env vars). This is the file most affected.
- **`utils/http.py`** — Shared `HttpClient` (aiohttp) with retries.
- **`errors.py`** — `ProviderError` / `_ProviderStatusError` already carry HTTP
  status for fallback decisions.
- **Tests** — `tests/test_ai.py` stubs `bot.http` and asserts on
  `bot.http.calls`; `conftest.py` provides `FakeBot`, `FakeEvent`, `FakeClient`.

**Key design rule:** keep every feature behind the existing `@command` +
repository pattern so it is testable with the current fakes and never touches
module globals.

---

## 1. Foundational DB schema changes (shared by several features)

Add four tables to `Database._create_schema()` (both dialects), plus an
`_migrate()` step mirroring the existing `reply_condition` migration style.

### `ai_providers`
```
id            INTEGER PK AUTOINCREMENT
name          TEXT NOT NULL UNIQUE        -- e.g. "anyapi", "openai", "my-server"
base_url      TEXT NOT NULL               -- OpenAI-compatible /v1 root
api_key       TEXT NOT NULL               -- encrypted at rest (see §9)
model         TEXT NOT NULL DEFAULT ''    -- optional; provider picks default if empty
kind          TEXT NOT NULL DEFAULT 'openai'  -- 'openai' | 'rapidapi' | 'rapidapi_backup'
is_default    INTEGER NOT NULL DEFAULT 0
enabled       INTEGER NOT NULL DEFAULT 1
cooldown_until TEXT                       -- ISO UTC; set on 429/quota
last_error    TEXT
success_count INTEGER DEFAULT 0
failure_count INTEGER DEFAULT 0
created_at    TEXT
```
- On first start, **seed** rows from existing env vars (`ANYAPI_*`,
  `BLUESMINDS_*`, `RAPIDAPI_KEY`) so existing users keep working with no manual
  step. Seeding only runs when the table is empty.
- `kind='rapidapi'` rows preserve the legacy RapidAPI path; everything else uses
  the OpenAI-compatible path.

### `ai_messages` (conversation memory)
```
id          INTEGER PK AUTOINCREMENT
chat_id     INTEGER NOT NULL
role        TEXT NOT NULL          -- 'system' | 'user' | 'assistant'
content     TEXT NOT NULL
provider    TEXT                   -- which provider answered (optional)
created_at  TEXT NOT NULL
INDEX (chat_id, id)
```

### `app_settings` (generic key/value for model selection, flags)
```
key    TEXT PRIMARY KEY
value  TEXT NOT NULL             -- JSON-encoded
```
Holds `ai.default_model`, `ai.memory_turns`, `health.bind`/`health.port`, etc.

### `plugin_state` (external plugin install records)
```
name       TEXT PRIMARY KEY
source     TEXT NOT NULL         -- 'local:<path>' | 'git:<url>' | 'pypi:<pkg>'
version    TEXT
enabled    INTEGER NOT NULL DEFAULT 1
loaded_at  TEXT
```

**Migration approach:** no new dependency. Each `CREATE TABLE IF NOT EXISTS` is
idempotent; existing databases just gain the tables. Add repository methods
(`add_provider`, `list_providers`, `set_provider_cooldown`, `add_ai_message`,
`recent_ai_messages`, `prune_ai_messages`, `get_setting`, `set_setting`, …)
following the existing `%s`→`?` translation already in `db.py`.

---

## 2. Secrets at rest

**Requirement:** API keys move from `.env` into the DB.

- Add `cryptography` as a core dependency (small, pure-wheel on supported
  platforms; Fernet/AES-GCM).
- New `src/selfbot/security.py`:
  - `SecretBox` — generated key at `$DATA_DIR/secret.key`, file mode `0600`.
  - `encrypt(plaintext) -> str` / `decrypt(token) -> str` (Fernet, URL-safe).
  - On first use, generate and persist the key; log its path so the operator
    knows to back it up alongside the DB.
  - `api_key` is stored encrypted; decrypted only when building a request.
  - Empty/legacy plaintext values decrypt transparently (if Fernet fails, treat
    as plaintext and re-encrypt on next write) so seeded rows migrate cleanly.
- The key file is excluded from any backup export unless the operator passes
  `-include-secrets`; otherwise provider entries are exported with redacted
  keys (see §8).

---

## 3. AI provider service (refactor of `plugins/ai.py`)

New `src/selfbot/services/ai.py` — `AIManager`, constructed in `SelfBot.__init__`
alongside the DB and HTTP client.

### Responsibilities
1. **Load providers from DB** (cached, invalidated on add/remove/toggle).
2. **Pick the active chain:** default provider first, then other enabled
   `openai` providers, then `rapidapi`, then `rapidapi_backup` — preserving the
   current precedence users expect.
3. **Per-provider cooldown:** on HTTP 429 / quota error, set
   `cooldown_until = now + backoff` (60s → 300s → 900s, capped; reset on
   success) and **skip** that provider on the next call. This is the automatic
   quota cooldown feature.
4. **Conversation memory:** prepend recent `ai_messages` for the chat (default
   last 10 turns = 20 rows) plus the system prompt; after a successful answer
   persist both the user message and the assistant reply.
5. **Model selection:** use the provider's stored model, overridden by the
   per-chat/global `ai.default_model` setting when set.
6. **Counters:** increment `success_count`/`failure_count`, record `last_error`.

### Public API
```python
class AIManager:
    async def chat(self, *, chat_id, prompt, history=True,
                   model: str | None = None, system: str | None = None) -> str
    async def providers(self) -> list[Provider]
    async def status(self) -> list[ProviderStatus]   # for aistatus
    async def add_openai_provider(name, base_url, api_key, model="") -> None
    async def remove_provider(name) -> None
    async def set_default(name) -> None
    async def test_provider(name) -> tuple[bool, str]   # cheap /models or 1-token call
    def cooldown_remaining(provider_id) -> int
```

The request/response parsing (`_extract_answer`, `_format_provider_error`,
content-part handling, RapidAPI shape) is **moved unchanged** into the service
so behavior and error messages stay identical. `plugins/ai.py` becomes a thin
command layer over `AIManager`.

**Backward compatibility:** the existing private functions are re-exported from
`plugins/ai.py` as wrappers so `tests/test_ai.py` and any external importers keep
working; new tests target the manager directly.

---

## 4. AI conversation memory

- Storage: `ai_messages` (§1).
- `AIManager.chat(history=True)` loads the last `2 * memory_turns` rows for
  `chat_id`, oldest-first, and includes them in the `messages` array.
- A `gptmemory` command controls it per user/chat:
  - `gptmemory on | off` — toggle memory for the current chat (stored in
    `app_settings` key `ai.memory.<chat_id>` as JSON bool; default on).
  - `gptmemory clear` — delete that chat's rows.
  - `gptmemory turns <n>` — set global rolling window (4–50).
  - `gptmemory status` — show on/off, turns, stored count.
- **Token safety:** cap the assembled prompt at a configurable character budget
  (default ~24k chars) by dropping oldest turns first; log when truncated.
- The system prompt is always first and never pruned.
- `gpt` reply/edit flows (§5) share the same memory write path so replies are
  contextual.

---

## 5. AI reply / edit mode

Two interaction modes on top of `gpt`:

- **Reply with GPT** — reply to any message and run `gpt [instruction]`. The
  replied message text is **prepended** to the prompt as context:
  `"Message being replied to:\n\"\"\"<text>\"\"\"\n\nInstruction: <args>"`.
  If no args, the instruction defaults to *"Reply to this message."* This makes
  `gpt` produce a direct answer to the quoted message.
- **Edit into an AI response** — new `gptedit` command (sudo-only), must be a
  reply:
  - Verifies the replied message was sent by the logged-in account
    (`replied.sender_id == bot.me.id`); otherwise refuses (you cannot edit
    others' messages).
  - Sends "🤖 Rewriting…", calls the AI with the replied text + instruction,
    then **edits** the original message in place via `bot.edit(...)`.
  - On failure, the original message is left untouched and an error is posted
    as a new reply.

Both commands share the existing `ctx.get_reply_message()` and `bot.edit`
helpers. `gptedit` is added to `plugins/ai.py`; `gpt` gains the
reply-context branch.

---

## 6. Provider status command

`aistatus` (sudo-only), category **AI**:

For each provider row show:
```
• anyapi        ✅ enabled · default · model: anthropic/...
  https://api.anyapi.ai/v1  ·  128 ok / 2 fail  ·  cooldown 47s
• my-server     ⏸ disabled
• rapidapi      ⚠️ last error: quota reached (HTTP 429)
```
- ✅/⏸/⚠️/❄️ icons for enabled, disabled, last-error, and in-cooldown.
- Key is shown only as `sk-••••1234` (last 4 chars).
- `aistatus test <name>` does a live connectivity probe via
  `AIManager.test_provider` and reports reachability + model, with a timeout.
- The existing `status` command (core) gets a one-line AI summary
  (`AI: 3 providers · 1 default · 0 cooling`).

---

## 7. AI model selection — `gptmodel`

- `gptmodel list` — query each enabled OpenAI provider's `GET /models` (cached
  10 min, with graceful per-provider failure), merge into a deduplicated list;
  mark the current global default and each provider's stored model. Falls back
  to listing just the configured models when `/models` is unsupported.
- `gptmodel set <model>` — set global default model in `app_settings`.
  Accepts a bare model id (applies to default provider) or
  `<provider>/<model>` to also update that provider's row.
- `gptmodel set <provider> <model>` — set a specific provider's model.
- `gptmodel clear` — remove the global override so providers use their own.
- `gptmodel current` — show what the next `gpt` call will use.
- Validation: non-empty, length cap; does not require the model to exist in
  `/models` (custom model names are allowed).

---

## 8. Summarize command

`summarize` (category **AI**), works in three modes:

1. **Replied message** — summarize the quoted text/document caption.
2. **Document** — reply to a document (`.txt`, `.md`, `.pdf` via `pypdf`
   already in `full`, or any text-based file under `MAX_FILE_SIZE_MB`); download
   to the temp workspace, extract text, truncate to a budget (~30k chars),
   summarize.
3. **Conversation** — `summarize <n>` (e.g. `summarize 50`) pulls the last `n`
   messages from the current chat via `client.iter_messages`, formats them as
   `<sender>: <text>`, and asks the model for a bullet summary.

Options: `-lang en|fa` (output language; auto-detected default), `-brief`
(3 bullets) / `-detailed` (paragraphs). Reuses the `AIManager` with
`history=False` and a dedicated summarization system prompt so it does not
pollute conversational memory.

---

## 9. Message search

`search` (category **Messaging**), scope is the **current chat** (safe, no
cross-chat scraping), with filters:

```
search <text>                      # substring in message text
search -from <id|@username|me>    # sender filter
search -since <YYYY-MM-DD>        # inclusive
search -until <YYYY-MM-DD>        # inclusive
search -type photo|video|voice|audio|file|sticker|gif|link
search [filters] -limit <n>       # default 20, max 100
```

- Uses `client.iter_messages(chat_id, search=text, from_user=..., offset_date=...)`.
- Media type maps to the same attributes used by `del` in `messaging.py`
  (`photo`, `video`, `voice`, `audio`, `document`, `sticker`, `gif`,
  `web_preview`).
- Renders up to `-limit` results as: sender · date · snippet (truncated 120
  chars) · a `t.me/c/.../id` deep link where resolvable.
- Date parsing is strict; bad dates raise `UsageError` with an example.
- Empty result set returns an explicit "nothing found" message.

---

## 10. Backup and restore

`backup` and `restore` (sudo-only, category **Admin**), in `plugins/backup.py`.

**`backup`** exports a versioned JSON document:
- `users`, `quick_replies`, `auto_replies`, `welcomes`, `channel_reactions`,
  `timers` (active only), `sticker_packs`, `app_settings`, `plugin_state`.
- Providers are included but **API keys are redacted** by default
  (`"sk-…"`); `backup -include-secrets` encrypts the whole archive instead (see
  below) and the secret key path is printed.
- The JSON is written to a temp file and sent as a Telegram document with a
  timestamped filename `selfbot-backup-YYYYMMDD-HHMMSS.json`.
- `backup -file <name>` re-uploads a previously stored copy from `DATA_DIR`.

**`restore`** (reply to a backup document):
- Downloads to the temp workspace, validates `version` and top-level shape.
- **Requires confirmation** via the existing `bot.confirm(...)` (and supports
  `-force` to skip). Shows what will be overwritten.
- Imports each table inside a best-effort per-section transaction; reports
  per-section counts (`quick_replies: 12 inserted`, `welcomes: 3 skipped`).
- Does **not** overwrite existing provider API keys with redacted stubs.
- Timers are restored as rows; the existing startup `restore_timers` then
  re-arms them on next restart (or we re-arm immediately for active ones).

**Encryption for `-include-secrets`:** reuse the `SecretBox` to Fernet-encrypt
the JSON before sending, producing `selfbot-backup-*.enc`. The operator must
keep `secret.key` to restore it. This avoids adding a password-KDF step for the
default path while still protecting secrets on request.

---

## 11. Better logging and health checks

### In-process metrics (new `services/metrics.py`)
A tiny `Metrics` singleton attached to `SelfBot`:
- Counters: `messages_seen`, `commands_run`, `commands_failed`,
  `ai_requests`, `ai_failures`, `http_*` (hooked in `utils/http.py`).
- Gauges read live: background task count (`len(bot._background)` + timer/spam/
  challenge tasks), DB backend + last-query OK, memory RSS via `resource.getrusage`
  (no new dependency on Linux/macOS; guarded import on Windows).
- Event-loop lag sampled every 10s by a `call_latency` probe.
- Rolling ring buffer (last 100) of API failures: timestamp, provider/host,
  status, truncated message. This surfaces "API failures" explicitly.

### Commands
- `health` (sudo-only, category **Core**) — replaces/extends the current
  `status` detail:
  ```
  🩺 SelfBot health
  • Runtime: up 2d 3h · state 🟢 · Python 3.12
  • Memory: 84.2 MB RSS · event-loop lag 4 ms
  • Tasks: 3 bg · 2 timers · 1 challenge
  • DB: sqlite · connected · 8 tables
  • AI: 3 providers · 128 ok / 2 fail · 0 cooling
  • Recent API failures: (2)
      12:01 anyapi 429 quota reached
      12:04 bluesminds 503 upstream
  ```
- `health metrics` — full counter dump as a code block.
- Existing `status` stays as the short version and links to `health`.

### Optional HTTP health endpoint
- `HEALTH_BIND` (default `127.0.0.1`) / `HEALTH_PORT` (default empty/off).
- When set, start a minimal `aiohttp.web` server in `SelfBot.start()` exposing
  `GET /healthz` → `200 {"status":"ok","uptime":...,"db":"ok","ai":...}` and
  `GET /readyz`. Bound to localhost by default so it is safe for Docker
  `HEALTHCHECK` without exposing data.
- Fails soft: if the port is busy, log a warning and continue (never fatal).

### Logging improvements
- `utils/http.py` logs non-retried failures at WARNING through the metrics
  ring buffer.
- `AIManager` logs cooldown transitions and provider fallback decisions at INFO.
- No change to the existing Telegram log sink.

---

## 12. Plugin system (external plugins)

The current "plugin system" only auto-imports modules inside the bundled
`plugins/` package. Add real extensibility without modifying core.

### New `services/plugins.py` — `PluginManager`
- **Discover paths:** `DATA_DIR/plugins/` (Python files and packages) plus
  entries recorded in `plugin_state` (git/pypi installs). This directory is
  created on startup and added to `sys.path` for plugins that ship extra
  modules.
- **Load:** import each enabled plugin module *after* the built-in plugins. A
  plugin is any module that calls `@command` on the global registry (the same
  mechanism built-ins use) — no new API to learn.
- **Manifest (optional):** a top-level `PLUGIN = PluginMeta(name=..., version=...,
  description=..., author=...)` dict; used for listing.
- **Isolation:** wrap each plugin import and each command invocation already
  goes through the dispatcher's try/except, so one broken plugin cannot crash
  the bot. Record load errors in `plugin_state` and surface them.
- **Lifecycle hooks (optional):** if the module defines `async def setup(bot)`
  it is awaited after load; `async def teardown(bot)` on shutdown. This lets
  plugins spawn background tasks via `bot._spawn` and clean up.
- **Public API surface:** document `Context`, `command`, `bot.client`,
  `bot.db`, `bot.http`, `bot.ai` (the new manager), and `bot.metrics` as the
  stable extension surface.

### Commands (`plugin`, sudo-only, category **System**)
- `plugin list` — installed/enabled, version, source, command count, load
  errors.
- `plugin enable <name>` / `plugin disable <name>` — toggle in `plugin_state`
  (requires restart for now; disabling unregisters its commands live where
  possible via a new `registry.unregister`).
- `plugin load <path>` — copy/add a local `.py` file or package directory into
  `DATA_DIR/plugins/` and import it immediately.
- `plugin reload <name>` — re-import (best-effort; commands are re-registered).
- `plugin unload <name>` — unregister its commands; disable in state.
- `plugin install <git-url|pypi-spec>` — clone via `git` (subprocess) into
  `DATA_DIR/plugins/` or `pip install` into the current environment; requires
  an explicit `-trust` acknowledgement (third-party code runs as the user
  account). Logged loudly.

### Registry change
- Add `CommandRegistry.unregister(name)` removing the command and its aliases,
  used by unload/reload. The global `registry` gains it; no existing caller is
  affected.

### Security
- External plugins run with **full account access** (they get the Telethon
  client). The `plugin install` flow prints a clear warning and requires
  `-trust`; bundled plugins remain the default. `DATA_DIR/plugins/` is noted
  in `.gitignore`.

---

## 13. Configuration changes

`config.py` additions (all optional, env-driven, preserving the frozen-dataclass
style):

| Var | Default | Purpose |
|---|---|---|
| `AI_MEMORY_TURNS` | `10` | rolling conversation turns |
| `AI_MEMORY_BUDGET` | `24000` | char cap before pruning |
| `AI_COOLDOWN_MAX` | `900` | max provider cooldown seconds |
| `HEALTH_BIND` | `127.0.0.1` | health endpoint bind |
| `HEALTH_PORT` | _(empty)_ | set to enable HTTP healthz |
| `PLUGINS_DIR` | `$DATA_DIR/plugins` | external plugin location |

`.env.example` is updated with these and the AI-provider section is rewritten to
note that keys now live in the DB (env vars remain supported as a **seed /
fallback**, with a deprecation note). `config.describe()` gains AI/health lines.

---

## 14. Command summary

| Command | Category | Auth | New/Changed |
|---|---|---|---|
| `gpt [instruction]` | AI | authorized | changed — reply context + memory |
| `gptedit [instruction]` | AI | sudo | **new** |
| `gptmemory <on\|off\|clear\|turns\|status>` | AI | authorized | **new** |
| `aistatus [test <name>]` | AI | sudo | **new** |
| `gptmodel <list\|set\|clear\|current>` | AI | sudo | **new** |
| `summarize [n] [-lang] [-brief\|-detailed]` | AI | authorized | **new** |
| `search ...filters` | Messaging | authorized | **new** |
| `backup [-include-secrets]` | Admin | sudo | **new** |
| `restore [-force]` | Admin | sudo | **new** |
| `health [metrics]` | Core | sudo | **new** |
| `plugin <list\|enable\|disable\|load\|reload\|unload\|install>` | System | sudo | **new** |
| `status` | Core | sudo | changed — AI one-liner |
| `help` | Core | any | auto-includes new commands |

No command-name collisions with existing commands (verified against
`core/ai/messaging/utilities/timers/files/reactions/welcome/stickers/challenge`).

---

## 15. Testing plan

Extend the existing pytest suite with the fakes already in `conftest.py`:

- **`test_ai_providers.py`** — `AIManager` selection, cooldown skip after 429,
  memory assembly + pruning, model override, count increments. Uses a stub
  HTTP client like the current `SequenceHttp`.
- **`test_ai_commands.py`** — `gpt` reply-context prompt shape, `gptedit`
  refuses non-owned messages, `gptmemory` toggles/clear, `gptmodel`
  set/list/current, `aistatus` rendering (including cooldown/disabled states).
- **`test_summarize.py`** — replied-text path, conversation path with a fake
  `iter_messages`, document path with a temp `.txt`; asserts the model call and
  that history is not written.
- **`test_search.py`** — argument parsing (all filters), date validation,
  media-type mapping, empty result, result rendering; fake client records
  `iter_messages` kwargs.
- **`test_backup.py`** — export shape, redacted vs encrypted secrets, restore
  with confirmation, idempotent re-import, provider-key protection; uses a
  temp SQLite DB.
- **`test_health.py`** — metrics counters, health text includes task/memory/db
  lines, `/healthz` via `aiohttp` test client, cooldown ring buffer.
- **`test_plugins_ext.py`** — load a temp plugin file from a tmp `plugins/`
  dir, verify its command registers and runs; disable/unregister; bad plugin
  does not crash load; setup/teardown hooks called.
- **`test_security.py`** — encrypt/decrypt round trip, plaintext fallback,
  key file creation with `0600`, wrong-key handling.
- **`test_db_ai.py`** — new repository methods and idempotent schema creation
  on both a fresh DB and a migrated pre-existing DB (simulated by creating the
  old schema first).
- Update **`tests/test_ai.py`** minimally to keep the legacy re-export contract
  green; add cases for the seeded-from-env path.

Target: all existing tests continue to pass; new coverage for every new
command and service. `ruff` and `mypy` stay clean (no new ignores).

---

## 16. Documentation

- `README.md`: new **AI providers** and **Memory** sections, a **Plugins**
  section with a hello-world external plugin, updated command table (from 44 to
  ~57 commands), and an updated architecture tree showing `services/ai.py`,
  `services/metrics.py`, `services/plugins.py`, `security.py`.
- `SETUP.md`: migrate AI keys step (`provider add`), health endpoint, plugin
  install + trust warning, secret-key backup note.
- `.env.example`: new vars + DB-first AI note.
- Docstrings on every new command (the `help` system uses the first line).

---

## 17. Implementation sequence (phased)

Each phase ends with runnable code + passing tests, so the branch is never in a
broken state.

**Phase 1 — Foundation (no behavior change)**
1. `security.py` (SecretBox) + tests.
2. DB tables + repositories + migration checks + tests.
3. Wire `AIManager`, `Metrics`, `PluginManager` into `SelfBot.__init__`/
   `start`/`shutdown` as no-ops (manager loaded but `gpt` unchanged).
4. Seed providers from env on first start.

**Phase 2 — Provider system & AI core**
5. Move completion logic into `AIManager`; keep `plugins/ai.py` wrappers.
6. Provider cooldown + counters.
7. `aistatus` + `gptmodel` commands.
8. Refactor `gpt` to use the manager; add reply-context.

**Phase 3 — Memory & AI UX**
9. `ai_messages` storage + memory in `AIManager.chat` + pruning/budget.
10. `gptmemory` command.
11. `gptedit` command.
12. `summarize` command.

**Phase 4 — Data features**
13. `search` command.
14. `backup` / `restore` commands.

**Phase 5 — Observability**
15. `Metrics` service + HTTP instrumentation.
16. `health` command + optional `/healthz`.
17. Logging improvements.

**Phase 6 — Plugins**
18. `registry.unregister`.
19. `PluginManager` discovery + load + setup/teardown.
20. `plugin` commands.
21. External-plugin docs + security warnings.

**Phase 7 — Docs, polish, CI**
22. Update README/SETUP/.env.example.
23. Full `ruff` + `mypy` + `pytest` pass; bump version to `2.1.0`.

---

## 18. Risks and mitigations

| Risk | Mitigation |
|---|---|
| **Account ban risk** (self-bot edits/reads history) | Edit only own messages (`gptedit` checks `sender_id`); search is current-chat only; document ToS warning already present. |
| **Losing access after moving keys to DB** | Env vars still seed on first start; secret key path logged; backup warns to back up `secret.key`. |
| **Memory blows prompt/token budget** | Character budget + rolling window + drop-oldest; system prompt pinned. |
| **Provider `/models` endpoint varies** | `gptmodel list` degrades gracefully to configured models; never hard-fails. |
| **External plugin executes untrusted code** | Opt-in `plugin install -trust`; disabled by default; loud warning; bundled plugins unaffected. |
| **MySQL/SQLite dialect drift** | All new DDL written in both dialects from the start, following existing pattern; tested on SQLite in CI. |
| **Backward compat for `test_ai.py`** | Re-export legacy helpers as wrappers; behavior/error messages preserved. |
| **Health endpoint exposure** | Binds `127.0.0.1` by default; off unless `HEALTH_PORT` set; no secrets in `/healthz`. |
| **Large conversation/document inputs** | Hard caps (`-limit 100`, document size cap, char budget); clear errors. |

---

## 19. New / changed files at a glance

**New**
```
src/selfbot/security.py
src/selfbot/services/ai.py
src/selfbot/services/metrics.py
src/selfbot/services/plugins.py
src/selfbot/plugins/backup.py
src/selfbot/plugins/search.py
src/selfbot/plugins/health.py
tests/test_security.py
tests/test_db_ai.py
tests/test_ai_providers.py
tests/test_ai_commands.py
tests/test_summarize.py
tests/test_search.py
tests/test_backup.py
tests/test_health.py
tests/test_plugins_ext.py
```

**Changed**
```
src/selfbot/bot.py                 # attach AI/metrics/plugins; reply/edit hooks
src/selfbot/config.py              # new optional settings
src/selfbot/db.py                  # 4 tables + repositories + migrations
src/selfbot/registry.py            # unregister
src/selfbot/plugins/ai.py          # thin command layer over AIManager; gptedit/gptmemory/gptmodel/aistatus/summarize
src/selfbot/plugins/core.py        # status AI one-liner; help auto-updates
src/selfbot/plugins/__init__.py    # load external plugins after built-ins
src/selfbot/utils/http.py          # metrics hooks
src/selfbot/__main__.py            # construct managers before client
pyproject.toml                     # add cryptography
.env.example                       # new vars + DB-first AI note
README.md, SETUP.md                # docs
```

---

## 20. Effort estimate

Roughly **6–8 focused days** across the seven phases, with Phases 2–3
(provider/AI core + memory) the largest. The plan is structured so each phase
ships independently and can be reviewed/tested on its own.
