"""Project update URL parsing, validation and transactional replacement."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FakeEvent
from selfbot.services.updater import (
    GitHubBranch,
    ProjectUpdateError,
    ProjectUpdater,
    ProjectUpdateResult,
    _replace_project_contents,
    _validate_checkout,
    parse_github_branch_url,
)

BRANCH_URL = (
    "https://github.com/ImTheAlireza/SelfBot/"
    "tree/arena%2F01a0337b-selfbot"
)


def _project(directory: Path, marker: str = "new") -> Path:
    (directory / "src" / "selfbot").mkdir(parents=True)
    (directory / "pyproject.toml").write_text("[project]\nname='selfbot'\n")
    (directory / "src" / "selfbot" / "__init__.py").write_text("")
    (directory / "src" / "selfbot" / "feature.py").write_text(
        f"VALUE = {marker!r}\n"
    )
    return directory


def test_parse_github_branch_url_decodes_branch_slash() -> None:
    source = parse_github_branch_url(BRANCH_URL)
    assert source.owner == "ImTheAlireza"
    assert source.repository == "SelfBot"
    assert source.branch == "arena/01a0337b-selfbot"
    assert source.clone_url == "https://github.com/ImTheAlireza/SelfBot.git"


def test_parse_github_branch_url_accepts_markdown_link() -> None:
    source = parse_github_branch_url(f"[{BRANCH_URL}]({BRANCH_URL})")
    assert source.branch == "arena/01a0337b-selfbot"
    bracketed = parse_github_branch_url(f"[{BRANCH_URL}]")
    assert bracketed == source


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/owner/repo/tree/main",
        "http://github.com/owner/repo/tree/main",
        "https://github.com/owner/repo",
        "https://github.com/owner/repo/tree/../main",
        "https://user:secret@github.com/owner/repo/tree/main",
    ],
)
def test_parse_github_branch_url_rejects_unsafe_values(url: str) -> None:
    with pytest.raises(ProjectUpdateError):
        parse_github_branch_url(url)


def test_validate_checkout_checks_shape_and_python(tmp_path: Path) -> None:
    checkout = _project(tmp_path / "checkout")
    files, total_bytes = _validate_checkout(checkout)
    assert files == 3
    assert total_bytes > 0

    (checkout / "src" / "selfbot" / "broken.py").write_text("if !!!")
    with pytest.raises(ProjectUpdateError, match="Python validation failed"):
        _validate_checkout(checkout)


def test_validate_checkout_rejects_symlinks(tmp_path: Path) -> None:
    checkout = _project(tmp_path / "checkout")
    (checkout / "unsafe-link").symlink_to("/etc/passwd")
    with pytest.raises(ProjectUpdateError, match="symlink"):
        _validate_checkout(checkout)


def test_replace_project_overwrites_code_and_preserves_runtime(tmp_path: Path) -> None:
    target = tmp_path / "Selfbot"
    target.mkdir()
    (target / "old.py").write_text("stale")
    (target / ".env").write_text("SECRET=keep")
    (target / "data").mkdir()
    (target / "data" / "selfbot.session").write_text("session")
    (target / ".venv").mkdir()
    (target / ".venv" / "python").write_text("runtime")

    checkout = _project(tmp_path / "checkout")
    (checkout / "README.md").write_text("fresh")
    _replace_project_contents(checkout, target)

    assert not (target / "old.py").exists()
    assert (target / "README.md").read_text() == "fresh"
    assert (target / "src" / "selfbot" / "feature.py").read_text() == "VALUE = 'new'\n"
    assert (target / ".env").read_text() == "SECRET=keep"
    assert (target / "data" / "selfbot.session").read_text() == "session"
    assert (target / ".venv" / "python").read_text() == "runtime"


def test_replace_project_rolls_back_on_install_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "Selfbot"
    target.mkdir()
    (target / "old.py").write_text("original")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    first = checkout / "first.py"
    second = checkout / "second.py"
    first.write_text("first")
    second.write_text("second")

    original_rename = Path.rename

    def failing_rename(path: Path, destination: Path) -> Path:
        if path == second:
            raise OSError("simulated install failure")
        return original_rename(path, destination)

    monkeypatch.setattr(Path, "rename", failing_rename)

    with pytest.raises(ProjectUpdateError, match="rolled back"):
        _replace_project_contents(checkout, target)

    assert (target / "old.py").read_text() == "original"
    assert not (target / "first.py").exists()
    assert not (target / "second.py").exists()


async def test_getcode_command_updates_configured_project(
    bot, registry, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import dataclasses

    bot.config = dataclasses.replace(bot.config, project_update_dir=tmp_path / "deploy")
    calls: list[tuple[Path, str]] = []

    async def fake_update(self: ProjectUpdater, url: str) -> ProjectUpdateResult:
        calls.append((self.target, url))
        return ProjectUpdateResult(
            source=GitHubBranch("ImTheAlireza", "SelfBot", "arena/01a0337b-selfbot"),
            target=self.target,
            commit="abc123def456",
            files=72,
            bytes=12345,
        )

    edits: list[str] = []

    async def fake_edit(_message, text: str, **_kwargs):
        edits.append(text)

    monkeypatch.setattr(ProjectUpdater, "update", fake_update)
    bot.edit = fake_edit
    event = FakeEvent(raw_text=f"getcode {BRANCH_URL}")

    await registry.dispatch(bot, event, event.raw_text)

    assert calls == [(tmp_path / "deploy", BRANCH_URL)]
    assert bot.confirm_prompts and "Replace deployed project code" in bot.confirm_prompts[0]
    assert edits and "Project code updated" in edits[-1]
    assert "self restart" in edits[-1]


async def test_getcode_cancel_does_not_download(bot, registry, monkeypatch) -> None:
    called = False

    async def fake_update(self: ProjectUpdater, url: str) -> ProjectUpdateResult:
        nonlocal called
        called = True
        raise AssertionError("must not run")

    monkeypatch.setattr(ProjectUpdater, "update", fake_update)
    bot.confirm_result = False
    event = FakeEvent(raw_text=f"getcode {BRANCH_URL}")

    await registry.dispatch(bot, event, event.raw_text)

    assert called is False
    assert any("cancelled" in reply.lower() for reply in event.replies)
