# Solana Ultimate Next

Marktonderzoek en paper trading met Python 3.11+. De CLI leest lokale opnames van publieke marktgegevens, werkt een geïsoleerd SQLite-paperboek bij en evalueert historische marktobservaties. Een DexScreener-profiel toont dat een profiel beschikbaar was. De aanmaakdatum beschrijft een specifieke pair. Bevestiging van een tokenlaunch vereist aanvullende brongegevens.

## Uitvoeren

Voer uit vanuit `solana_ultimate_next/`.

```sh
python -B -m unittest discover -v
python -B -m ultimate status
python -B -m ultimate --db runtime/demo.sqlite ingest examples/capture.json
python -B -m ultimate --db runtime/demo.sqlite command examples/frame.json top_tokens
python -B -m ultimate --db runtime/demo.sqlite evaluate 1700003600
python -B -m ultimate --db runtime/demo.sqlite backtest 1700003600
python -B -m ultimate --db runtime/demo.sqlite telegram-replay examples/telegram.json --user-id 42
```

De voorbeelden zijn synthetisch. Gebruik voor een herhaalbare evaluatie een nieuwe SQLite-bestandsnaam. De CLI opent lokale bestanden binnen de bouwmap. De offline scan-, wallet- en papercommando’s gebruiken opgenomen providerantwoorden. `telegram-replay` verwerkt één update uit een vertrouwd lokaal bestand en schrijft het antwoord naar stdout. Gebruik de expliciete gebruikers-ID van de fixture. Het bestand is invoer voor een simulatie. Voor dezelfde gebruiker en database worden oudere en dubbele bericht-ID’s overgeslagen. Gebruik een nieuwe testdatabase om hetzelfde voorbeeld opnieuw uit te voeren.

## Telegram-host en periodieke scanner

Installeer voor deze opt-in host de optionele dependency in een eigen Python-omgeving. Geef het proces de token van een aparte Telegram-testbot via `ULTIMATE_TELEGRAM_TOKEN`. Geef de numerieke gebruikers-ID’s door via `ULTIMATE_TELEGRAM_USER_IDS`, kommagescheiden en zonder spaties. Er wordt geen omgevingsbestand geladen. Laat iedere toegestane gebruiker eerst de privéchat met de bot openen. Draai één host per testbot en gebruik een nieuwe, aparte SQLite-bestandsnaam binnen deze projectmap.

```sh
python -m pip install -r requirements-telegram.txt
python -B -m ultimate --db runtime/telegram-paper-test.sqlite telegram-host --auto-scan --interval 120
```

De zelfstandige variant gebruikt dezelfde opties via `python -B testbot_complete.py` in plaats van `python -B -m ultimate`. Laat `--auto-scan` weg om alleen Telegram te starten. Gebruik dan `/scan` voor één ronde of `/scheduler_start` voor periodieke rondes. De interval tussen rondes is standaard 120 seconden na voltooiing van een ronde en kan worden ingesteld op 15–86400 seconden. Het hostproces houdt Telegram actief tijdens `/pause` en `/scheduler_stop`. `/pause` bewaart de scanpauze in SQLite en `/resume` heft deze op. Handmatige virtuele commando’s blijven tijdens de scanpauze beschikbaar. Stop het hele proces met Ctrl+C of SIGTERM op Unix. De host stopt polling en annuleert de schedulertaak. Een al lopende systeemeigen DNS-opvraag kan het procesafsluiten vertragen.

Polling verwerkt alleen nieuwe berichten en verwerpt updates die bij het starten al in de Telegram-wachtrij staan. Alleen toegestane gebruikers in hun eigen privéchat ontvangen antwoorden. Meldingscategorieën staan standaard uit. Activeer bijvoorbeeld `/alert_settings new_token on`. SQLite bewaart per ontvanger en gebeurtenis maximaal één verzendpoging. Een onderbreking of onduidelijke verzenduitkomst kan daarom een melding kosten. Met `/scheduler_status` en `/alert_history` bekijk je de toestand. Polling- en HTTP-clientlogs worden tijdens de host onderdrukt omdat de Telegram-URL de token bevat.

De scanner verwerkt maximaal 30 unieke Solana-profielen per ronde en ververst ook bestaande virtuele posities. Alle publieke verzoeken delen binnen de host één actieve verbinding en minimaal één seconde tussen verzoekstarts. DexScreener gebruikt vaste HTTPS GET-eindpunten. `/wallet` kan uitsluitend de publieke Solana-RPC-methoden `getBalance` en `getSignaturesForAddress` aanroepen. Het transport gebruikt TLS-verificatie, volgt geen redirects, begrenst JSON-antwoorden en past timeouts, retries, 429-cooldown en backoff toe. Conflicterende pairquotes uit één antwoord vallen af. Quote-freshness gebruikt de lokale ontvangsttijd en een limiet van 300 seconden voor paper-beslissingen. De feed geeft geen onafhankelijke timestamp van de laatste handel. De host heeft geen vertrouwde risicobron geconfigureerd. Automatische entries zijn daarom geblokkeerd. Bestaande virtuele posities kunnen met een geldige, recentere quote voor dezelfde pair automatisch sluiten.

Een opname is een JSON-array met strikt oplopende Unix-tijdstippen in seconden. Elke frame heeft `observed`, optioneel `profiles` (DexScreener-objecten), `pairs` (tokenadres naar lijst met pair-objecten) en `wallets` (publiek adres naar lijst met opgenomen RPC-signatures). Voeg waar beschikbaar `pair_quotes` toe als mapping van pair-adres naar het opgenomen DexScreener-antwoord, bijvoorbeeld `{"pairs": [... ]}`. Dit vangt ontbrekende pairs in de tokenfeed op voor open posities. `wallet_balances` mapt een publiek adres naar het Solana `getBalance`-resultaat, bijvoorbeeld `{"context": {"slot": 123}, "value": 1000000000}`. `value` is een geheel aantal lamports. DexScreener geeft de pairaanmaaktijd in milliseconden. Gebruik de daadwerkelijke opnametijd als `observed`. De feeds leveren geen onafhankelijke timestamp van de laatste handel in deze implementatie.

Voor een handmatige virtuele entry moet in SQLite een recente observatie bestaan. `command` accepteert precies één frame. Voor een handmatige exit bevat dat frame een nieuwere quote voor dezelfde pair. Een automatische exit wordt bij een volgende `ingest`-frame beoordeeld, ook buiten de profielfeed.

## Actuele marktdata

```sh
python -B -m ultimate live-market
python -B -m ultimate live-market <mint>
python -B -m ultimate live-market <mint> --pair <pairadres>
```

De losse opvraag toont respectievelijk Solana-profieladressen, markten voor één basistoken of één specifieke pair. Het commando gebruikt geverifieerde HTTPS naar vaste DexScreener-eindpunten, beperkt de antwoordgrootte en volgt geen redirects. Het opent geen SQLite-database. Per transportinstantie start maximaal één verzoek per seconde, inclusief retries. De sockettimeout is 5 seconden. De client wacht maximaal drie pogingen af bij timeouts, I/O-fouten, HTTP 429 of 5xx. Een `Retry-After` langer dan 60 seconden beëindigt de opvraag. De OS-resolver kan langer wachten dan de sockettimeout. `observed` is het lokale ontvangsttijdstip in seconden en geeft geen tijd van de laatste transactie aan.

## Paper-account

Startsaldo €20, standaardinzet €2, harde bovengrens €4 en standaard maximaal drie posities. De entryfee van 1% komt bovenop de inzet. Beide zijden gebruiken 0,5% slippage en de exitfee wordt over de gesimuleerde opbrengst berekend. Stop loss (15%), take profit (25%) en maximale leeftijd (6 uur) gebruiken vernieuwde quotes. Een leeftijdsexit blijft wachten bij ontbrekende quotes.

De inzet en balans zijn EUR-centen. De marktdataprijs blijft USD. Opbrengst in EUR is inzet maal de verhouding tussen exit- en entryprijs na slippage, verminderd met de exitfee. Dit veronderstelt een constante USD/EUR-koers gedurende één trade. Resultaten bevatten geen valutawinst of valutaverlies. Handmatige entries zijn lokale simulaties waarbij de operator de onzekerheid accepteert. Automatische entries vereisen volledig, recent en aan de juiste pair gekoppeld bewijs van een expliciet vertrouwde provider. De CLI configureert geen dergelijke provider.

`/advanced_paper_report` toont ook beste en slechtste gesloten trade, aantallen winst/verlies/break-even, gemiddelde looptijd, exitredenen en gerealiseerd rendement op de totale kostprijs van gesloten trades. De waardering gebruikt per open positie de laatste opgeslagen, recente quote voor dezelfde pair, met dezelfde paperkosten als bij een exit. Bij ontbrekende quotes toont het rapport de waardering van het gedekte deel en blijft de totale geschatte portefeuillewaarde onbekend. Het tijdstip van het frame begrenst de gebruikte quotes. Het rapport gebruikt de huidige boekstand. Dezelfde segmentatie is los beschikbaar via `python -B -m ultimate --db runtime/demo.sqlite paper-analysis`. Groepen gebruiken de pairleeftijd bij instap (<1 uur, 1–24 uur, 1–7 dagen, ≥7 dagen of onbekend) en de risicocategorie bij instap. Posities zonder opgeslagen instapcontext tellen mee bij onbekend. Elke groep toont open en gesloten aantallen, winst/verlies/break-even, netto-P&L en rendement op de gesloten kostprijs.

`/learning_report [split]` en `evaluate <split>` tellen foutpositieven en gemiste positieve signalen bij vaste drempels van 60, 80 en 100. De beste kandidaat uit de trainingsperiode levert pas een voorstel op als de fouten ook in de daaropvolgende testperiode dalen zonder extra fouten van het andere type. Beide perioden vereisen minimaal vijf positieve en vijf negatieve labels. De uitkomst blijft een voorstel voor handmatige beoordeling. De scanconfiguratie en risicoregels worden door het rapport niet gewijzigd.

## Opslag

`python -B -m ultimate backup` maakt een consistente SQLite-kopie. `python -B -m ultimate recover runtime/backup-NAAM.sqlite runtime/recovered.sqlite` controleert de backup en herstelt naar een nieuwe database. Gebruik daarna `--db runtime/recovered.sqlite status` om haar te controleren. Audit, paperhistorie, observaties en backups blijven bewaard totdat een operator ze archiveert. Er draait geen automatische retentietaak. Bewaar backups volgens de eigen opslaglimiet en neem voor archivering een gecontroleerde kopie.

Zie [COMMANDS.md](COMMANDS.md), [FEATURE_MATRIX.md](FEATURE_MATRIX.md) en [TEST_REPORT.md](TEST_REPORT.md).
