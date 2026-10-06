# FantaManager — regole per chi sviluppa

## Web e app mobile devono essere uguali

La console web (`/dashboard/…`: `auctions/templates/auctions/market/*`,
`admin_*.html`, `views/admin_*.py`) e l'app mobile (`/app/…`:
`auctions/templates/auctions/app_*.html`, `views/app.py`,
`views/app_admin.py`) offrono le **stesse funzioni, con le stesse opzioni e gli
stessi testi**.

- Chi aggiunge o cambia una funzione in una delle due, la porta anche
  nell'altra **nello stesso commit**.
- Dove si può, un solo partial condiviso incluso da entrambe invece di due
  copie. Esempio: il wizard «Nuovo Mercato» è solo in
  `auctions/templates/auctions/_market_wizard.html`, incluso da
  `app_mercato.html`, `app_regia.html`, `market/hub.html` e `market/buste.html`.
  Un nuovo tipo di mercato o una nuova regola si aggiunge lì.
- Un test verifica che le pagine web e app mostrino le stesse opzioni
  (vedi `MarketWizardParityTests` in `auctions/tests/test_market.py`).

## Ogni lega è separata

Mercati, sessioni, scambi e regole valgono solo per la lega in cui sono creati.
L'admin di una lega (proprietario o co-admin in `league.admins`) gestisce solo
le sue leghe: usare `user_can_manage_league` / `manageable_leagues`
(`views/common.py`), mai il solo `league.owner`.

## Test

```
python manage.py collectstatic --noinput   # serve una volta: i template usano il manifest
python manage.py test auctions
```
