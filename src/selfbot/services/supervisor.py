"""Locating and driving ``supervisorctl``.

Calling ``supervisorctl`` as a bare name only works when it happens to be on
``PATH``. It usually is not: supervisor is commonly pip-installed into the same
virtualenv as the bot, and a process started *by* supervisord inherits a
minimal environment. That produced a bare "not found on PATH" with no way to
fix it from configuration.

This module resolves the executable from several likely locations and, as a
last resort, runs the module directly with the current interpreter — which
works whenever ``supervisor`` is importable, regardless of ``PATH``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "ProgramAudit",
    "SupervisorNotFound",
    "SupervisorResult",
    "SupervisorRunner",
    "audit_program",
    "describe_discovery",
]

#: Absolute paths worth checking on shared hosts and typical Linux installs.
_COMMON_PATHS = (
    "/usr/local/bin/supervisorctl",
    "/usr/bin/supervisorctl",
    "/bin/supervisorctl",
    "/opt/supervisor/bin/supervisorctl",
)


class SupervisorNotFound(RuntimeError):
    """``supervisorctl`` could not be located by any strategy."""


@dataclass(slots=True)
class SupervisorResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        """Whichever stream carried the message."""
        return self.stdout or self.stderr


def _candidate_paths() -> list[Path]:
    """Places supervisorctl plausibly lives, most specific first."""
    candidates: list[Path] = []

    # The venv running this bot. If supervisor was pip-installed alongside the
    # bot, supervisorctl sits right next to the interpreter — the single most
    # likely location, and the one PATH lookup misses under supervisord.
    interpreter_dir = Path(sys.executable).resolve().parent
    candidates.append(interpreter_dir / "supervisorctl")

    # An active virtualenv that is not the running interpreter.
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        candidates.append(Path(venv) / "bin" / "supervisorctl")

    home = Path.home()
    candidates.append(home / ".local" / "bin" / "supervisorctl")

    # cPanel/CloudLinux keep per-application virtualenvs under ~/virtualenv.
    virtualenv_root = home / "virtualenv"
    if virtualenv_root.is_dir():
        try:
            for entry in sorted(virtualenv_root.rglob("bin/supervisorctl"))[:10]:
                candidates.append(entry)
        except OSError:  # pragma: no cover - permission quirks on shared hosts
            pass

    candidates.extend(Path(p) for p in _COMMON_PATHS)
    return candidates


def _is_runnable(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:  # pragma: no cover - unreadable mount
        return False


def _module_fallback() -> list[str] | None:
    """Run supervisorctl through the current interpreter, if importable."""
    from importlib.util import find_spec

    try:
        if find_spec("supervisor.supervisorctl") is not None:
            return [sys.executable, "-m", "supervisor.supervisorctl"]
    except (ImportError, ValueError):  # pragma: no cover - broken install
        pass
    return None


def resolve_supervisorctl(explicit: str = "") -> list[str] | None:
    """Return the argv prefix that invokes supervisorctl, or ``None``.

    ``explicit`` (``SUPERVISOR_CTL``) wins when set, and may be either an
    absolute path or a bare command name to look up on ``PATH``.
    """
    if explicit:
        candidate = Path(explicit).expanduser()
        if _is_runnable(candidate):
            return [str(candidate)]
        found = shutil.which(explicit)
        if found:
            return [found]
        logger.warning("SUPERVISOR_CTL=%r is not executable; falling back", explicit)

    found = shutil.which("supervisorctl")
    if found:
        return [found]

    for candidate in _candidate_paths():
        if _is_runnable(candidate):
            return [str(candidate)]

    return _module_fallback()


def describe_discovery(explicit: str = "") -> str:
    """A human-readable report of what was searched. Used by ``self diag``."""
    lines: list[str] = []

    if explicit:
        candidate = Path(explicit).expanduser()
        mark = "✅" if _is_runnable(candidate) else "❌"
        lines.append(f"{mark} SUPERVISOR_CTL: `{explicit}`")

    on_path = shutil.which("supervisorctl")
    lines.append(
        f"{'✅' if on_path else '❌'} PATH lookup: "
        + (f"`{on_path}`" if on_path else "not found")
    )

    for candidate in _candidate_paths():
        if _is_runnable(candidate):
            lines.append(f"✅ Found: `{candidate}`")
            break
    else:
        lines.append("❌ Common locations: none executable")

    module = _module_fallback()
    lines.append(
        f"{'✅' if module else '❌'} Python module: "
        + ("`python -m supervisor.supervisorctl`" if module else "not importable")
    )
    return "\n".join(lines)


class SupervisorRunner:
    """Runs supervisorctl subcommands for a configured process."""

    def __init__(
        self,
        *,
        process_name: str,
        config_path: str = "",
        executable: str = "",
    ) -> None:
        self.process_name = process_name
        self.config_path = config_path
        self._explicit = executable
        self._resolved: list[str] | None = None

    def resolve(self) -> list[str]:
        if self._resolved is None:
            found = resolve_supervisorctl(self._explicit)
            if found is None:
                raise SupervisorNotFound(
                    "supervisorctl could not be located. Set `SUPERVISOR_CTL` "
                    "in your .env to its absolute path, or install it with "
                    "`pip install supervisor` inside the bot's virtualenv."
                )
            self._resolved = found
            logger.info("Using supervisorctl: %s", " ".join(found))
        return self._resolved

    def build_command(self, *args: str) -> list[str]:
        command = list(self.resolve())
        # `-c` is optional: supervisorctl finds /etc/supervisord.conf and
        # ./supervisord.conf on its own when no path is given.
        if self.config_path:
            command += ["-c", self.config_path]
        command += list(args)
        return command

    async def run(
        self,
        *args: str,
        timeout: float = 30.0,
        detached: bool = False,
    ) -> SupervisorResult:
        """Execute a subcommand without blocking the event loop.

        ``detached`` starts the child in its own session so it survives the
        bot being signalled — essential for ``restart``, where supervisord
        terminates the bot while supervisorctl is still working.
        """
        command = self.build_command(*args)

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=detached,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            with_suppress = getattr(process, "kill", None)
            if with_suppress is not None:
                try:
                    process.kill()
                except ProcessLookupError:  # pragma: no cover - already gone
                    pass
            raise

        return SupervisorResult(
            returncode=process.returncode or 0,
            stdout=stdout.decode(errors="replace").strip(),
            stderr=stderr.decode(errors="replace").strip(),
        )

    async def status(self, timeout: float = 20.0) -> SupervisorResult:
        return await self.run("status", self.process_name, timeout=timeout)

    async def restart(self, timeout: float = 60.0) -> SupervisorResult:
        return await self.run(
            "restart", self.process_name, timeout=timeout, detached=True
        )

    async def stop(self, timeout: float = 30.0) -> SupervisorResult:
        return await self.run("stop", self.process_name, timeout=timeout, detached=True)

    async def start(self, timeout: float = 30.0) -> SupervisorResult:
        return await self.run("start", self.process_name, timeout=timeout)


@dataclass(slots=True)
class ProgramAudit:
    """What a supervisord ``[program:...]`` section says about our process."""

    found: bool = False
    config_file: str = ""
    command: str = ""
    directory: str = ""
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _iter_config_files(config_path: str) -> list[Path]:
    """The supervisord config plus anything it pulls in via [include]."""
    candidates: list[Path] = []
    if config_path:
        candidates.append(Path(config_path).expanduser())
    else:
        candidates += [
            Path("/etc/supervisord.conf"),
            Path("/etc/supervisor/supervisord.conf"),
            Path.home() / "supervisord.conf",
            Path.cwd() / "supervisord.conf",
        ]

    resolved: list[Path] = []
    for base in candidates:
        if not base.is_file():
            continue
        resolved.append(base)
        try:
            text = base.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Follow [include] files= globs, where per-app programs usually live.
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.lower().startswith("files"):
                continue
            _, _, patterns = stripped.partition("=")
            for pattern in patterns.split():
                pattern = pattern.strip()
                if not pattern:
                    continue
                target = Path(pattern).expanduser()
                if not target.is_absolute():
                    target = base.parent / target
                try:
                    resolved.extend(sorted(target.parent.glob(target.name))[:20])
                except (OSError, ValueError):
                    continue
    return resolved


def audit_program(process_name: str, config_path: str = "") -> ProgramAudit:
    """Inspect the ``[program:NAME]`` section and sanity-check its command.

    The v1 bot was launched with ``python self.py``. That file no longer
    exists in v2, so an un-migrated supervisord config will happily stop the
    bot and then fail to start it again. Catching that here turns a confusing
    outage into a clear warning.
    """
    import configparser

    audit = ProgramAudit()
    section = f"program:{process_name}"

    for config_file in _iter_config_files(config_path):
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read(config_file, encoding="utf-8")
        except (configparser.Error, OSError):
            continue
        if not parser.has_section(section):
            continue

        audit.found = True
        audit.config_file = str(config_file)
        audit.command = parser.get(section, "command", fallback="").strip()
        audit.directory = parser.get(section, "directory", fallback="").strip()
        break

    if not audit.found:
        return audit

    command = audit.command
    if not command:
        audit.problems.append("No `command=` set for this program.")
        return audit

    # The critical check: does it still launch the deleted v1 entry point?
    if "self.py" in command:
        audit.problems.append(
            "`command` runs `self.py`, which no longer exists in v2. "
            "Change it to `-m selfbot` or the bot will not come back up."
        )
    elif "-m selfbot" not in command and "/selfbot" not in command:
        audit.notes.append(
            "`command` does not obviously launch this bot — double-check it."
        )

    # Verify the interpreter or executable actually exists.
    executable = command.split()[0] if command.split() else ""
    if executable.startswith("/") and not Path(executable).exists():
        audit.problems.append(f"`{executable}` does not exist.")

    if audit.directory and not Path(audit.directory).expanduser().is_dir():
        audit.problems.append(f"`directory={audit.directory}` does not exist.")

    return audit


def parse_state(output: str) -> str:
    """Extract the process state token from a ``supervisorctl status`` line."""
    for state in (
        "RUNNING", "STARTING", "STOPPING", "STOPPED",
        "BACKOFF", "FATAL", "EXITED", "UNKNOWN",
    ):
        if state in output:
            return state
    return "UNKNOWN"


STATE_EMOJI = {
    "RUNNING": "✅",
    "STARTING": "🔄",
    "STOPPING": "🔻",
    "STOPPED": "⏹",
    "BACKOFF": "⚠️",
    "FATAL": "❌",
    "EXITED": "🚪",
    "UNKNOWN": "❓",
}
