"""Supervisor discovery and control.

The bug these lock down: ``supervisorctl`` was invoked as a bare command name,
so it had to be on ``PATH``. Under supervisord the child inherits a minimal
environment, and on shared hosts supervisor is usually pip-installed into a
virtualenv — so the lookup failed with an unactionable "not found on PATH".
"""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path

import pytest

from conftest import FakeEvent
from selfbot.services import supervisor as sup
from selfbot.services.supervisor import (
    STATE_EMOJI,
    SupervisorNotFound,
    SupervisorRunner,
    describe_discovery,
    parse_state,
    resolve_supervisorctl,
)


def make_executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """No PATH, no venv, no importable supervisor — a blank slate."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setattr(sup, "_COMMON_PATHS", ())
    monkeypatch.setattr(sup, "_module_fallback", lambda: None)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    return tmp_path


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_explicit_absolute_path_wins(isolated_env, monkeypatch):
    target = make_executable(isolated_env / "custom" / "supervisorctl")
    assert resolve_supervisorctl(str(target)) == [str(target)]


def test_finds_binary_next_to_interpreter(isolated_env, monkeypatch):
    """The case that broke: pip-installed into the bot's own virtualenv."""
    venv_bin = isolated_env / "venv" / "bin"
    make_executable(venv_bin / "supervisorctl")
    monkeypatch.setattr(sup.sys, "executable", str(venv_bin / "python3"))

    assert resolve_supervisorctl() == [str(venv_bin / "supervisorctl")]


def test_finds_binary_in_active_virtualenv(isolated_env, monkeypatch):
    venv = isolated_env / "othervenv"
    make_executable(venv / "bin" / "supervisorctl")
    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    monkeypatch.setattr(sup.sys, "executable", str(isolated_env / "nowhere" / "python"))

    assert resolve_supervisorctl() == [str(venv / "bin" / "supervisorctl")]


def test_finds_binary_in_cpanel_virtualenv_tree(isolated_env, monkeypatch):
    """cPanel/CloudLinux nest per-app virtualenvs under ~/virtualenv."""
    home = isolated_env / "home"
    target = make_executable(
        home / "virtualenv" / "selfbot" / "3.11" / "bin" / "supervisorctl"
    )
    monkeypatch.setattr(sup.sys, "executable", str(isolated_env / "nowhere" / "python"))

    assert resolve_supervisorctl() == [str(target)]


def test_falls_back_to_python_module(isolated_env, monkeypatch):
    """Works whenever `supervisor` is importable, whatever PATH says."""
    monkeypatch.setattr(
        sup, "_module_fallback", lambda: ["/usr/bin/python3", "-m", "supervisor.supervisorctl"]
    )
    monkeypatch.setattr(sup.sys, "executable", str(isolated_env / "nowhere" / "python"))

    assert resolve_supervisorctl() == [
        "/usr/bin/python3", "-m", "supervisor.supervisorctl",
    ]


def test_returns_none_when_truly_absent(isolated_env, monkeypatch):
    monkeypatch.setattr(sup.sys, "executable", str(isolated_env / "nowhere" / "python"))
    assert resolve_supervisorctl() is None


def test_bad_explicit_path_falls_back(isolated_env, monkeypatch):
    """A typo'd SUPERVISOR_CTL should not disable discovery entirely."""
    venv_bin = isolated_env / "venv" / "bin"
    make_executable(venv_bin / "supervisorctl")
    monkeypatch.setattr(sup.sys, "executable", str(venv_bin / "python3"))

    assert resolve_supervisorctl("/no/such/supervisorctl") == [
        str(venv_bin / "supervisorctl")
    ]


def test_non_executable_file_is_rejected(isolated_env, monkeypatch):
    path = isolated_env / "plain" / "supervisorctl"
    path.parent.mkdir(parents=True)
    path.write_text("not executable")
    monkeypatch.setattr(sup.sys, "executable", str(isolated_env / "nowhere" / "python"))

    assert resolve_supervisorctl(str(path)) is None


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


def test_config_path_is_omitted_when_unset(isolated_env):
    target = make_executable(isolated_env / "supervisorctl")
    runner = SupervisorRunner(process_name="selfbot", executable=str(target))
    # No -c: supervisorctl then finds its own config, which is what we want
    # when SUPERVISOR_CONFIG is not set.
    assert runner.build_command("status", "selfbot") == [
        str(target), "status", "selfbot",
    ]


def test_config_path_is_passed_when_set(isolated_env):
    target = make_executable(isolated_env / "supervisorctl")
    runner = SupervisorRunner(
        process_name="selfbot",
        config_path="/home/me/supervisord.conf",
        executable=str(target),
    )
    assert runner.build_command("status", "selfbot") == [
        str(target), "-c", "/home/me/supervisord.conf", "status", "selfbot",
    ]


def test_resolve_raises_actionable_error(isolated_env, monkeypatch):
    monkeypatch.setattr(sup.sys, "executable", str(isolated_env / "nowhere" / "python"))
    runner = SupervisorRunner(process_name="selfbot")
    with pytest.raises(SupervisorNotFound, match="SUPERVISOR_CTL"):
        runner.resolve()


# ---------------------------------------------------------------------------
# Execution against a stub binary
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_ctl(tmp_path):
    return make_executable(
        tmp_path / "supervisorctl",
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *status*)  echo "selfbot   RUNNING   pid 4242, uptime 1:02:03"; exit 0 ;;\n'
        '  *restart*) echo "selfbot: stopped"; echo "selfbot: started"; exit 0 ;;\n'
        '  *)         echo "unknown" >&2; exit 2 ;;\n'
        "esac\n",
    )


@pytest.mark.asyncio
async def test_status_parses_running_state(stub_ctl):
    runner = SupervisorRunner(process_name="selfbot", executable=str(stub_ctl))
    result = await runner.status()

    assert result.ok
    assert parse_state(result.output) == "RUNNING"
    assert STATE_EMOJI["RUNNING"] == "✅"


@pytest.mark.asyncio
async def test_restart_reports_success(stub_ctl):
    runner = SupervisorRunner(process_name="selfbot", executable=str(stub_ctl))
    result = await runner.restart()

    assert result.ok
    assert "started" in result.output


@pytest.mark.asyncio
async def test_failure_exit_code_is_surfaced(stub_ctl):
    runner = SupervisorRunner(process_name="selfbot", executable=str(stub_ctl))
    result = await runner.run("bogus")

    assert not result.ok
    assert result.returncode == 2


@pytest.mark.asyncio
async def test_timeout_is_raised_not_swallowed(tmp_path):
    slow = make_executable(tmp_path / "supervisorctl", "#!/bin/sh\nsleep 5\n")
    runner = SupervisorRunner(process_name="selfbot", executable=str(slow))

    with pytest.raises(asyncio.TimeoutError):
        await runner.run("status", timeout=0.3)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("selfbot RUNNING pid 1, uptime 0:00:05", "RUNNING"),
        ("selfbot STOPPED Not started", "STOPPED"),
        ("selfbot FATAL Exited too quickly", "FATAL"),
        ("selfbot BACKOFF Exited too quickly", "BACKOFF"),
        ("selfbot STARTING", "STARTING"),
        ("something unparseable", "UNKNOWN"),
    ],
)
def test_parse_state(output, expected):
    assert parse_state(output) == expected


def test_every_state_has_an_emoji():
    for state in (
        "RUNNING", "STARTING", "STOPPING", "STOPPED",
        "BACKOFF", "FATAL", "EXITED", "UNKNOWN",
    ):
        assert state in STATE_EMOJI


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def test_describe_discovery_reports_each_strategy(isolated_env, monkeypatch):
    monkeypatch.setattr(sup.sys, "executable", str(isolated_env / "nowhere" / "python"))
    report = describe_discovery("")

    assert "PATH lookup" in report
    assert "Python module" in report


def test_describe_discovery_flags_bad_explicit_path(isolated_env, monkeypatch):
    monkeypatch.setattr(sup.sys, "executable", str(isolated_env / "nowhere" / "python"))
    report = describe_discovery("/no/such/binary")

    assert "SUPERVISOR_CTL" in report
    assert "❌" in report


# ---------------------------------------------------------------------------
# Command integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_command_reports_running(bot, registry, stub_ctl):
    import dataclasses

    bot.config = dataclasses.replace(
        bot.config,
        supervisor=dataclasses.replace(
            bot.config.supervisor,
            process_name="selfbot",
            executable=str(stub_ctl),
        ),
    )

    event = FakeEvent(raw_text="self status")
    await registry.dispatch(bot, event, "self status")

    output = " ".join(event.replies)
    assert "RUNNING" in output
    assert "✅" in output


@pytest.mark.asyncio
async def test_status_command_without_process_name_explains(bot, registry):
    event = FakeEvent(raw_text="self status")
    await registry.dispatch(bot, event, "self status")
    assert any("SUPERVISOR_PROCESS" in r for r in event.replies)


@pytest.mark.asyncio
async def test_restart_command_asks_before_acting(bot, registry, stub_ctl):
    import dataclasses

    bot.config = dataclasses.replace(
        bot.config,
        supervisor=dataclasses.replace(
            bot.config.supervisor,
            process_name="selfbot",
            executable=str(stub_ctl),
        ),
    )
    bot.confirm_result = False

    event = FakeEvent(raw_text="self restart")
    await registry.dispatch(bot, event, "self restart")

    assert bot.confirm_prompts, "user should have been asked"
    assert any("Cancelled" in r for r in event.replies)


@pytest.mark.asyncio
async def test_restart_does_not_prompt_when_ctl_is_missing(
    bot, registry, isolated_env, monkeypatch
):
    """Don't ask to restart if we already know we cannot."""
    import dataclasses

    monkeypatch.setattr(sup.sys, "executable", str(isolated_env / "nowhere" / "python"))
    bot.config = dataclasses.replace(
        bot.config,
        supervisor=dataclasses.replace(bot.config.supervisor, process_name="selfbot"),
    )

    event = FakeEvent(raw_text="self restart")
    await registry.dispatch(bot, event, "self restart")

    assert not bot.confirm_prompts
    assert any("self diag" in r for r in event.replies)
