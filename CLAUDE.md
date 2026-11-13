# Vinted-Notifications (tracker): guida per Claude

Fork di `davegnima/Vinted-Notifications`: cerca annunci Vinted per le ricerche salvate e li manda su Telegram (e RSS). Un processo (`vinted_notifications.py`) avvia scraper, bot Telegram, interfaccia web Flask (`web_ui_plugin/`) e feed RSS. Database SQLite in `db.py`, logica in `core.py`, client Vinted in `pyVintedVN/`.

Le regole dell'utente (lingua, niente chiavi nei file, niente scraping di vinted.it da parte di Claude, ecc.) stanno nel `CLAUDE.md` di `davegnima/vinted-flip-bot`: valgono anche qui.

## Due servizi Railway dallo stesso repo (progetto "loyal-beauty")
- `vinted-notifications`: tracker della moda.
- `vinted-radar`: tracker del radar fuori moda.
Ogni merge su `main` fa il deploy di entrambi: comportamento identico, cambia solo la configurazione (ricerche e variabili).

## Variabili d'ambiente proprie lette dal codice
`BANWORDS_BRAND`, `DEBUG_ORDINE_QUERIES`, `LOG_CATALOG`, `NIGHT_PAUSE_FROM`, `NIGHT_PAUSE_TO`, `RADAR_INVIA_ROSSI`, `RADAR_INVIA_TUTTI`, `SCRAPER_STALL_SECONDS`, `TELEGRAM_SILENT`, `TITLE_MUST_MATCH_QUERY`.

## Ricerche
Si gestiscono dalla pagina `/queries` dell'interfaccia web. Modifica con POST a `/update_query/<id>`, rimozione con POST a `/remove_query/<id>` (`/remove_query/all` le toglie tutte). Il tracker non va visitato su vinted.it da Claude: le pagine le scarica il bot.

## Convenzioni
- Codice e commenti in italiano, stile come quello vicino. Niente trattini lunghi.
- Non toccare la logica dei proxy (`proxies.py`), il processo RSS, `DEBUG_ORDINE_QUERIES`.
- CI (`.github/workflows/linter.yml`): `ruff check .` e `black --check .`. Con il ruff attuale restano errori che richiedono cambi di comportamento (`G201`, `BLE001`, `DTZ`), noti.
- Branch `claude/...`, PR verso `main`, squash merge.
