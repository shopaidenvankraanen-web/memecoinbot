"""Paper research CLI with an explicit read-only DexScreener lookup."""
import argparse
import asyncio
import json
import logging

from . import analytics, backtest
from .clients import ProviderError, PublicData, RecordedTransport
from .host import HostError, telegram_host
from .live import market_snapshot
from .commands import Commands
from .notifications import Notifications
from .paper import Paper
from .scanner import Scanner, Scheduler
from .storage import Store, owned
from .telegram import Telegram
from .wallets import Wallets


async def run(args):
    if args.action == "live-market":
        return await market_snapshot(args.mint, args.pair)
    if args.action == "recover":
        return Store.recover(args.backup, args.destination).status()
    store = Store(args.db)
    if args.action == "status":
        return {"database": store.status(), "paper": Paper(store).report()}
    if args.action == "paper-analysis":
        return Paper(store).segments()
    if args.action == "backup":
        return str(store.backup())
    if args.action == "evaluate":
        return analytics.review(analytics.labeled(analytics.replay(store)), args.split)
    if args.action == "backtest":
        return backtest.compare_portfolios(store, args.split)
    if args.action == "replay":
        return analytics.replay(store)
    frames = json.loads(owned(args.capture).read_text(encoding="utf-8"))
    if not isinstance(frames, list) or not frames:
        raise ValueError("Capture must contain a nonempty array of frames")
    if args.action in ("command", "telegram-replay") and len(frames) != 1:
        raise ValueError("Command requires exactly one frame")
    current = None
    client = PublicData(None)
    paper = Paper(store)

    async def local_notice(recipient, message):
        logging.getLogger(__name__).info("event=local_notification")

    user_id = args.user_id if args.action == "telegram-replay" else 1
    if not 0 < user_id < 2**52:
        raise ValueError("Invalid Telegram user ID")
    alerts = Notifications(store, {user_id}, local_notice)
    scanner = Scanner(store, paper, client, clock=lambda: current, notifications=alerts)
    scheduler = Scheduler(scanner)
    commands = Commands(scanner, scheduler, Wallets(store, client), alerts, {user_id})
    results = []
    try:
        for frame in frames:
            timestamp = frame["observed"]
            if type(timestamp) is not int or not 0 < timestamp < 2**53:
                raise ValueError("Invalid capture timestamp")
            if current is not None and timestamp <= current:
                raise ValueError("Capture timestamps must strictly increase")
            current = timestamp
            client.transport = RecordedTransport(frame)
            if args.action == "telegram-replay":
                return await Telegram(commands).replay(frame["telegram_update"])
            if args.action == "command":
                if args.name.removeprefix("/") == "scheduler_start":
                    raise ValueError("Use a finite capture with ingest")
                return await commands.dispatch(1, True, args.name.removeprefix("/"), args.arguments)
            results.append(await scanner.scan())
        return results
    finally:
        await scheduler.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="SQLite path inside this build folder")
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("status", "backup", "replay", "paper-analysis"):
        actions.add_parser(action)
    live = actions.add_parser("live-market")
    live.add_argument("mint", nargs="?")
    live.add_argument("--pair")
    actions.add_parser("ingest").add_argument("capture")
    for action in ("evaluate", "backtest"):
        actions.add_parser(action).add_argument("split", type=int)
    recovery = actions.add_parser("recover")
    recovery.add_argument("backup")
    recovery.add_argument("destination")
    telegram = actions.add_parser("telegram-replay")
    telegram.add_argument("capture")
    telegram.add_argument("--user-id", required=True, type=int)
    host = actions.add_parser("telegram-host", help="Start the PAPER-ONLY Telegram host")
    host.add_argument("--auto-scan", action="store_true", help="Start periodic public market scans")
    host.add_argument("--interval", type=int, default=120, help="Seconds between scan rounds")
    command = actions.add_parser("command")
    command.add_argument("capture")
    command.add_argument("name")
    command.add_argument("arguments", nargs="*")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s level=%(levelname)s %(message)s")
    args = parser.parse_args()
    try:
        if args.action == "telegram-host":
            telegram_host(args.db, args.auto_scan, args.interval)
            return
        result = asyncio.run(run(args))
    except HostError as error:
        parser.exit(2, f"{error}\n")
    except ProviderError as error:
        parser.exit(2, f"Market provider failed ({error}).\n")
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(2, f"Operation failed ({type(error).__name__}).\n")
    print(result if isinstance(result, str) else json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
