# Security

## ⚠️ Action required: rotate the leaked credentials

The v1 `self.py` had live credentials hardcoded, and that file is in this
repository's git history. **Deleting the file does not remove it from history** —
anyone who has cloned the repo, or any fork, cache or mirror, still has them.

Treat all of the following as **compromised** and rotate them now:

| Credential | Where it was | How to rotate |
|---|---|---|
| Telegram `api_hash` | `TELEGRAM_CONFIG` | [my.telegram.org](https://my.telegram.org) → API development tools |
| Telegram phone number | `TELEGRAM_CONFIG` | Personal data — can't be rotated; be aware it's public |
| Sticker helper bot token | `STICKER_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) → `/revoke` |
| RapidAPI key | `API_KEYS['rapidapi']` | [RapidAPI dashboard](https://rapidapi.com/developer/security) → regenerate |
| CoinMarketCap key | `API_KEYS['coinmarketcap']` | [CoinMarketCap](https://pro.coinmarketcap.com/account) → regenerate |
| MySQL password | `get_db_cursor()` | `ALTER USER ... IDENTIFIED BY '<new>';` |

Also **terminate old Telegram sessions**: Telegram → Settings → Devices →
Terminate all other sessions. A leaked `api_hash` alone does not grant account
access, but a leaked `.session` file does.

## Purging the credentials from git history

This rewrites history, so it must be done by someone who can force-push `main`.
Coordinate with anyone else working on the repo first — everyone will need to
re-clone.

```bash
# 1. Install the tool
pip install git-filter-repo

# 2. Fresh, full clone (filter-repo refuses to run on a shallow or dirty clone)
git clone https://github.com/ImTheAlireza/SelfBot.git selfbot-clean
cd selfbot-clean

# 3. Drop the file that contained the secrets from every commit
git filter-repo --invert-paths --path self.py

# 4. Verify nothing remains
git log --all -p | grep -iE '9510290042|8176933750|a58dc11fa1|4c0146c41f|cc4c01e3' \
  && echo "STILL PRESENT" || echo "clean"

# 5. Force-push the rewritten history
git remote add origin https://github.com/ImTheAlireza/SelfBot.git
git push --force --all origin
git push --force --tags origin
```

Then, in the GitHub UI, delete any forks you control and ask GitHub Support to
purge cached views of the old commit if the repository is public.

> Rotating the credentials matters more than purging history. Do step one first.

## How v2 prevents this

- No secret appears in source. Everything loads from the environment via
  `config.py`, and `.env` is gitignored.
- `.env.example` documents every variable with placeholder values only.
- CI runs a secret scan that fails the build on anything resembling a committed
  API hash or bot token.
- `config.describe()` redacts credentials before they reach logs, so database
  URLs with passwords are never printed.
- `.session` files are gitignored — they grant complete account access.

## Other hardening in v2

| Issue | Mitigation |
|---|---|
| Zip-slip via `extractall` on untrusted archives | `utils.files.safe_extract` validates every member: rejects `../`, absolute paths, drive letters and symlink escapes |
| Zip bombs | Declared expansion size and file count are capped before extraction |
| Path traversal via `rename` / `metadata` | `sanitize_filename` reduces input to a single component and strips reserved names |
| Unbounded downloads | `MAX_FILE_SIZE_MB` enforced from `Content-Length` and again while streaming |
| SQL injection | All queries use bound parameters; no string interpolation |
| Unbounded bulk delete | `purge` defaults to your own messages, caps the scan, and requires confirmation |
| Command injection | Subprocesses use `create_subprocess_exec` with argument lists, never `shell=True` |

## Reporting a vulnerability

Please open a [private security advisory](https://github.com/ImTheAlireza/SelfBot/security/advisories/new)
rather than a public issue.

## Reminder

Running a self-bot violates Telegram's Terms of Service and can get your account
limited or banned. Your `.session` file is equivalent to your password — anyone
holding it can read and send your messages. Never commit it, share it, or store
it in a public volume.
