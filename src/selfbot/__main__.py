"""Command-line entry point: ``python -m selfbot``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

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
    return parser


async def _run(args: argparse.Namespace) -> int:
    from .bot import SelfBot
    from .plugins import load_all

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
