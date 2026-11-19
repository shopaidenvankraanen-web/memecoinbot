# Changelog

## Opt-in PAPER-ONLY marktscanner en Telegram-host

- Periodieke scans met gedeelde publieke API-limieten, gevalideerde pairquotes en fail-closed automatische instapcontrole.
- Optionele Telegram-polling voor 31 commando’s, persistente pauze en deduplicatie, en expliciete procesafsluiting.
- Standalone-pariteit en offline verificatie met tijdelijke SQLite-databases.

## Uitbreiding offline onderzoek

- DexScreener-pairlookup op adres voor open posities en publieke Solana-saldoreplay toegevoegd.
- Telegram-adapter controleert privéchat, afzender, bericht-ID en invoerlengte. Lokale replay verwerkt één update.
- Uitgebreid paperrapport bevat gerealiseerde trade-uitkomsten en waardering met dekking van recente quotes.

## Eerste offline build

- Toegevoegd: legacy-inventaris, lokale DexScreener-pipeline, uiteenzetting van scores en conservatieve bewijsclassificatie.
- Toegevoegd: persistent virtueel EUR-paperboek, vernieuwde pairquotes, exits en audit.
- Toegevoegd: lokale replay, tijdsplit-evaluatie, vaste-drempel-portefeuillebacktests en registratie van alle legacy-commandonamen.
- Toegevoegd: versiebeheer van het databaseschema, backup en herstel naar een nieuwe database.
