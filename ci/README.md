# CI workflow

`github-actions.yml` is the CI pipeline for this project. It lives here rather
than in `.github/workflows/` because the GitHub App used to push this branch
does not hold the `workflows` permission, so it cannot create workflow files.

**Activate it with one command:**

```bash
mkdir -p .github/workflows
git mv ci/github-actions.yml .github/workflows/ci.yml
git commit -m "Enable CI workflow"
git push
```

## What it runs

| Job | Purpose |
|---|---|
| **lint** | `ruff check`, format check and `mypy` on `src/` and `tests/` |
| **test** | Full suite on Python 3.10, 3.11 and 3.12, with coverage |
| **smoke** | `python -m selfbot --check` — proves config loads and all 59 commands register |
| **secrets** | Fails the build if anything resembling an API hash, bot token or hardcoded password is committed |

The secret scan is the one that matters most here: v1 leaked live credentials
into git, and this job exists so it cannot happen again. It catches all three
shapes the original file used:

```python
{'api_hash': '<32 hex chars>'}                    # dict literal
TOKEN = "12345678:AA<35 chars>"                   # Telegram bot token
os.getenv('DB_PASSWORD', '<fallback>')            # getenv default
```

Run it locally before pushing:

```bash
q="[\"']"
patterns="(api_hash|api_id|password|secret|token)${q}?[[:space:]]*[:=][[:space:]]*${q}[0-9a-zA-Z_-]{16,}${q}"
patterns="${patterns}|[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}"
patterns="${patterns}|getenv\([^)]*,[[:space:]]*${q}[0-9a-zA-Z_-]{12,}${q}[[:space:]]*\)"
git grep -InE "$patterns" -- ':!*.md' ':!.github/*' ':!.env.example' \
  && echo "SECRETS FOUND" || echo "clean"
```
