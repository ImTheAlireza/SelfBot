# Deploying with supervisor

## Do I need to activate the virtualenv?

**No — and you shouldn't try.** Supervisor doesn't run your shell, so
`source .venv/bin/activate` has no effect on it. Activation only sets `PATH`
for your current terminal.

Instead, name the venv's interpreter directly in `command`. That is exactly
equivalent to activating, and it survives reboots and daemon restarts:

```ini
command=/home/YOURUSER/self/public/Selfbot/.venv/bin/python -m selfbot
```

You only activate the venv for **manual** work — `--login`, `--check`,
`pip install`. For those, `source .venv/bin/activate` then `deactivate` when
done, or just call `.venv/bin/python` directly and skip activation entirely.

## The two mistakes that break v2

A config carried over from v1 fails for two independent reasons:

```ini
command=python3 /home/YOURUSER/self/public/Selfbot/self.py
        ^^^^^^^                                    ^^^^^^^
        (1) system python                          (2) deleted file
```

1. **Bare `python3`** resolves to `/usr/bin/python3`, which does not have
   `telethon`, `aiohttp` or `aiosqlite` installed. Even with a correct script
   path you get `ModuleNotFoundError: No module named 'telethon'`.

2. **`self.py` no longer exists.** v2 replaced it with the `src/selfbot`
   package. supervisord reports:
   `python3: can't open file 'self.py': [Errno 2] No such file or directory`,
   then `BACKOFF` → `FATAL`.

Both are fixed by the single `command` line above.

## Applying the change

```bash
# 1. Back up first
cp ~/supervisord.conf ~/supervisord.conf.bak

# 2. Edit the [program:selfbot] section — see supervisord-example.conf
nano ~/supervisord.conf

# 3. Load the new config without disturbing your other programs
supervisorctl reread
supervisorctl update selfbot

# 4. Confirm
supervisorctl status selfbot
```

`update selfbot` restarts only that program. A bare `update` would restart
every changed program, which you don't want when other bots share the file.

## Log in before starting the service

The bot needs a valid `.session` file. Supervisor has no terminal, so it
cannot answer Telegram's login code prompt.

```bash
supervisorctl stop selfbot
cd /home/YOURUSER/self/public/Selfbot
.venv/bin/python -m selfbot --login     # enter the code
supervisorctl start selfbot
```

If you skip this, the log says exactly what to do:

```
Not logged in, and there is no terminal to log in from.
The session file is missing or expired: .../data/selfbot.session

Stop the service, run `python -m selfbot --login` from a shell in the
project directory, then start it again.
```

## Verifying

From Telegram:

| Command | Expected |
|---|---|
| `self diag` | ✅ on every line, and `✅ Looks correct for v2.` |
| `self status` | `✅ selfbot — RUNNING` |
| `self logs 20` | Recent log lines |
| `self restart` | Bot goes quiet ~10s, then answers `ping` again |

Run `self diag` **before** `self restart`. It audits the `[program:...]`
command and warns you if a restart would fail to bring the bot back.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `can't open file 'self.py'` | v1 command | Use `-m selfbot` |
| `ModuleNotFoundError: telethon` | System python | Use the venv's python |
| `BACKOFF`/`FATAL` right after start | Crash on boot | `self logs 40`, or `tail -40 ~/logs/selfbot.err.log` |
| `database is locked` | Two instances running | Add `stopasgroup=true`, then `supervisorctl stop selfbot; pkill -f "python -m selfbot"; supervisorctl start selfbot` |
| Bot runs but ignores commands | Not signed in as you | `whoami` should match `SUDO_USER_ID` |
| `no such process` | Name mismatch | `SUPERVISOR_PROCESS` must equal `[program:NAME]` |
