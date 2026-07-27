"""Command-line entry point: ``python -m selfbot``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from . import __version__
from .config import load_config
from .errors import ConfigError, SelfBotError
from .logging_setup import setup_logging

logger = logging.getLogger("selfbot")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="selfbot",
        description="An asynchronous Telegram self-bot built on Telethon.",
        epilog="Self-bots violate Telegram's ToS. Use at your own risk.",
    )
    parser.add_argument("--version", action="version", version=f"selfbot {__version__}")
    parser.add_argument(
        "--env-file", default=".env", help="path to the .env file (default: .env)"
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="override LOG_LEVEL",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration and exit without connecting",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="sign in, print your user ID, and write SUDO_USER_ID into the .env file",
    )
    return parser


async def _login(args: argparse.Namespace) -> int:
    """Authenticate, report the account's ID and record it as SUDO_USER_ID."""
    from telethon import TelegramClient

    try:
        config = load_config(env_file=args.env_file, allow_missing_sudo=True)
    except ConfigError as exc:
        print(f"❌ Configuration error:\n{exc}", file=sys.stderr)
        return 2

    config.ensure_dirs()
    setup_logging(level="WARNING")

    client = TelegramClient(
        str(config.session_path),
        config.telegram.api_id,
        config.telegram.api_hash,
    )

    print("Signing in to Telegram — you'll be asked for a code.\n")
    try:
        async with client:
            await client.start(phone=config.telegram.phone or None)
            me = await client.get_me()
    except (EOFError, KeyboardInterrupt):
        print("\n❌ Login cancelled.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"\n❌ Login failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(
            "\nCheck that TELEGRAM_API_ID and TELEGRAM_API_HASH are correct "
            "(from https://my.telegram.org) and that you have network access.",
            file=sys.stderr,
        )
        return 1

    name = " ".join(
        filter(None, [getattr(me, "first_name", ""), getattr(me, "last_name", "")])
    )
    username = getattr(me, "username", None)

    print("\n✅ Signed in successfully.\n")
    print(f"   Account  : {name or '(no name)'}")
    if username:
        print(f"   Username : @{username}")
    print(f"   User ID  : {me.id}")
    print(f"   Session  : {config.session_path}.session\n")

    written = _persist_sudo_id(Path(args.env_file), me.id)
    if written:
        print(f"✅ SUDO_USER_ID={me.id} written to {args.env_file}")
        print("\nYou're ready: run `python -m selfbot`")
    else:
        print(f"➡️  Add this line to {args.env_file}:\n\n    SUDO_USER_ID={me.id}\n")

    print(
        "\n⚠️  Keep the .session file private — it grants full access to this account."
    )
    return 0


def _persist_sudo_id(env_path: Path, user_id: int) -> bool:
    """Write SUDO_USER_ID into the .env file, updating any existing entry."""
    if not env_path.is_file():
        return False

    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
        updated = False
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("SUDO_USER_ID"):
                # Leave an explicit, non-empty value alone.
                _, _, current = stripped.partition("=")
                if current.strip() and current.strip() != str(user_id):
                    return False
                lines[index] = f"SUDO_USER_ID={user_id}"
                updated = True
                break

        if not updated:
            lines.append(f"SUDO_USER_ID={user_id}")

        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True
    except OSError:
        return False


async def _run(args: argparse.Namespace) -> int:
    from .bot import SelfBot
    from .plugins import load_all

    if args.login:
        return await _login(args)

    try:
        config = load_config(env_file=args.env_file)
    except ConfigError as exc:
        print(f"❌ Configuration error:\n{exc}", file=sys.stderr)
        return 2

    config.ensure_dirs()
    setup_logging(
        level=args.log_level or config.log_level,
        log_file=config.data_dir / "selfbot.log",
    )

    load_all()

    if args.check:
        from .registry import registry

        print(f"✅ Configuration valid — {len(registry)} commands registered.\n")
        print(config.describe())
        return 0

    bot = SelfBot(config)

    loop = asyncio.get_running_loop()
    stop = loop.create_future()

    def request_stop(*_: object) -> None:
        if not stop.done():
            stop.set_result(None)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except (NotImplementedError, RuntimeError):  # Windows
            signal.signal(sig, lambda *_: request_stop())

    try:
        await bot.start()
        logger.info("Running. Press Ctrl-C to stop.")
        runner = asyncio.ensure_future(bot.client.run_until_disconnected())
        await asyncio.wait({runner, stop}, return_when=asyncio.FIRST_COMPLETED)
        runner.cancel()
    except SelfBotError as exc:
        logger.error("%s", exc)
        return 1
    except Exception:
        logger.exception("Fatal error")
        return 1
    finally:
        await bot.shutdown()

    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
