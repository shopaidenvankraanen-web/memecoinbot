# Commando’s

Alle 31 namen zijn beschikbaar in de opt-in Telegram-host. De offline CLI kan een opdracht uitvoeren tegen één frame. De adapter accepteert alleen nieuwe privéberichten van toegestane gebruikers met een overeenkomende chat-ID en bewaart per chat het hoogste verwerkte berichtnummer. Elke toegestane gebruiker heeft binnen één draaiende host maximaal acht verzoeken per tien seconden.

| Commando | Effect |
|---|---|
| `/start`, `/help` | Introductie en commandolijst |
| `/scan` | Scan publieke marktdata in de host of verwerk het huidige lokale frame |
| `/top_tokens`, `/token <mint>` | Toon ranking of opgeslagen pairdetails met scorecomponenten |
| `/wallet <adres>` | Toon publieke signatures, steekproef-failure-rate en saldo als beschikbaar |
| `/wallet_watch <adres> [label]`, `/wallet_unwatch <adres>`, `/wallet_list` | Beheer en toon publieke watchlist |
| `/paper_status`, `/paper_report` | Actueel saldo en gerealiseerde statistieken in EUR-centen |
| `/advanced_paper_report` | Basisrapport met trade-uitkomsten, exitredenen en actuele geschatte waardering met dekking door recente quotes, plus segmentatie op pairleeftijd en instaprisico |
| `/paper_open <mint>`, `/paper_close <id>`, `/paper_trades` | Virtuele entry, virtuele exit en open posities |
| `/market_regime`, `/false_signals` | Marktsteekproef en score-foutpositieven volgens het nieuwe evaluatielabel |
| `/learning_status`, `/learning_report [split]` | Rollende statistieken, tijdsplit-evaluatie en drempelvoorstellen ter handmatige beoordeling |
| `/alert_history [limiet]`, `/alert_settings [type on/off]` | Lokale meldingsgeschiedenis en voorkeuren. Standaard uit. |
| `/scheduler_start`, `/scheduler_stop`, `/scheduler_status` | Beheer de taak binnen een draaiende host. De lokale eenmalige CLI gebruikt `ingest`. |
| `/dashboard`, `/system_report` | Samenvattingen van scan, virtueel boek en systeem |
| `/database_status`, `/database_backup`, `/database_tables` | SQLite-status, lokale backup en tabelnamen |
| `/pause`, `/resume` | Bewaar of hef de scanpauze op. Telegram blijft beschikbaar. |

Legacy-verschillen staan per commando in [FEATURE_MATRIX.md](FEATURE_MATRIX.md). De CLI verwerkt lokale opdrachten zonder Telegram-token. Invoer voor Telegram in een host wordt begrensd tot privéchats van expliciet toegestane gebruikers.

Stop de volledige host met Ctrl+C of SIGTERM op Unix. Zie [README.md](README.md) voor configuratie.
