# SOLANA SCANNER ULTIMATE — FULL REBUILD TASK FOR CODEX

## Mission
Build a substantially more capable and maintainable successor to the EXISTING approximately 5,800-line test bot in the untracked local file `bot,py test`. Do not mistake the small prototypes (`test_bot_v2.py`, `night_paper_test.py`, `ultimate_test/`) for the original source. They are incomplete experimental components, not replacements. **Read the full original test file before planning or coding.**

## Non-negotiable permissions and boundaries
- Work ONLY inside a NEW folder `solana_ultimate_next/` in the repository.
- READ `bot,py test` and relevant Python source for analysis; do not modify the original.
- Do NOT open, print, edit, or copy `.env`, credentials, secrets, private keys, `scanner_data.db`, `scanner_state.json`, logs, or unrelated files.
- Never modify `bot.py` or any existing tracked file, never delete old code, never change settings.
- No git push, commit, branch operations, remote deployments, paid services, real transactions, wallet signing or orders.
- Any operations requiring permissions beyond the new folder must stop and request consent.
- Keep all new SQLite databases and runtime files inside `solana_ultimate_next/` (add a .gitignore INSIDE the new folder).
- Preserve virtual-trade history in existing DBs by not touching them; do not silently migrate historical data.
- Do not start the Telegram polling bot or an unattended background process during development.

## Stage A: source inventory, no shortcuts
1. Read every line of `bot,py test` (including classes, globals, imports, handlers, SQL schema, scheduler, risk engine, paper trading, commands, and alerts). Inspect the existing `ultimate_test/` modules as reference only.
2. Write `solana_ultimate_next/LEGACY_INVENTORY.md` listing EVERY discovered Telegram command (exact name), handler, table, subsystem, and configuration name. Note source line numbers.
3. Write `solana_ultimate_next/FEATURE_MATRIX.md` mapping every legacy feature to its new module, status (implemented / tested / pending), and the verification evidence.
4. Never claim parity if the new system is missing a legacy feature. Maintain exact legacy command names, even if aliases are added.

## Stage B: architecture and implementation
Build a fully integrated modular Python package with a clear entrypoint and the following real capabilities:
- Async HTTP clients for permitted public market data; bounded concurrency, retries, exponential backoff, 429 handling, timeouts, validation, and API-health metrics.
- Solana token discovery from DexScreener; distinguish *latest profiles* from confirmed new launches. Store pair creation timestamps, deduplicate token/pair IDs, and persist discoveries. Do not claim coverage of all launches.
- Candidate normalization, data freshness, multi-pair selection, liquidity and volume checks, momentum, market-cap/FDV sanity checks, and deterministic score explanations.
- Risk engine for missing data, mint/freeze authorities, holder concentration, suspicious wallet patterns, fake volume, sellability and transfer restrictions; **do not invent checks**: if no trustworthy provider supplies evidence, label UNKNOWN and block automatic entries.
- A paper-only execution simulator: starting virtual balance EUR20, default stake EUR2, max stake EUR4, configurable position limit, fee and slippage assumptions, atomic SQLite transactions, persistent positions, automatic virtual exit on refreshed quotes, stop loss, take profit, age limit, audit events, no double-close, no negative balance.
- Do not pretend USD and EUR are interchangeable: document or model exchange-rate assumptions. Price ratios may be used for PnL if the fixed-FX assumption is explicit.
- Historical signals and outcomes, rolling precision/recall where measurable, false-positive analysis, calibration, time-split backtesting without lookahead bias, market replay from recorded observations, reproducible strategy comparisons.
- Public-wallet watchlists, with strictly read-only activity and conservative confidence labels when attribution is unknown.
- Telegram authorization on EVERY command, rate limiting, selective notifications, deduplication, /pause and /resume while Telegram remains active, separate full process shutdown and host-managed restart.
- Existing legacy Telegram commands must remain registered and behave consistently; preserve semantics or document a failing gap.
- Persistent SQLite schema migrations, WAL, transactions, indexes, integrity checks, backup, recovery and retention policies.
- Structured logging, health/status reporting, safe exception handling, cancellation and graceful shutdown.
- Documentation: requirements.txt, README.md, COMMANDS.md, ARCHITECTURE.md, TEST_REPORT.md, CHANGELOG.md, and clear run instructions.
- Prefer real, testable code over artificial line count. 10,000–15,000+ lines are welcome if genuinely warranted by functionality, but no boilerplate filler.

## Stage C: integration and testing
- Unit tests for data parsing, risk classification, missing evidence, fees, slippage, stop/take profit, cash accounting, restart persistence, duplicate entries, missing prices, stale quotes, and user authorization.
- Integration tests using local fixtures/mocked HTTP and mocked Telegram; tests must not contact the network or Telegram.
- Test migrations, restart recovery, concurrency, race conditions, and schema integrity.
- Ensure all Python modules compile and import in a clean environment.
- Three verification passes: syntax/imports, unit/integration, regression/security review.
- Do not fabricate test success. Record exact commands, exit codes, counts, failures and limitations in TEST_REPORT.md.
- Test that `bot.py` and `bot,py test` remain unchanged.
- Do not start overnight unattended. If you run out of time/context, preserve work in the new folder, document unfinished features, and stop without claiming completion.

## Output requirements
- Actually write code in `solana_ultimate_next/` and run safe tests.
- In the final response report number of Python source lines, exact implemented vs missing features, test results, and the next safe terminal command.
- Do not replace a 5,800-line test system with a 300-line prototype and call it an upgrade.
