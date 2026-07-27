# Setup — step by step

Follow these in order. Steps 1–2 are security cleanup for the old leak; steps
3–7 get the bot running. Budget about 20 minutes.

---

## Part 1 — Damage control (do this first)

Your old `self.py` had live credentials in it and is in this repository's git
history. Anyone who cloned, forked or scraped the repo has them. Rotate
everything before you do anything else.

### Step 1 — Rotate the five leaked credentials

Work through this table. Each one takes a minute.

| # | Credential | What to do |
|---|---|---|
| 1 | **Telegram API hash** | Go to [my.telegram.org](https://my.telegram.org) → *API development tools*. You cannot regenerate a hash, so **create a new application** and use its `api_id` / `api_hash`. |
| 2 | **Sticker bot token** | Open [@BotFather](https://t.me/BotFather) → `/mybots` → pick your sticker bot → *API Token* → **Revoke current token**. Copy the new one. |
| 3 | **RapidAPI key** | [RapidAPI security page](https://rapidapi.com/developer/security) → your app → **Regenerate** the key. |
| 4 | **CoinMarketCap key** | [CoinMarketCap account](https://pro.coinmarketcap.com/account) → **Regenerate** the API key. |
| 5 | **MySQL password** | On your database server: `ALTER USER 'selfnit4_alireza'@'localhost' IDENTIFIED BY '<new-strong-password>';` — only needed if you keep using MySQL. |

### Step 2 — Kick out any unknown Telegram sessions

Telegram → **Settings** → **Devices** → **Terminate all other sessions**.

Do this even though a leaked `api_hash` alone doesn't grant account access — it
costs nothing and rules out a stolen session file.

> **Optional, later:** to scrub the secrets from git history entirely, follow the
> `git filter-repo` recipe in [`SECURITY.md`](SECURITY.md). It rewrites `main`
> and force-pushes, so do it when nobody else is mid-work. **Rotating the keys
> (step 1) matters far more than this.**

---

## Part 2 — Get the bot running

### Step 3 — Install

You need **Python 3.10 or newer** (`python3 --version` to check).

```bash
cd SelfBot
git checkout arena/019fa03c-selfbot

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -e ".[full]"
```

`[full]` pulls in everything the plugins need — Pillow, reportlab, qrcode,
mutagen, pypdf, pyzipper and the Persian text shapers.

### Step 4 — Create your `.env`

```bash
cp .env.example .env
```

Open `.env` and fill in **just these two**, using the *new* application you
created in step 1:

```env
TELEGRAM_API_ID=1234567
TELEGRAM_API_HASH=your_new_hash_from_step_1
```

Leave `SUDO_USER_ID` blank — the next step fills it in.

### Step 5 — Log in

```bash
python -m selfbot --login
```

Telegram sends a code to your app; type it in. If you have 2FA on, it asks for
your password too. You'll see:

```
✅ Signed in successfully.

   Account  : Alireza
   User ID  : 1038991065
   Session  : ./data/selfbot.session

✅ SUDO_USER_ID=1038991065 written to .env
```

That writes `SUDO_USER_ID` for you. It also creates `data/selfbot.session` —
**that file is equivalent to your password.** It's gitignored; keep it that way,
and back it up somewhere private if you don't want to re-authenticate later.

### Step 6 — Verify

```bash
python -m selfbot --check
```

Expected:

```
✅ Configuration valid — 43 commands registered.

session      : ./data/selfbot
database     : sqlite+aiosqlite:///./data/selfbot.db
sudo user    : 1038991065
ai provider  : none [disabled]
```

`ai provider: none` is correct for now — step 8 turns it on.

### Step 7 — Start it

```bash
python -m selfbot
```

Open **Saved Messages** in Telegram and send:

```
help
```

You should get the command list back. Try `ping`, then `settimer 30 test` to
watch a live countdown. Stop the bot with `Ctrl-C`.

**That's the minimum. Everything below is optional.**

---

## Part 3 — Optional extras

### Step 8 — Turn on AI (`gpt`, `gpts`, `gptr`, `imagine`)

Pick one and add it to `.env`:

<details open>
<summary><b>OpenAI</b></summary>

```env
AI_PROVIDER=openai
AI_API_KEY=sk-...
AI_MODEL=gpt-4o-mini
IMAGE_PROVIDER=openai
IMAGE_API_KEY=sk-...
```
</details>

<details open>
<summary><b>AgentRouter</b> — one key, many models</summary>

```env
AI_PROVIDER=agentrouter
AI_API_KEY=sk-your-agentrouter-key
AI_MODEL=claude-sonnet-4-5-20250929
AI_REASONING_MODEL=gpt-4o

# Optional: image generation through the same key
IMAGE_PROVIDER=agentrouter
IMAGE_API_KEY=sk-your-agentrouter-key
IMAGE_MODEL=dall-e-3
```

Keys come from [agentrouter.org/console/token](https://agentrouter.org/console/token).
Use whatever model slug AgentRouter lists — it's a passthrough router, so the
slug is sent through untouched. `AI_MODEL` powers `gpt`/`gpts`;
`AI_REASONING_MODEL` powers `gptr`.
</details>

<details>
<summary><b>OpenRouter</b> — many models behind one key, has web search</summary>

```env
AI_PROVIDER=openrouter
AI_API_KEY=sk-or-...
AI_MODEL=anthropic/claude-3.5-sonnet
```
</details>

<details>
<summary><b>Local model</b> — free, private, no API key</summary>

With [Ollama](https://ollama.com) running (`ollama pull llama3.1`):

```env
AI_PROVIDER=openai
AI_BASE_URL=http://localhost:11434/v1
AI_API_KEY=ollama
AI_MODEL=llama3.1
```
</details>

<details>
<summary><b>Your regenerated RapidAPI key</b> — reuses the v1 endpoints</summary>

```env
AI_PROVIDER=rapidapi
RAPIDAPI_KEY=your_new_key_from_step_1
TTS_PROVIDER=rapidapi
```
Also enables `tts`, and `annas`-style book search if you re-add it.
</details>

Restart the bot, then send `gpt hello` to test.

### Step 9 — Persian text in PDFs and stickers

Without this, `topdf fa` and Persian `stick` text render as boxes:

```bash
curl -Lo assets/fonts/Vazirmatn-Regular.ttf \
  https://github.com/rastikerdar/vazirmatn/raw/master/fonts/ttf/Vazirmatn-Regular.ttf
```

### Step 10 — Enable CI on GitHub

The workflow ships at `ci/github-actions.yml` because the app that pushed this
branch lacks permission to create workflow files. Activate it:

```bash
mkdir -p .github/workflows
git mv ci/github-actions.yml .github/workflows/ci.yml
git commit -m "Enable CI workflow"
git push
```

You get lint, tests on Python 3.10–3.12, a startup smoke test, and a secret scan
that fails the build if credentials ever get committed again.

### Step 11 — Keep it running 24/7

<details>
<summary><b>Docker</b> (simplest)</summary>

```bash
docker compose run --rm selfbot --login   # first time only
docker compose up -d
docker compose logs -f
```
The `selfbot-data` volume holds your session and database — back it up.
</details>

<details>
<summary><b>systemd</b> (Linux server)</summary>

`/etc/systemd/system/selfbot.service`:

```ini
[Unit]
Description=Telegram SelfBot
After=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/full/path/to/SelfBot
ExecStart=/full/path/to/SelfBot/.venv/bin/python -m selfbot
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now selfbot
journalctl -u selfbot -f
```

Log in **once interactively** before enabling the service.
</details>

<details>
<summary><b>supervisor</b> — enables <code>self status/restart/logs</code></summary>

**1. Define the program** in your `supervisord.conf`:

```ini
[program:selfbot]
command=/home/youruser/Selfbot/.venv/bin/python -m selfbot
directory=/home/youruser/Selfbot
autostart=true
autorestart=true
stopasgroup=true
killasgroup=true
stderr_logfile=/home/youruser/logs/selfbot.err.log
stdout_logfile=/home/youruser/logs/selfbot.out.log
```

**2. Point the bot at it** in `.env`:

```env
SUPERVISOR_PROCESS=selfbot
SUPERVISOR_LOG_FILE=/home/youruser/logs/selfbot.err.log
```

`SUPERVISOR_PROCESS` must match the name in `[program:NAME]`. That is normally
all you need — `supervisorctl` is found automatically, and it locates its own
config file.

**3. Reload and verify:**

```bash
supervisorctl reread && supervisorctl update
```

Then from Telegram: `self status`, `self logs 30`, `self restart`.

**If you get "supervisorctl could not be located":** send `self diag`. It
prints every location searched and the exact command it would run. Fix with
whichever applies:

```env
# Option A — tell the bot exactly where it is
SUPERVISOR_CTL=/home/youruser/Selfbot/.venv/bin/supervisorctl
```
```bash
# Option B — install it into the bot's own virtualenv
/home/youruser/Selfbot/.venv/bin/pip install supervisor
```

Find the path with `which supervisorctl` or
`find ~ -name supervisorctl -type f 2>/dev/null`.

**If you get "supervisord has no program named ...":** `SUPERVISOR_PROCESS`
doesn't match your `[program:NAME]`. Run `supervisorctl status` to see the
real names.

**⚠️ Upgrading from v1?** v1 was launched with `python self.py`, and that file
no longer exists. If your `[program:selfbot]` still references it, `self
restart` will stop the bot and fail to start it again. `self diag` checks this
for you and prints ❌ if the command is stale — update it to:

```ini
command=/home/youruser/Selfbot/.venv/bin/python -m selfbot
```

then `supervisorctl reread && supervisorctl update selfbot`.

A ready-to-copy stanza with every recommended setting lives in
[`deploy/supervisord-example.conf`](deploy/supervisord-example.conf), and
[`deploy/README.md`](deploy/README.md) covers the virtualenv question and
common failures.

**Non-default config location?** Only then do you need:

```env
SUPERVISOR_CONFIG=/home/youruser/supervisord.conf
```
</details>

### Step 12 — Merge the branch

When you're happy with it:

```bash
gh pr create --base main --head arena/019fa03c-selfbot \
  --title "Rebuild as modular async package" \
  --body "See README and SECURITY.md"
```

Or open it at
<https://github.com/ImTheAlireza/SelfBot/pull/new/arena/019fa03c-selfbot>.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Missing required configuration: SUDO_USER_ID` | Run `python -m selfbot --login` (step 5). |
| `Login failed: ApiIdInvalidError` | `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` don't match. Recheck my.telegram.org. |
| Commands do nothing | Send them from the **same account** the bot logged in as — Saved Messages is easiest. Check the bot isn't paused (`self on`). |
| `AI is not configured` | Step 8. |
| `supervisorctl could not be located` | Send `self diag`, then set `SUPERVISOR_CTL` or `pip install supervisor` in the venv. |
| `supervisord has no program named X` | `SUPERVISOR_PROCESS` must match `[program:NAME]`. Check with `supervisorctl status`. |
| Persian shows as boxes | Step 9. |
| `database is locked` | Two instances are running. `pkill -f "python -m selfbot"` and start one. |
| Need to re-login | Delete `data/*.session` and run `--login` again. |
| Want a command prefix | Set `COMMAND_PREFIX=.` in `.env`, then use `.help`. |

Run `python -m selfbot --log-level DEBUG` for verbose output.

---

## Daily use

```
help              full command list
help settimer     details for one command
whoami            your ID, role, current chat ID
status            uptime, providers, active timers
self off / self on   pause and resume
```

Commands work in any chat, typed from your own account. Nobody else sees them
unless you've added them with `setadmin`.

> **Reminder:** self-bots violate Telegram's Terms of Service. Keep the spam
> commands conservative, and don't use this on an account you can't afford to
> lose.
