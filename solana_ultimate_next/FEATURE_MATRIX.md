# Functiematrix

Legacy-regelnummers en alle exacte registraties staan in [LEGACY_INVENTORY.md](LEGACY_INVENTORY.md). De status “Getest” verwijst naar lokale fixtures of mocks. De naamregistratie van alle 29 legacy-commando’s en respectieve autorisatie zijn getest. Uitvoeropmaak en de specifieke legacy-heuristieken verschillen.

| Legacy-subsysteem | Opvolger | Status en bewijs |
|---|---|---|
| Imports, omgeving, globale boot en logging (1–495) | `__main__.py`, `storage.py` | Getest in schone omgeving. Config via Python-beleid en CLI. Geen .env-bootstrap. |
| HTTP, profielscan en pairselectie (498–1001) | `clients.py`, `market.py`, `scanner.py` | Getest met 429, timeout, foute payload, begrensde gelijktijdigheid, multi-pair en dubbele profielen. Opgenomen DexScreener-lookup op pairadres getest als terugval voor open posities. Expliciete read-only CLI-opvraag met drie toegestane HTTPS-routes, timeouts, pacing voor elke poging, begrensde retries en strikte antwoordvalidatie getest met mocks. Launchbevestiging blijft open. |
| Intelligencecache, snapshots en risico (1006–1283) | `market.py`, `risk.py`, `storage.py` | Getest met ontbrekend, verouderd en ongunstig bewijs. Marktcomponenten verschillen. Geen aparte in-memory intelligencecache. |
| Wallet-RPC en watchlist (1288–1608) | `wallets.py`, `clients.py` | Getest met opgenomen signatures en deduplicatie per adres. Opgenomen SOL-balans en steekproef-failure-rate beschikbaar. Attributie en legacy-risicoclassificatie blijven open. |
| Learninghistorie, scoreverdeling, rapportopslag (1613–1915) | `analytics.py` | Tijdsplit, labels, foutpositieven, foutnegatieven en handmatige drempelvoorstellen na bevestiging in een aparte periode getest. Persistente rapporten, individuele legacy-trendrapporten en exacte scoreverdeling ontbreken. |
| Regime en false-signal-rapporten (1920–2281) | `commands.py`, `analytics.py` | Foutpositieven getest. Regime wordt berekend bij opvraag. Aparte regime- en foutsignaaltabellen ontbreken. |
| Filters en geavanceerde scan (2286–2532) | `market.py`, `risk.py`, `scanner.py` | Marktfilters en brongebonden toelating getest. De legacy dynamische leeftijd-, cap- en risicoinstellingen ontbreken. |
| Paper-account, openen/sluiten, statistieken (2537–2983) | `paper.py` | Getest op fees, slippage, saldo, limieten, restart, gelijktijdigheid en vernieuwde prijs. Stakes hebben een harde €4-grens. |
| Geavanceerde paper-intelligence (2988–3319) | `paper.py` | Beste en slechtste gesloten trade, exitredenen, rendement, looptijd en waardering met dekking door recente quotes getest. Instapcontext blijft per positie bewaard. Segmenten op pairleeftijd en risico tonen aantallen, uitkomsten, netto-P&L en rendement. Oudere posities zonder instapcontext worden als onbekend gerapporteerd. Aparte analytische snapshots blijven open. |
| Meldingsvoorkeuren, geschiedenis en meldingen (3324–3623) | `notifications.py`, `scanner.py` | Deduplicatie bij gelijktijdige melding getest. Standaard uit, versturen via geïnjecteerde functie. De opt-in Telegram-host verzorgt levering via python-telegram-bot. |
| Scheduler (3628–3852) | `scanner.py` | Start, stop, annulering en persistente pauze getest. Volgende-scantijd en persistente schedulerhistorie ontbreken. |
| Dashboard en systeemstatistieken (3857–4027) | `commands.py` | Samengestelde status geïmplementeerd. Exacte legacy-formattering, gemiddelde score en ‘vandaag ontdekt’ ontbreken. |
| SQLite-status, backup en repair (4032–4267) | `storage.py` | Versiemigratie, integriteit, rollback en herstel getest. Zelfde-map-backups en handmatige retentie. Legacy-reparatie en automatische retentie ontbreken. |
| Handlers en lifecycle (4272–5782) | `commands.py`, `__main__.py`, `scanner.py` | Alle handlernamen en autorisatie getest met mocks. Lokale single-update-replay, invoergrenzen, private-chat-ID en persistente deduplicatie toegevoegd. Zelfstandige host en supervisor blijven open. |

## Commando’s

| Legacy | Module | Status en verschil |
|---|---|---|
| /start | commands | Geïmplementeerd. PAPER-ONLY-introductie. |
| /help | commands | Geïmplementeerd. Dynamische lijst. |
| /scan | scanner | Getest. Lokaal frame, risk gate en exits. |
| /top_tokens | commands | Geïmplementeerd. Nieuwste pair per mint op score gerangschikt. |
| /token | commands | Geïmplementeerd. Opgeslagen pairdetails. |
| /wallet | wallets | Getest. Opgenomen signatures, failure-rate en exact saldo in lamports en SOL met slot. Attributie blijft UNKNOWN. |
| /wallet_watch | wallets | Getest. Publiek adres en label. |
| /wallet_unwatch | wallets | Getest. Verwijdert watchlistlidmaatschap. |
| /wallet_list | wallets | Getest. Onbekende attributie. |
| /paper_status | paper | Getest. Virtuele EUR-centen en gerealiseerde statistieken. |
| /paper_open | commands, paper | Getest. Handmatig met recente opgeslagen quote. |
| /paper_close | commands, paper | Geïmplementeerd. Gebruikt een nieuwere quote van dezelfde pair, met opgenomen pairlookup als terugval. |
| /paper_trades | paper | Getest. Open posities na herstart. |
| /paper_report | paper | Getest. Berekening op aanvraag. |
| /advanced_paper_report | paper | Getest. Extra uitkomststatistieken en quote-afhankelijke virtuele waardering met expliciete dekking, plus segmenten op pairleeftijd en instaprisico. |
| /market_regime | commands | Geïmplementeerd. Gewijzigde steekproef en heuristiek. |
| /false_signals | analytics | Getest. Voorspellingsdrempel en uitkomstlabel vervangen legacy-dalingsregels. |
| /learning_status | analytics | Getest. Rollende precisie/recall vervangen legacy-tellingen. |
| /learning_report | analytics | Getest. Tijdsplitrapport met foutanalyse en drempelvoorstel ter handmatige beoordeling. |
| /alert_history | commands | Getest. Persistente pogingen per ontvanger. |
| /alert_settings | notifications | Getest. Zelfde categorieën, standaard uit. |
| /scheduler_start | scanner | Getest. Alleen beschikbaar in een blijvende host. |
| /scheduler_stop | scanner | Getest. Annuleert de taak binnen de host. |
| /scheduler_status | scanner | Geïmplementeerd. Status en API-metrics. |
| /dashboard | commands | Geïmplementeerd. Nieuwe samenvatting. |
| /system_report | commands | Geïmplementeerd. Integriteit, tabellen en scannerstatus. |
| /database_status | storage | Getest. Integriteit, versie en grootte. |
| /database_backup | storage | Getest. Lokale SQLite-snapshot. |
| /database_tables | commands | Geïmplementeerd. Tabellen van het opvolgersschema. |

## Tabelmapping

| Legacy SQLite-tabel | Opslag in de opvolger |
|---|---|
| discovered_tokens | profiles, pairs, observations |
| paper_trades | positions, audit |
| paper_account | account |
| wallet_watch | watch |
| wallet_activity | activity |
| scanner_events | Gestructureerde uitvoer op stderr, met discovery-data in profiles en trade-audit in audit |
| learning_snapshots | observations |
| learning_reports | Herberekend vanuit observations, geen aparte tabel |
| market_regimes | Herberekend vanuit observations, geen aparte tabel |
| false_signal_reports | Herberekend vanuit observations, geen aparte tabel |
| alert_history | notifications |
| alert_settings | settings |
| paper_analytics | Herberekend vanuit positions, opgeslagen instapcontext en recente observations |

## Nieuwe CLI-commando’s

| Commando | Status |
|---|---|
| `live-market [mint] [--pair pairadres]` | Getest met mocks voor de expliciete, eenmalige alleen-lezen opvraag. |
| `paper-analysis` | Getest op opgeslagen instapcontext, onbekende context en netto-uitkomsten per groep. |

## Openstaande bouwvereisten

- Onafhankelijke veiligheidsbronnen voor alle zeven bewijsvelden. De standaardconfiguratie kan daarom geen automatische entry vrijgeven.
- Praktijkverificatie tegen DexScreener en providerbrede rate limiting over meerdere CLI-processen ontbreken.
- Bevestigde nieuwe launches. Pairaanmaak is wel persistent en profielwaarnemingen dragen een bronlabel.
- Echte Telegram-levering en polling met de geïnstalleerde dependency zijn nog niet geverifieerd. Pauze en berichtdeduplicatie over een gesimuleerde herstart zijn offline getest.
- Portefeuilleonderzoek is beperkt tot vaste markt-scoredrempels en gerealiseerde resultaten. Bewijsgestuurde historische evaluatie en mark-to-market op het einde ontbreken.
- Geautomatiseerd retentiebeheer blijft open. De paper-CLI bewaart de eigen geschiedenis. De actuele CLI-opvraag geeft resultaten alleen via stdout.
