# Inventaris van bot,py test

Alle 5.782 regels gelezen als tekst. `ultimate_test/` is referentiemateriaal. Het risicomodulebestand bevat letterlijke backslash-n-tekens in de eligibility-expressie.

## Telegramregistratie

| Commando | Registratie | Handler |
|---|---:|---|
| /start | 5636 | cmd_start (4373) |
| /help | 5637 | cmd_help (4387) |
| /scan | 5640 | cmd_scan (4445) |
| /top_tokens | 5641 | cmd_top_tokens (4495) |
| /token | 5642 | cmd_token (4535) |
| /wallet | 5645 | cmd_wallet (4583) |
| /wallet_watch | 5646 | cmd_wallet_watch (4633) |
| /wallet_unwatch | 5647 | cmd_wallet_unwatch (4677) |
| /wallet_list | 5648 | cmd_wallet_list (4710) |
| /paper_status | 5651 | cmd_paper_status (4751) |
| /paper_open | 5652 | cmd_paper_open (4782) |
| /paper_close | 5653 | cmd_paper_close (4843) |
| /paper_trades | 5654 | cmd_paper_trades (4946) |
| /paper_report | 5655 | cmd_paper_report (4976) |
| /advanced_paper_report | 5656 | cmd_advanced_paper_report (5006) |
| /market_regime | 5659 | cmd_market_regime (5049) |
| /false_signals | 5660 | cmd_false_signals (5079) |
| /learning_status | 5661 | cmd_learning_status (5109) |
| /learning_report | 5662 | cmd_learning_report (5139) |
| /alert_history | 5665 | cmd_alert_history (5180) |
| /alert_settings | 5666 | cmd_alert_settings (5217) |
| /scheduler_start | 5669 | cmd_scheduler_start (5285) |
| /scheduler_stop | 5670 | cmd_scheduler_stop (5302) |
| /scheduler_status | 5671 | cmd_scheduler_status (5317) |
| /dashboard | 5674 | cmd_dashboard (5340) |
| /system_report | 5675 | cmd_system_report (5362) |
| /database_status | 5678 | cmd_database_status (5399) |
| /database_backup | 5679 | cmd_database_backup (5418) |
| /database_tables | 5680 | cmd_database_tables (5435) |

## Bronindex

Regelnummers verwijzen naar het originele testscript.

- 4 — `# DEEL 1 - BASIS, CONFIGURATIE EN DATABASE`
- 7 — `import os`
- 8 — `import json`
- 9 — `import time`
- 10 — `import math`
- 11 — `import asyncio`
- 12 — `import logging`
- 13 — `import sqlite3`
- 14 — `import shutil`
- 15 — `import statistics`
- 17 — `from pathlib import Path`
- 18 — `from datetime import datetime, timezone, timedelta`
- 19 — `from contextlib import contextmanager`
- 21 — `import aiohttp`
- 22 — `from dotenv import load_dotenv`
- 24 — `from telegram import Update`
- 25 — `from telegram.ext import (`
- 37 — `TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()`
- 39 — `ALLOWED_USERS = set()`
- 41 — `for value in os.getenv("ALLOWED_USER_IDS", "").split(","):`
- 52 — `BOT_NAME = "Solana Scanner Ultimate"`
- 54 — `START_BALANCE = 20.00`
- 55 — `DEFAULT_POSITION_SIZE = 2.00`
- 56 — `MAX_OPEN_TRADES = 3`
- 58 — `SCAN_INTERVAL = 60`
- 59 — `MAX_TOKENS_PER_SCAN = 30`
- 60 — `MAX_CONCURRENT_REQUESTS = 5`
- 62 — `MIN_LIQUIDITY_USD = 3000`
- 63 — `MIN_VOLUME_24H_USD = 1000`
- 64 — `MIN_INTELLIGENCE_SCORE = 60`
- 66 — `FEE_RATE = 0.01`
- 67 — `SLIPPAGE_RATE = 0.005`
- 69 — `DATABASE_FILE = "scanner_data.db"`
- 70 — `BACKUP_FOLDER = "backups"`
- 72 — `DEX_PROFILES_URL = (`
- 76 — `DEX_TOKEN_PAIRS_URL = (`
- 80 — `SOLANA_RPC_URL = (`
- 84 — `HTTP_TIMEOUT = 20`
- 85 — `MAX_RETRIES = 3`
- 111 — `def now_utc():`
- 115 — `def safe_float(value, default=0.0):`
- 132 — `def safe_int(value, default=0):`
- 141 — `def shorten_address(address, start=6, end=6):`
- 158 — `def format_money(value):`
- 162 — `def format_usd(value):`
- 166 — `def parse_timestamp(value):`
- 180 — `def clamp(value, minimum=0, maximum=100):`
- 188 — `class Database:`
- 190 — `def __init__(self, path=DATABASE_FILE):`
- 194 — `def connect(self):`
- 217 — `def initialize(self):`
- 233 — `CREATE TABLE IF NOT EXISTS discovered_tokens (`
- 256 — `CREATE TABLE IF NOT EXISTS paper_trades (`
- 277 — `CREATE TABLE IF NOT EXISTS paper_account (`
- 304 — `CREATE TABLE IF NOT EXISTS wallet_watch (`
- 314 — `CREATE TABLE IF NOT EXISTS wallet_activity (`
- 329 — `CREATE TABLE IF NOT EXISTS scanner_events (`
- 343 — `CREATE TABLE IF NOT EXISTS learning_snapshots (`
- 358 — `CREATE INDEX IF NOT EXISTS`
- 368 — `CREATE TABLE IF NOT EXISTS learning_reports (`
- 380 — `CREATE TABLE IF NOT EXISTS market_regimes (`
- 396 — `CREATE TABLE IF NOT EXISTS false_signal_reports (`
- 413 — `CREATE TABLE IF NOT EXISTS alert_history (`
- 427 — `CREATE TABLE IF NOT EXISTS alert_settings (`
- 455 — `CREATE TABLE IF NOT EXISTS paper_analytics (`
- 472 — `CREATE INDEX IF NOT EXISTS`
- 478 — `CREATE INDEX IF NOT EXISTS`
- 498 — `# DEEL 2 - SOLANA SCANNER & TOKEN DISCOVERY`
- 501 — `class SolanaScanner:`
- 503 — `def __init__(self):`
- 512 — `async def start(self):`
- 534 — `async def close(self):`
- 541 — `async def get_json(self, url, params=None):`
- 592 — `async def get_latest_profiles(self):`
- 627 — `async def get_token_pairs(self, address):`
- 662 — `def select_best_pair(self, pairs):`
- 667 — `def pair_quality(pair):`
- 687 — `def analyse_pair(self, pair):`
- 840 — `def save_token(self, token):`
- 908 — `async def analyse_address(self, address):`
- 929 — `async def scan_once(self):`
- 943 — `async def worker(profile):`
- 1006 — `# DEEL 3 - TOKEN INTELLIGENCE & RISICOANALYSE`
- 1009 — `class TokenIntelligence:`
- 1011 — `def __init__(self):`
- 1014 — `def analyse(self, token):`
- 1147 — `def analyse_many(self, tokens):`
- 1168 — `def get_cached(self, address):`
- 1172 — `def clear_cache(self):`
- 1176 — `def save_snapshot(self, token, intelligence):`
- 1210 — `def get_token_history(self, address, limit=50):`
- 1230 — `def get_latest_analysis(self, address):`
- 1264 — `def analyse_token_completely(token):`
- 1288 — `# DEEL 4 - WALLET INTELLIGENCE`
- 1291 — `class WalletIntelligence:`
- 1293 — `def __init__(self):`
- 1296 — `async def rpc_request(self, method, params):`
- 1356 — `async def get_signatures(self, address, limit=30):`
- 1373 — `async def get_balance(self, address):`
- 1390 — `async def analyse_wallet(self, address):`
- 1507 — `def save_activity(self, address, signatures):`
- 1537 — `def add_to_watchlist(`
- 1570 — `def remove_from_watchlist(self, address):`
- 1581 — `def get_watchlist(self):`
- 1596 — `def get_cached(self, address):`
- 1613 — `# DEEL 5 - LEARNING INTELLIGENCE`
- 1616 — `class LearningIntelligence:`
- 1618 — `def __init__(self):`
- 1623 — `def record_snapshot(self, token):`
- 1635 — `def get_snapshots(self, address, limit=100):`
- 1655 — `def calculate_price_change(self, old_price, new_price):`
- 1668 — `def analyse_token_history(self, address):`
- 1731 — `def calculate_scanner_performance(self):`
- 1774 — `def create_learning_report(self):`
- 1860 — `def get_latest_report(self):`
- 1880 — `def get_report_history(self, limit=10):`
- 1920 — `# DEEL 6 - MARKET REGIME & FALSE SIGNAL DETECTION`
- 1923 — `class MarketRegime:`
- 1925 — `def __init__(self):`
- 1928 — `def collect_market_data(self):`
- 1949 — `def analyse(self):`
- 2078 — `def save(self, result):`
- 2101 — `def get_history(self, limit=20):`
- 2124 — `class FalseSignalDetector:`
- 2126 — `def __init__(self):`
- 2129 — `def inspect_tokens(self, hours=24):`
- 2229 — `def save_report(self, report):`
- 2254 — `def get_reports(self, limit=20):`
- 2286 — `# DEEL 7 - ADVANCED SCANNER FILTERS`
- 2289 — `class AdvancedScannerFilters:`
- 2291 — `def __init__(self):`
- 2306 — `def update_setting(self, key, value):`
- 2318 — `def reset_settings(self):`
- 2322 — `def check_liquidity(self, token):`
- 2335 — `def check_volume(self, token):`
- 2345 — `def check_score(self, token):`
- 2355 — `def check_age(self, token):`
- 2371 — `def check_market_cap(self, token):`
- 2393 — `def check_price(self, token):`
- 2402 — `def check_risk(self, token):`
- 2419 — `def evaluate(self, token):`
- 2445 — `def filter_tokens(self, tokens):`
- 2479 — `def get_settings(self):`
- 2495 — `async def run_advanced_scan():`
- 2537 — `# DEEL 8 - PAPER TRADING ENGINE`
- 2540 — `class PaperTrading:`
- 2542 — `def __init__(self):`
- 2553 — `def initialize_account(self):`
- 2571 — `def get_account(self):`
- 2583 — `def get_open_trades(self):`
- 2599 — `def get_closed_trades(self, limit=100):`
- 2618 — `def open_trade(`
- 2760 — `def close_trade(`
- 2887 — `def get_trade(self, trade_id):`
- 2901 — `def get_statistics(self):`
- 2988 — `# DEEL 9 - ADVANCED PAPER TRADING INTELLIGENCE`
- 2991 — `class PaperTradingIntelligence:`
- 2993 — `def __init__(self):`
- 2997 — `def get_closed_trades(self):`
- 3013 — `def calculate_win_rate(self, trades):`
- 3028 — `def calculate_profit_factor(self, trades):`
- 3050 — `def calculate_streaks(self, trades):`
- 3094 — `def calculate_drawdown(self, trades):`
- 3133 — `def calculate_average_profit(self, trades):`
- 3148 — `def calculate_average_win_loss(self, trades):`
- 3172 — `def generate_report(self):`
- 3230 — `def save_report(self, report):`
- 3255 — `def get_history(self, limit=20):`
- 3273 — `def get_best_trades(self, limit=10):`
- 3292 — `def get_worst_trades(self, limit=10):`
- 3324 — `# DEEL 10 - TELEGRAM ALERTS & ALERT HISTORY`
- 3327 — `class AlertManager:`
- 3329 — `def __init__(self):`
- 3334 — `def is_enabled(self, alert_type):`
- 3346 — `def set_enabled(self, alert_type, enabled):`
- 3376 — `def get_settings(self):`
- 3391 — `def can_send(self, alert_key):`
- 3403 — `async def send_to_users(`
- 3466 — `async def send_new_token_alert(`
- 3498 — `async def send_high_risk_alert(`
- 3537 — `async def send_market_movement_alert(`
- 3567 — `async def send_paper_trade_alert(`
- 3597 — `def get_history(self, limit=20):`
- 3628 — `# DEEL 11 - SCHEDULER & AUTOMATISCHE SCANS`
- 3631 — `class ScannerScheduler:`
- 3633 — `def __init__(self):`
- 3644 — `async def run_scan(self, application):`
- 3742 — `async def _loop(self, application):`
- 3786 — `async def start(self, application):`
- 3801 — `async def stop(self):`
- 3825 — `def get_status(self):`
- 3857 — `# DEEL 12 - DASHBOARD & SYSTEEMSTATISTIEKEN`
- 3860 — `class Dashboard:`
- 3862 — `def get_scanner_stats(self):`
- 3909 — `def get_paper_stats(self):`
- 3913 — `def get_learning_stats(self):`
- 3917 — `def get_wallet_stats(self):`
- 3936 — `def get_full_dashboard(self):`
- 3948 — `def format_dashboard(self):`
- 4032 — `# DEEL 13 - DATABASE MANAGER & BACKUPS`
- 4035 — `class DatabaseManager:`
- 4037 — `def get_status(self):`
- 4099 — `def list_tables(self):`
- 4115 — `def create_backup(self):`
- 4168 — `def repair_database(self):`
- 4177 — `CREATE UNIQUE INDEX IF NOT EXISTS`
- 4183 — `CREATE INDEX IF NOT EXISTS`
- 4189 — `CREATE INDEX IF NOT EXISTS`
- 4195 — `CREATE INDEX IF NOT EXISTS`
- 4225 — `CREATE UNIQUE INDEX IF NOT EXISTS`
- 4272 — `# DEEL 14 - TELEGRAM COMMANDS`
- 4275 — `def authorized(update):`
- 4286 — `async def access_check(update):`
- 4301 — `async def reply(update, message):`
- 4310 — `async def call_first_method(`
- 4342 — `def get_token_from_database(address):`
- 4355 — `def get_open_paper_trades():`
- 4373 — `async def cmd_start(update, context):`
- 4387 — `async def cmd_help(update, context):`
- 4445 — `async def cmd_scan(update, context):`
- 4495 — `async def cmd_top_tokens(update, context):`
- 4535 — `async def cmd_token(update, context):`
- 4583 — `async def cmd_wallet(update, context):`
- 4633 — `async def cmd_wallet_watch(update, context):`
- 4677 — `async def cmd_wallet_unwatch(update, context):`
- 4710 — `async def cmd_wallet_list(update, context):`
- 4751 — `async def cmd_paper_status(update, context):`
- 4782 — `async def cmd_paper_open(update, context):`
- 4843 — `async def cmd_paper_close(update, context):`
- 4946 — `async def cmd_paper_trades(update, context):`
- 4976 — `async def cmd_paper_report(update, context):`
- 5006 — `async def cmd_advanced_paper_report(update, context):`
- 5049 — `async def cmd_market_regime(update, context):`
- 5079 — `async def cmd_false_signals(update, context):`
- 5109 — `async def cmd_learning_status(update, context):`
- 5139 — `async def cmd_learning_report(update, context):`
- 5180 — `async def cmd_alert_history(update, context):`
- 5217 — `async def cmd_alert_settings(update, context):`
- 5285 — `async def cmd_scheduler_start(update, context):`
- 5302 — `async def cmd_scheduler_stop(update, context):`
- 5317 — `async def cmd_scheduler_status(update, context):`
- 5340 — `async def cmd_dashboard(update, context):`
- 5362 — `async def cmd_system_report(update, context):`
- 5399 — `async def cmd_database_status(update, context):`
- 5418 — `async def cmd_database_backup(update, context):`
- 5435 — `async def cmd_database_tables(update, context):`
- 5468 — `# DEEL 15 - BOT INITIALISATIE, COMMAND REGISTRATIE & START`
- 5497 — `async def telegram_error_handler(`
- 5523 — `async def on_startup(application):`
- 5585 — `async def on_shutdown(application):`
- 5631 — `def register_commands(application):`
- 5706 — `def build_application():`
- 5732 — `def main():`

## Aanvullende instellingen en gedragingen

- 220–224 — SQLite foreign_keys en busy_timeout. 226 — WAL.
- 438–443 — new_token, high_risk, market_movement en paper_trade standaard ingeschakeld.
- 518–534 — HTTP-connectorlimieten, DNS-cache en User-Agent.
- 1620 — minimum_samples. 2127 — minimum_price_change.
- 2293–2304 — minimum_liquidity, minimum_volume, minimum_score, maximum_risk, minimum_age_hours, maximum_age_hours, minimum_market_cap, maximum_market_cap, require_price en require_liquidity.
- 3332 — cooldown_seconds. 5755–5756 — drop_pending_updates en allowed_updates.
- 5139–5155 — /learning_report zoekt drie methoden die ontbreken. De werkelijke methode heet create_learning_report.
- 5006–5021 — /advanced_paper_report valt terug op hetzelfde rapport als /paper_report.
- Discovery gebruikt profielpublicaties. Het script kan prijsvelden van base-tokens niet gebruiken als prijzen voor quote-tokens.
- De scheduler start op verzoek. Entry en exit in het originele script zijn handmatig. Alertdefinities voor marktbeweging en paper trades worden in het normale pad niet aangeroepen.
