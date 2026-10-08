# FantaManager — regole per chi sviluppa

## Ogni pagina guida passo passo

Ogni pagina dice a chi la usa cosa fare adesso, cosa viene dopo e a che punto
è. Le procedure (creare, collegare, chiudere…) sono passi numerati: fatti con
✓, quello di adesso evidenziato col suo tasto, i successivi spiegati. Ogni
pagina ha il suo «Cosa fare in questa pagina» (`details.howto`); un messaggio
d'errore dice anche cosa fare per rimediare. Esempio: `_sala_card.html`.

## Web e app mobile devono essere uguali

La console web (`/dashboard/…`: `auctions/templates/auctions/market/*`,
`admin_*.html`, `views/admin_*.py`) e l'app mobile (`/app/…`:
`auctions/templates/auctions/app_*.html`, `views/app.py`,
`views/app_admin.py`) offrono le **stesse funzioni, con le stesse opzioni e gli
stessi testi**.

- Chi aggiunge o cambia una funzione in una delle due, la porta anche
  nell'altra **nello stesso commit**.
- Dove si può, un solo partial condiviso incluso da entrambe invece di due
  copie. I wizard di creazione sono già così:
  - «Nuovo Mercato»: `auctions/templates/auctions/_market_wizard.html`, incluso
    da `app_mercato.html`, `app_regia.html`, `market/hub.html`, `market/buste.html`;
  - «Nuova Competizione»: `auctions/templates/auctions/_competition_wizard.html`,
    incluso da `app_lega.html` e `admin_competitions.html`.
  Un nuovo tipo, formato o regola si aggiunge nel partial, non nella pagina.
- Anche le schermate di gestione sono partial condivisi:
  - una sessione di mercato: `market/_session_manage.html`, in
    `market/session.html` (console) e `app_regia_session.html` (app);
  - gli scambi (ratifiche, regole, periodi): `market/_trades_manage.html`, in
    `market/scambi.html` (console) e `app_regia_trades.html` (app).
  I dati arrivano da `session_manage_context` / `trades_manage_context`
  (`views/admin_market.py`). Ogni form manda `next` = la pagina da cui parte,
  così l'azione torna lì (console o app); `_back()` accetta solo indirizzi del sito.
- Giornate, voti e calcolo: `_giornate_manage.html`, in `admin_giornate.html`
  (console) e `app_giornate.html` (app); una sola view, `admin_giornate`
  (`views/admin_voti.py`), che sceglie la cornice dall'indirizzo.
- Il campo della formazione: `_formation_pitch.html`, in `app_formazione.html`
  (il manager, prossima giornata) e nell'editor dell'admin per una giornata
  qualsiasi (`admin_formazione.html` console, `app_regia_formazione.html` app,
  testata `_formation_admin_head.html`; view `admin_formation_edit`).
- L'anagrafica dei calciatori (API-Football, unica per tutte le leghe):
  `_footballers.html`, in `admin_footballers.html` (console) e
  `app_footballers.html` (app); dati da `footballers_context`
  (`views/footballers.py`).
- Lo storico della lega (albo d'oro e classifica di sempre), che vedono anche i
  manager: `_season_history.html`, in `admin_storico.html` (console) e
  `app_storico.html` (app, dalla pagina Lega); dati da `league_history`
  (`services/history.py`).
- Le squadre della lega (email e inviti, account del portale, link e QR,
  scheda): `_teams_manage.html`, in `admin_participants.html` (console) e
  `app_regia_teams.html` (app); dati da `teams_manage_context`
  (`views/admin_participants.py`). Colonne e schede seguono la larghezza del
  riquadro (container query), non della finestra: la colonna dell'app è più
  stretta della pagina della console.
- Le altre pagine della console girano anche nell'app con la **stessa view e lo
  stesso template**: cambia solo la cornice. Il template estende
  `page_frame|default:"auctions/_frame_console.html"` e riempie il blocco
  `page`; la view aggiunge `**page_frame(request, league)` al contesto
  (`views/common.py`), che dall'indirizzo `/app/regia/…` sceglie
  `_frame_app.html` (guscio dell'app) invece di `_frame_console.html` (testata
  della console). Sono così Giocatori, Contratti, Stagione, Importa, Export,
  Impostazioni, Competizioni, Mercato (hub, buste, asta, movimenti) e Nuova
  asta; la Dashboard della console nell'app è la Regia.
  - Una pagina nuova: stessa view su due indirizzi (`dashboard/…` e
    `app/regia/…`), la coppia in `APP_PAGES` e in `FRAMED_PAGES`
    (`auctions/tests/test_app_pages.py`).
  - I link fra pagine usano `{% purl 'nome' %}` (`{% load fm_pages %}`): nella
    console è `{% url %}`, nell'app porta alla pagina dell'app se c'è.
  - Ogni form manda `next`; la view ci torna con `safe_next` o, se aggiunge
    la sua query, con `back_to_page`.
  - Se la pagina mostra già i messaggi: `page_frame(..., own_messages=True)`,
    così l'app non li ripete.
- I test di parità confrontano web e app: `MarketWizardParityTests` e
  `MarketManageParityTests` in `auctions/tests/test_market.py`,
  `CompetitionWizardParityTests` in `auctions/tests/test_competitions.py`,
  `FootballersPageTests` in `auctions/tests/test_footballers.py`,
  `TeamsPageTests` in `auctions/tests/test_participant_accounts.py`,
  `AppPagesParityTests` in `auctions/tests/test_app_pages.py`,
  `GiornatePageTests` e `AdminFormationEditorTests` in `auctions/tests/test_voti.py`,
  `HistoryPageTests` in `auctions/tests/test_competitions.py`.

## Asta live: una sola connessione

Offerte (`bid.html`), maxischermo (`screen.html`) e regia
(`dashboard/_live_js.html`) si collegano all'asta solo con `fmLiveSocket`
(`base.html`): riconnessione con attese crescenti e al risveglio del telefono,
`sync` numerato ogni 4 s (recupera gli eventi persi e misura il ritardo),
connessione senza risposta per 10 s chiusa e riaperta. Il timer parte da
`performance.now() - live.lagMs()`: lo stato è partito dal server un attimo
prima. Mai un `new WebSocket` in una pagina.

## Asta in sala: la lega va dal sito al PC e torna

FantaManager gira sul sito (NAS, VPS: la lega tutto l'anno) e sul PC in sala
(app desktop: l'asta, anche senza internet). `services/sala.py` li collega;
chi usa il PC da solo non ci passa mai.

- Sul sito l'admin genera la chiave della lega (Impostazioni → scheda →
  «Asta in sala»; si mostra una volta, il sito tiene l'impronta in
  `League.sala`). Il PC scarica la lega con indirizzo e chiave
  (`/api/sala/v1/scarica/`): il sito la **blocca**, il PC ne fa una copia
  collegata (`sala["link"]`, con gli id del sito) e ci gioca l'asta.
- Alla fine il PC rimanda lo stato finale di rose, crediti e contratti più il
  registro movimenti (`/risultati/`): il sito lo applica tutto o niente e si
  sblocca. «Annulla e sblocca» (`/sblocca/`) lascia il sito com'era.
- Mentre è bloccata, ogni cambio di rose, crediti o listone della lega
  solleva `LeagueLocked` (`sala.ensure_unlocked`, chiamato in ogni servizio
  che li scrive e nelle view che scrivono direttamente); `SalaLockGuard`
  (`middleware.py`) lo trasforma in messaggio o JSON 423. **Un nuovo
  servizio che scrive rose, crediti o listone chiama `ensure_unlocked`.**
  Formazioni, voti, nomi e il resto restano liberi.
- Chi gioca da fuori sala entra dal **tunnel** del PC (`remote.py`), ma
  dall'app del sito: quando il tunnel apre, cambia o chiude, `remote` avvisa
  chi ascolta (`on_public_url`) e `sala.publish_live` manda al sito
  l'indirizzo e il codice di ogni squadra sul PC (`/live/`). Nella home
  dell'app del sito compare «Asta in corso in sala» → `app_sala_enter`, che
  porta nell'asta già riconosciuti. Se il tunnel cade si riapre da solo
  (finché la regia non preme «Disattiva») e il nuovo indirizzo riparte.
- Con l'asta raggiungibile da internet (`PUBLIC_TOKENS_REQUIRED`) la
  connessione in tempo reale accetta solo le squadre dell'asta, chi gestisce
  la lega e il maxischermo aperto col suo codice (`screen_ok_<id>` in sessione).

## Formazioni: una per giornata

Si schiera per la prossima giornata ancora da giocare (`target_giornata`,
`services/formation.py`); il salvataggio scrive l'ultima formazione
(`Formation`, il modello per le giornate dopo) e la copia della giornata
(`MatchdayFormation`). Quando la giornata parte (blocco a mano, primo sync live,
import dei voti o scadenza `Giornata.starts_at` applicata dallo scheduler
`run_scheduler` o alla prima visita: `lock_formations`) ogni squadra ha la sua copia e da lì non
cambia più: i ricalcoli usano quella, anche se la rosa poi cambia. L'ordine
della panchina lo sceglie il manager ed è la priorità dei cambi. Capitano e vice
(`captain_id`/`vice_id`) stanno fra i titolari e seguono la copia della giornata;
valgono solo se la lega accende `captain_enabled` nelle Regole di punteggio.
L'admin della lega modifica la formazione di ogni squadra per qualsiasi
giornata dalla pagina Giornate (`admin_save_matchday_formation`): tocca solo
la copia di quella giornata e, se la giornata ha già punteggi dai voti, li
ricalcola.

## Regole di punteggio della lega

Soglie dei gol, bonus e malus stanno in `Season.rules` della stagione corrente
della lega, sopra `scoring.DEFAULTS` (Fantacalcio classico). Si leggono sempre
con `scoring.effective_rules(...)`: mette i default e azzera le voci spente
(`rules["off"]`). Si cambiano dalla pagina Giornate (`_scoring_rules.html`,
view `admin_scoring_rules`); `recompute_season` le applica alle giornate già
giocate. Un nuovo bonus si aggiunge a `DEFAULTS`, al motore e a `RULE_GROUPS`
(`views/admin_voti.py`).

## Punteggi a mano

Una lega che gioca su un altro sito (Fantapazz esporta solo un'immagine della
partita) scrive per ogni giornata il totale di ogni squadra nella pagina
Giornate (`admin_giornata_manual_scores` → `set_manual_scores`,
`services/scoring.py`): i gol vengono dalle soglie della lega se non scritti,
poi risultati e classifiche come da voti. Le righe hanno `breakdown.manual`;
il Live mostra quei totali. Un import dei voti o un sync live li ricalcola dai
voti; la correzione di una formazione no.

## Barre di pulsanti: sempre su una riga

Nessuna barra di pulsanti, link o chip (azioni di pagina, sotto-navigazione,
filtri a chip) va a capo su due righe, a nessuna larghezza.

- Alla riga si mette la classe `fm-onerow` (`static/css/console.css`): resta
  su una riga e, se non ci sta, scorre di lato con una sfumatura sul lato dove
  c'è altro. Il titolo della pagina sta fuori dalla riga.
- Se la riga è dentro un contenitore flex in colonna, quel contenitore vuole
  `min-width: 0`, altrimenti si allarga quanto la riga ed esce dallo schermo.
- La barra principale della console (`_console_head.html`) non scorre: le
  voci che non ci stanno passano in «Altro» (mai la pagina attiva).

## Ogni lega è separata

Mercati, sessioni, scambi e regole valgono solo per la lega in cui sono creati.
L'unica cosa comune è l'anagrafica dei calciatori (`Footballer`, da
API-Football, ruoli corretti solo dal superuser): è il listone di default delle
leghe senza una lista propria (`League.own_listone`), ma i giocatori di ogni
lega (`Player`, con rosa, costo e contratto) restano della lega.
L'admin di una lega (proprietario o co-admin in `league.admins`) gestisce solo
le sue leghe: usare `user_can_manage_league` / `manageable_leagues`
(`views/common.py`), mai il solo `league.owner`.

## Stagioni

`start_new_season` chiude la `Season` corrente e ne apre una nuova
(`roll_season`, `services/competitions.py`) con le stesse regole e competizioni:
giornate e classifiche dell'anno finito restano lì (storico). Le coppe a
eliminazione avanzano da sole a fine turno (`services/knockout.py`).

## Test

```
python manage.py collectstatic --noinput   # serve una volta: i template usano il manifest
python manage.py test auctions
```
