"""Opt-in Telegram polling and public market scanning for the virtual ledger."""
import asyncio
import logging
import os
import re

from .clients import PublicData
from .commands import Commands
from .live import MarketTransport
from .notifications import Notifications
from .paper import Paper
from .scanner import Scanner, Scheduler
from .storage import Store
from .wallets import Wallets


class HostError(RuntimeError):
    pass


def host_settings(environment):
    token = environment.get("ULTIMATE_TELEGRAM_TOKEN", "")
    ids = environment.get("ULTIMATE_TELEGRAM_USER_IDS", "")
    if not re.fullmatch(r"[0-9]{1,20}:[A-Za-z0-9_-]{20,100}", token):
        raise HostError("Configure ULTIMATE_TELEGRAM_TOKEN.")
    if not re.fullmatch(r"[0-9]{1,16}(?:,[0-9]{1,16}){0,31}", ids):
        raise HostError("Configure ULTIMATE_TELEGRAM_USER_IDS with comma-separated user IDs.")
    allowed = frozenset(map(int, ids.split(",")))
    if any(not 0 < value < 2**52 for value in allowed):
        raise HostError("Telegram user ID outside supported range.")
    return token, allowed


def telegram_host(db, auto_scan, interval, *, environment=None):
    token, allowed = host_settings(os.environ if environment is None else environment)
    if db is None or not 15 <= interval <= 86400:
        raise HostError("Use an explicit --db and a scan interval of 15–86400 seconds.")
    try:
        from telegram.error import Conflict
        from telegram.ext import Application, CommandHandler
        from telegram.request import HTTPXRequest
    except ImportError:
        raise HostError("Install the optional dependencies from requirements-telegram.txt.") from None

    # Telegram request URLs contain credentials. Suppress library logs, including
    # errors and their tracebacks, throughout the polling lifecycle.
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with asyncio.Runner() as runner:
            runner.get_loop()
            store = Store(db)
            client = PublicData(MarketTransport(), concurrency=1)

            async def send(recipient, message):
                await application.bot.send_message(chat_id=recipient, text=message, parse_mode=None,
                                                   disable_web_page_preview=True)

            alerts = Notifications(store, allowed, send)
            scanner = Scanner(store, Paper(store), client, notifications=alerts, public_quotes=True)
            scheduler = Scheduler(scanner, interval)
            commands = Commands(scanner, scheduler, Wallets(store, client), alerts, allowed)

            async def start(_):
                if auto_scan:
                    scheduler.start()

            async def stop(_):
                await scheduler.stop()

            async def error(_, context):
                if isinstance(context.error, Conflict):
                    application.stop_running()

            def request():
                return HTTPXRequest(connect_timeout=5, read_timeout=5, write_timeout=5,
                                    pool_timeout=5, httpx_kwargs={"trust_env": False})

            application = (Application.builder().token(token).request(request())
                           .get_updates_request(request()).concurrent_updates(False)
                           .post_init(start).post_stop(stop).post_shutdown(stop).build())
            commands.register_telegram(application, CommandHandler, silent_unauthorized=True)
            application.add_error_handler(error)
            try:
                application.run_polling(allowed_updates=["message"], drop_pending_updates=True,
                                        bootstrap_retries=0, timeout=10, close_loop=False)
            finally:
                runner.run(scheduler.stop())
    except Exception as error:
        raise HostError(f"Telegram host stopped ({type(error).__name__}).") from None
    finally:
        logging.disable(previous_logging)
