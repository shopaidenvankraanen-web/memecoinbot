# Migratierapport Solana Scanner Ultimate

[testbot_complete.py](testbot_complete.py) is de zelfstandige versie van de huidige modulaire scanner in `ultimate/`. Het script vereist Python 3.11 of hoger met de standaardbibliotheek, inclusief `sqlite3`. Voor de optionele Telegram-host is python-telegram-bot vereist. Zie [README.md](README.md). De JSON-bestanden in `examples/` zijn optionele voorbeeldinvoer.

## Gebruik

Voer de opdrachten uit vanuit de map met het script. Relatieve invoer- en databasepaden worden vanuit de werkmap opgelost en moeten binnen de map van het script vallen.

```bash
python -B testbot_complete.py --help
python -B testbot_complete.py status
python -B testbot_complete.py --db runtime/test-voorbeeld.sqlite ingest examples/capture.json
python -B testbot_complete.py --db runtime/test-voorbeeld.sqlite command examples/frame.json /help
```

Het zelfstandige script gebruikt standaard `runtime/testbot-paper.sqlite`, een eigen pad voor testdata. Met `--db` is een ander pad binnen dezelfde map te kiezen. Gebruik een kopie voor een afzonderlijke verificatie met bestaande data. Het CLI-commando `recover` herstelt een gevalideerde backup naar een nieuw pad.

## Functiebehoud

De regressiesuite controleert dezelfde functies tegen de modulaire en zelfstandige uitvoering. [FEATURE_MATRIX.md](FEATURE_MATRIX.md) beschrijft de eerder vastgelegde verschillen met de oude bot.

| Onderdeel | Behouden gedrag |
|---|---|
| CLI | `status`, `backup`, `replay`, `paper-analysis`, `live-market`, `ingest`, `evaluate`, `backtest`, `recover`, `telegram-replay`, `telegram-host` en `command` |
| Dispatcher | Alle 31 commandonamen uit [COMMANDS.md](COMMANDS.md), privéchatcontrole, gebruikerscontrole, snelheidslimiet, scheduler in een host en lokale Telegram-replay |
| Scanner | Normalisatie, scoring, risicobeoordeling, automatische instapvoorwaarden, lokale notificaties, publieke walletobservaties uit fixtures en expliciete publieke marktlookup |
| Papertrading | Atomair virtueel kasboek, auditregels, positie- en saldolimieten, quotegeldigheid, handmatige exits, stop loss, take profit, leeftijdsexit en transactiekosten |
| Statistieken en leren | Rendement, P&L, drawdown, streaks, portefeuillewaardering, segmentatie, tijdsplit-evaluatie, rollende statistieken, foutpositieven en geïsoleerde backtests |
| Database | Dezelfde tabellen, indexen, constraints en migraties voor SQLite-schemaversies 1–3 |

De bovengrens geldt voor de virtuele **inzet**. 400 cent wordt geaccepteerd en 401 cent wordt geweigerd via het beleid, de paper-entry en de SQLite-constraint. Zoals in de modulaire build wordt de virtuele instapfee apart geboekt. Bij 400 cent inzet en de standaardfee van 1% bedraagt de totale virtuele kasafschrijving 404 cent.

## Verificatie

Zie [TEST_REPORT.md](TEST_REPORT.md) voor de drie volledige eindrondes en de open verificatiepunten. De standalone-tests gebruiken een kopie zonder projectmodules en voeren migraties van schemaversies 1–3 uit. De modulaire code leest ook de virtuele positie die de zelfstandige code schrijft.
