"""Transactional project updates from public GitHub branch URLs.

The updater clones a selected branch into a sibling staging directory, validates
its shape and Python syntax, then replaces every non-runtime top-level entry in
the configured project directory. The project root itself is never renamed, so
a running process whose current working directory is the project remains valid.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import re
import shutil
import tempfile
import tokenize
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

__all__ = [
    "GitHubBranch",
    "ProjectUpdateError",
    "ProjectUpdateResult",
    "ProjectUpdater",
    "parse_getcode_request",
    "parse_github_branch_url",
    "resolve_update_target",
]

logger = logging.getLogger(__name__)

_MAX_FILES = 10_000
_MAX_BYTES = 128 * 1024 * 1024
_UPDATE_LOCK = asyncio.Lock()
_GITHUB_PART = re.compile(r"^[A-Za-z0-9_.-]+$")
_DESTINATION_FOLDER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MARKDOWN_LINK = re.compile(r"^\[[^\]]+\]\((https://[^)]+)\)$")
_MARKDOWN_REQUEST = re.compile(r"^\[[^\]]+\]\((https://[^)]+)\)\s+(.+)$")
_BRACKET_REQUEST = re.compile(r"^\[(https://[^\]]+)\]\s+(.+)$")
_PLAIN_REQUEST = re.compile(r"^(https://\S+)\s+(.+)$")
_PRESERVE_EXACT = frozenset(
    {
        ".env",
        ".venv",
        "venv",
        "data",
        "logs",
        "secret.key",
    }
)


class ProjectUpdateError(RuntimeError):
    """A safe, user-facing project update failure."""


@dataclass(frozen=True, slots=True)
class GitHubBranch:
    owner: str
    repository: str
    branch: str

    @property
    def clone_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repository}.git"

    @property
    def label(self) -> str:
        return f"{self.owner}/{self.repository}@{self.branch}"


@dataclass(frozen=True, slots=True)
class ProjectUpdateResult:
    source: GitHubBranch
    target: Path
    commit: str
    files: int
    bytes: int


def parse_getcode_request(value: str) -> tuple[str, str]:
    """Return ``(branch_url, destination_folder)`` from command arguments.

    Plain URLs, bracketed URLs and Telegram Markdown links are accepted. Common
    formatting wrappers around the complete command argument are ignored.
    """
    value = _strip_formatting_wrapper(value.strip())
    match = (
        _MARKDOWN_REQUEST.fullmatch(value)
        or _BRACKET_REQUEST.fullmatch(value)
        or _PLAIN_REQUEST.fullmatch(value)
    )
    if match is None:
        raise ProjectUpdateError("Usage: `getcode <GitHub branch URL> <destination folder>`.")

    branch_url = match.group(1).strip()
    destination = _strip_formatting_wrapper(match.group(2).strip())
    _validate_destination_folder(destination)
    return branch_url, destination


def _strip_formatting_wrapper(value: str) -> str:
    wrappers = (("++**", "**++"), ("**", "**"), ("++", "++"), ("__", "__"), ("`", "`"))
    changed = True
    while changed:
        changed = False
        for prefix, suffix in wrappers:
            if value.startswith(prefix) and value.endswith(suffix):
                value = value[len(prefix) : -len(suffix)].strip()
                changed = True
                break
    return value


def _validate_destination_folder(destination: str) -> None:
    if (
        not _DESTINATION_FOLDER.fullmatch(destination)
        or destination in {".", ".."}
        or destination.endswith(".lock")
    ):
        raise ProjectUpdateError(
            "Destination must be one safe folder name using letters, numbers, '.', '-' or '_'."
        )


def resolve_update_target(root: Path, destination: str) -> Path:
    """Resolve one direct child of the configured update root."""
    _validate_destination_folder(destination)
    root = Path(os.path.abspath(Path(root).expanduser()))
    if root.is_symlink():
        raise ProjectUpdateError("The configured project update root cannot be a symlink.")
    target = root / destination
    if target.parent != root:
        raise ProjectUpdateError("Destination escapes the configured update root.")
    return target


def parse_github_branch_url(value: str) -> GitHubBranch:
    """Parse ``https://github.com/<owner>/<repo>/tree/<branch>`` safely.

    Encoded slashes in GitHub branch URLs (``arena%2F...``) and a Telegram
    Markdown link wrapper are both accepted.
    """
    value = value.strip()
    markdown = _MARKDOWN_LINK.fullmatch(value)
    if markdown:
        value = markdown.group(1)
    value = value.strip("<>[]")

    parsed = urlsplit(value)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in {
        "github.com",
        "www.github.com",
    }:
        raise ProjectUpdateError("Use an HTTPS GitHub branch URL.")
    if parsed.username or parsed.password:
        raise ProjectUpdateError("GitHub URLs containing credentials are not allowed.")

    parts = parsed.path.strip("/").split("/")
    if len(parts) < 4 or parts[2].lower() != "tree":
        raise ProjectUpdateError("Expected `https://github.com/<owner>/<repo>/tree/<branch>`.")

    owner = unquote(parts[0])
    repository = unquote(parts[1])
    if repository.endswith(".git"):
        repository = repository[:-4]
    branch = unquote("/".join(parts[3:])).strip("/")

    if not _GITHUB_PART.fullmatch(owner) or not _GITHUB_PART.fullmatch(repository):
        raise ProjectUpdateError("Invalid GitHub owner or repository name.")
    _validate_branch(branch)
    return GitHubBranch(owner=owner, repository=repository, branch=branch)


def _validate_branch(branch: str) -> None:
    invalid = (
        not branch
        or len(branch) > 255
        or branch.startswith(("-", "."))
        or branch.endswith(("/", ".", ".lock"))
        or ".." in branch
        or "//" in branch
        or "@{" in branch
        or any(ord(char) < 32 or char.isspace() for char in branch)
        or any(char in branch for char in "~^:?*[\\")
    )
    if invalid:
        raise ProjectUpdateError("Invalid or unsafe Git branch name.")


class ProjectUpdater:
    """Download, validate and transactionally replace a project checkout."""

    def __init__(self, target: Path) -> None:
        # abspath normalizes a relative path without dereferencing a final
        # symlink; _prepare_parent can therefore reject symlink targets.
        self.target = Path(os.path.abspath(Path(target).expanduser()))

    async def update(self, branch_url: str) -> ProjectUpdateResult:
        source = parse_github_branch_url(branch_url)

        async with _UPDATE_LOCK:
            await asyncio.to_thread(self._prepare_parent)
            workspace = Path(
                await asyncio.to_thread(
                    tempfile.mkdtemp,
                    prefix=f".{self.target.name}-update-",
                    dir=self.target.parent,
                )
            )
            checkout = workspace / "checkout"
            try:
                commit = await self._clone(source, checkout)
                files, total_bytes = await asyncio.to_thread(_validate_checkout, checkout)
                await asyncio.to_thread(_replace_project_contents, checkout, self.target)
            finally:
                await asyncio.to_thread(shutil.rmtree, workspace, True)

        return ProjectUpdateResult(
            source=source,
            target=self.target,
            commit=commit,
            files=files,
            bytes=total_bytes,
        )

    def _prepare_parent(self) -> None:
        if self.target == Path(self.target.anchor):
            raise ProjectUpdateError("Refusing to replace a filesystem root.")
        if self.target.is_symlink():
            raise ProjectUpdateError("The project update directory cannot be a symlink.")
        if self.target.exists() and not self.target.is_dir():
            raise ProjectUpdateError("The project update path is not a directory.")
        self.target.parent.mkdir(parents=True, exist_ok=True)

    async def _clone(self, source: GitHubBranch, checkout: Path) -> str:
        env = dict(os.environ)
        env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"})
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                "clone",
                "--depth",
                "1",
                "--single-branch",
                "--no-tags",
                "--branch",
                source.branch,
                "--",
                source.clone_url,
                str(checkout),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError:
            raise ProjectUpdateError("`git` is not installed on this server.") from None
        try:
            _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=180)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise ProjectUpdateError("GitHub download timed out after 180 seconds.") from None

        if process.returncode != 0:
            detail = stderr.decode(errors="replace").strip().splitlines()
            message = detail[-1] if detail else "unknown git error"
            raise ProjectUpdateError(f"Could not download the branch: {message}")

        code, stdout, stderr_text = await _run_process(
            "git", "-C", str(checkout), "rev-parse", "--short=12", "HEAD"
        )
        if code != 0 or not stdout:
            raise ProjectUpdateError(
                f"Could not identify the downloaded commit: {stderr_text or 'unknown error'}"
            )
        return stdout


async def _run_process(*args: str) -> tuple[int, str, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise ProjectUpdateError(f"Required executable `{args[0]}` was not found.") from None
    stdout, stderr = await process.communicate()
    return (
        process.returncode or 0,
        stdout.decode(errors="replace").strip(),
        stderr.decode(errors="replace").strip(),
    )


def _validate_checkout(checkout: Path) -> tuple[int, int]:
    """Reject malformed, unexpectedly large or unsafe project snapshots."""
    files = 0
    total_bytes = 0
    looks_like_code = False
    project_markers = {
        "pyproject.toml",
        "requirements.txt",
        "setup.py",
        "setup.cfg",
        "package.json",
        "composer.json",
        "go.mod",
        "Cargo.toml",
        "Dockerfile",
        "pom.xml",
        "build.gradle",
        "Gemfile",
        "mix.exs",
    }
    code_suffixes = {
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".php",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".rb",
        ".cs",
        ".sh",
    }

    for path in checkout.rglob("*"):
        relative = path.relative_to(checkout)
        if relative.parts and relative.parts[0] == ".git":
            continue
        if path.is_symlink():
            raise ProjectUpdateError(f"Downloaded project contains a symlink: {relative}")
        if not path.is_file():
            continue
        files += 1
        total_bytes += path.stat().st_size
        if path.name in project_markers or path.suffix.lower() in code_suffixes:
            looks_like_code = True
        if files > _MAX_FILES:
            raise ProjectUpdateError(
                f"Downloaded project has too many files (more than {_MAX_FILES:,})."
            )
        if total_bytes > _MAX_BYTES:
            raise ProjectUpdateError("Downloaded project is larger than the 128 MiB safety limit.")
        if path.suffix == ".py":
            try:
                with tokenize.open(path) as source:
                    ast.parse(source.read(), filename=str(relative))
            except (OSError, SyntaxError, UnicodeError) as exc:
                raise ProjectUpdateError(
                    f"Python validation failed for `{relative}`: {exc}"
                ) from exc

    if not files or not looks_like_code:
        raise ProjectUpdateError("Downloaded branch does not look like a supported code project.")
    return files, total_bytes


def _is_preserved(name: str) -> bool:
    if name in _PRESERVE_EXACT:
        return True
    if name.startswith(".env.") and name != ".env.example":
        return True
    return name.endswith((".session", ".session-journal"))


def _replace_project_contents(checkout: Path, target: Path) -> None:
    """Replace target contents while retaining runtime state and root inode.

    Existing code is first moved to a sibling backup. If any installation move
    fails, newly installed entries are removed and every old entry is restored.
    """
    if target.is_symlink():
        raise ProjectUpdateError("The project update directory cannot be a symlink.")
    target.mkdir(parents=True, exist_ok=True)
    backup = target.parent / f".{target.name}-backup-{uuid.uuid4().hex}"
    backup.mkdir()
    installed: list[Path] = []
    moved_old: list[Path] = []

    try:
        for entry in list(target.iterdir()):
            if _is_preserved(entry.name):
                continue
            destination = backup / entry.name
            entry.rename(destination)
            moved_old.append(destination)

        for entry in list(checkout.iterdir()):
            if _is_preserved(entry.name):
                continue
            destination = target / entry.name
            entry.rename(destination)
            installed.append(destination)
    except Exception as exc:
        rollback_errors: list[str] = []
        for entry in reversed(installed):
            try:
                _remove_entry(entry)
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        for old_entry in moved_old:
            try:
                old_entry.rename(target / old_entry.name)
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        if rollback_errors:
            logger.critical("Project update rollback errors: %s", rollback_errors)
            raise ProjectUpdateError(
                "Project replacement failed and rollback was incomplete; check server files immediately."
            ) from exc
        shutil.rmtree(backup, ignore_errors=True)
        raise ProjectUpdateError(f"Project replacement failed and was rolled back: {exc}") from exc
    else:
        shutil.rmtree(backup, ignore_errors=True)


def _remove_entry(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
