# Testrapport PAPER-ONLY

Elke eindronde draaide met Python 3.14.2, nieuwe bronkopieën, aparte subprocessen en nieuwe tijdelijke SQLite-databases binnen de projectmap. De netwerkaudit blokkeerde uitgaande socketverbindingen en DNS. De Telegram-tests gebruikten een synthetische token en gesimuleerde telegrammodules.

| Ronde | Modulaire suite inclusief distributietests | Regres­sies tegen zelfstandige variant | Statische veiligheidsreview | Exitcode |
|---|---:|---:|---|---:|
| 1 | 49/49 | 45/45 | Geslaagd | 0 |
| 2 | 49/49 | 45/45 | Geslaagd | 0 |
| 3 | 49/49 | 45/45 | Geslaagd | 0 |

Herhaal een ronde met `python -B -m tests.run_standalone_round`.

## Gecontroleerd

- Automatische rondes, dubbel starten, annuleren, persistente pauze en hervatten. De host registreert alle 31 commando’s en wordt via gesimuleerde polling- en shutdownhooks getest.
- Autorisatie van privéchats, genegeerde groeps- en botupdates, stille afwijzing van onbevoegde gebruikers, persistente berichtdeduplicatie en tokenvrije foutmelding bij een gesimuleerde frameworkfout.
- Vaste API-eindpunten en toegestane publieke RPC-methoden, begrensde HTTPS/JSON-verwerking, gedeelde pacing inclusief retries, HTTP 429/503, timeouts en maximaal één lopende provideraanvraag.
- Deduplicatie van profielen, pairquotes en alerts, afwijzing van conflicterende en verlopen quotes, en pair-lookup voor bestaande posities wanneer ontdekking uitvalt. Onbekend risicobewijs blokkeert automatische entries.
- Startsaldo 2.000 cent, standaardinzet 200 cent, grens 400 cent en virtuele transacties. Herstart, migraties, databasebackup en herstel naar een nieuw pad, inclusief behoud van pauze en meldingsclaims. Bij een onduidelijke verzenduitkomst wordt dezelfde alert na herstart niet opnieuw geprobeerd.

## Open verificatie

De echte python-telegram-bot-installatie, Telegram-polling en bezorging, OS-signaalafhandeling met het echte framework en uitgaande DexScreener/Solana-aanroepen zijn niet uitgevoerd. Andere Python-versies, productiebelasting en limieten over meerdere processen zijn niet afzonderlijk getest. Freshness gebruikt de ontvangsttijd. De marktdatabron levert geen onafhankelijke laatste-transactietijd. De host configureert geen vertrouwde bron voor automatische entries.
