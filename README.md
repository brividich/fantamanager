# Live Auction Server

Web app locale per gestire **aste live in LAN**. Un PC fa da server; i
partecipanti si collegano dal browser dei loro smartphone/tablet/PC tramite un
URL locale e rilanciano con pochi grandi pulsanti. L'amministratore avvia,
mette in pausa, riprende e chiude l'asta. **Timer e validazione delle offerte
sono gestiti esclusivamente lato server.**

Stack: **Python + Django + Django Channels (WebSocket) + SQLite**. Frontend in
HTML/CSS/JS semplice, responsive. Nessun Redis necessario per l'MVP (channel
layer in-memory, processo singolo).

---

## Caratteristiche principali

- Timer **server-side** autoritativo: le offerte sono accettate solo se l'asta
  è `LIVE` e `now < ends_at`.
- Le offerte sono **validate solo dal server**. Il client invia solo
  `auction_id`, l'identità di sessione e l'`increment`; il server calcola
  l'importo (`current_price + increment`).
- Vince **chi arriva prima al server**: bid serializzati con transazione
  atomica + `select_for_update`.
- **Tutte** le offerte (anche rifiutate) sono registrate con motivo del rifiuto,
  timestamp server (ms), IP e user-agent.
- Aggiornamenti **realtime via WebSocket** (stato, prezzo, timer, offerte).
- Rate limit anti-spam configurabile per partecipante.
- **Anti-sniping (soft-close)**: un'offerta negli ultimi secondi estende
  automaticamente il timer (lato server). Configurabile per asta
  (`antisnipe_seconds`, 0 = disattivato).
- UI curata e **responsive** (tema "casa d'aste", anello-timer animato, pulse
  sul prezzo, banner "tempo esteso"), con fallback eleganti se la LAN è offline.

---

## Pagine

| URL | Descrizione |
|-----|-------------|
| `/admin-auction/` | Dashboard admin (login richiesto): crea/seleziona asta, Start/Pause/Resume/Stop, lista offerte live, annulla offerta con audit. |
| `/join/` | Ingresso partecipante: nome + PIN opzionale → sessione → pagina offerta. |
| `/bid/<auction_id>/` | Pagina partecipante: titolo, timer, stato, prezzo, miglior offerente, 4 pulsanti rilancio. |
| `/screen/<auction_id>/` | Maxischermo: prezzo grande, timer grande, stato, ultimo offerente, log ultime offerte. |
| `/admin-auction/market/` | Mercato a buste e scambi (admin): sessioni, anteprima e spoglio, pareggi, annullamento, ratifica scambi. |
| `/app/` | App del fantallenatore: home, rosa, formazione, lega, **mercato** (`/app/mercato/`) e **scambi** (`/app/scambi/`). Per chi gestisce una lega c'è anche la **Regia** (`/app/regia/`). |
| `/admin-auction/setup/` | **Nuova lega** guidata: listone e rose → lega e squadre → asta, con riepilogo dal vivo. |
| `/admin-auction/config/` | **Impostazioni** delle leghe che gestisci (nome, crediti, rosa, Classic/Mantra, regole) e pulizia di aste e salvataggi. |
| `/django-admin/` | Admin Django nativo (accesso dati grezzi / debug). |

---

## Installazione

Richiede **Python 3.10+**.

### 1. Virtualenv

**Windows (PowerShell):**
```powershell
cd "C:\Users\luca\Desktop\Asta live"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Linux/macOS:**
```bash
cd "Asta live"
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Dipendenze
```bash
pip install -r requirements.txt
```

### 3. (Opzionale) configurazione
```bash
cp .env.example .env   # Windows: copy .env.example .env
```

### 4. Migrazioni database
```bash
python manage.py migrate
```

### 5. Crea l'utente admin
```bash
python manage.py createsuperuser
```

### 6. Avvia il server (in ascolto su tutta la LAN)
```bash
python manage.py runserver 0.0.0.0:8000
```
> `runserver` usa automaticamente lo stack ASGI/Channels (daphne) grazie a
> `daphne` in `INSTALLED_APPS`, quindi i WebSocket funzionano subito.

---

## Trovare l'IP locale del PC server

**Windows:**
```powershell
ipconfig
```
Cerca *"Indirizzo IPv4"* sulla scheda di rete attiva (es. `192.168.1.10`).

**Linux/macOS:**
```bash
ip addr            # oppure: ifconfig
hostname -I
```

---

## URL da dare ai partecipanti

Con IP del server `192.168.1.10` e porta `8000`:

- **Admin** (solo tu): `http://192.168.1.10:8000/admin-auction/`
- **Partecipanti**: `http://192.168.1.10:8000/join/`
- **Maxischermo** (proiettore/TV): `http://192.168.1.10:8000/screen/<id_asta>/`

Tutti i dispositivi devono essere sulla **stessa rete Wi‑Fi/LAN** del PC server.
Se i partecipanti non si collegano, verifica il **firewall** del PC (consenti
le connessioni in entrata sulla porta 8000).

---

## Flusso d'uso tipico

1. Apri `/admin-auction/`, fai login, **crea un'asta** (prezzo base, rilancio
   minimo, durata, pulsanti rilancio). L'asta nasce in stato `READY`.
2. Proietta `/screen/<id>/` sul maxischermo.
3. I partecipanti aprono `/join/`, inseriscono il nome e scelgono l'asta.
4. Premi **Start**. Il timer parte lato server; i pulsanti dei partecipanti si
   attivano.
5. Pause/Resume/Stop quando serve. Allo scadere del timer l'asta si **chiude
   automaticamente** e i pulsanti si disabilitano.
6. Se necessario, **annulla** una singola offerta accettata dalla dashboard:
   il prezzo viene ricalcolato e l'offerta resta nel log come `annullata`.

---

## Console e app: una lega sola

Console (`/dashboard/`) e app (`/app/`) lavorano sulla **stessa lega**: la
lega scelta da una parte è già selezionata dall'altra. In alto nella console
il selettore **Console | App** apre la stessa lega nell'app; nell'app il tasto
**Console** fa il viaggio inverso.

Chi gestisce una lega (proprietario o superadmin) nell'app trova la scheda
**Regia**: le cose da fare (asta in corso, scambi da ratificare, spoglio delle
buste, listone o squadre mancanti, squadre senza accesso), i numeri della lega,
tutte le squadre con la loro rosa, le aste, le buste e i collegamenti agli
strumenti della console. Gli scambi si ratificano direttamente da lì.

Con **Vedi come** l'admin apre l'app come una qualunque squadra delle sue leghe
(per controllare cosa vede un allenatore o agire per chi non può): un banner lo
ricorda sempre e riporta alla Regia. Un admin senza squadra propria non passa
più dal login dell'app: entra direttamente in Regia.

## Nuova lega e impostazioni

La **Nuova lega** (`/admin-auction/setup/`) riconosce da solo il formato del
file rose, ne prende squadre e budget, suggerisce Mantra se il listone ha la
colonna RM, propone preset per rosa e ritmo dell'asta, accetta un elenco di
squadre incollato, e tiene una bozza se la pagina viene ricaricata.

Le **Impostazioni** (`/admin-auction/config/`) mostrano una scheda per lega con
la lista di controllo (listone, squadre, accessi, prima asta) e un editor per
nome, crediti, sistema di gioco, rosa e regole (scambi, ratifica, contratti,
tetto salariale). Un admin di lega vede e modifica solo le sue leghe; per
eliminarne una bisogna scriverne il nome.

## Mercato a buste e scambi

**Buste (mercato di riparazione).** L'admin crea una sessione per la lega:
apertura subito o programmata, chiusura automatica opzionale, taglio
condizionato (con modalità di rimborso) e tetti di acquisto per ruolo. I
fantallenatori inviano dall'app buste segrete con importo e priorità
(1 = obiettivo principale), eventualmente legate al taglio di un proprio
giocatore.

Lo spoglio (`auctions/services/market.py`):

- ogni calciatore va all'offerta più alta **valida**; a parità d'importo vince
  la priorità più bassa, a parità anche di priorità è pari merito;
- le buste di ciascuno entrano in gioco in ordine di priorità, così crediti e
  slot vengono spesi prima sugli obiettivi principali;
- al momento dell'assegnazione si ricontrollano calciatore ancora libero,
  crediti, slot della rosa (Classic e Mantra), tetti per ruolo e taglio ancora
  possibile.

Prima di confermare l'admin può vedere l'**anteprima** dell'esito. I **pari
merito** si risolvono scegliendo il vincitore o con un sorteggio. Uno spoglio
si può **annullare** finché i calciatori coinvolti non sono stati toccati.

**Scambi.** Dall'app un fantallenatore propone calciatori e/o crediti a
un'altra squadra; chi riceve accetta o rifiuta. Per ogni lega l'admin decide se
gli scambi sono attivi e se serve la sua **ratifica**. Rose, crediti e slot
vengono ricontrollati quando lo scambio viene eseguito.

---

## Test

```bash
python manage.py test
```

Tra le altre cose copre: offerta accettata in `LIVE`, rifiuto se chiusa / timer scaduto
/ partecipante inattivo / incremento non valido, ricalcolo del prezzo su due
offerte ravvicinate, log delle offerte rifiutate, rate limit, lifecycle
pause/resume/auto-close, **anti-sniping** (estende / non estende), le view
principali, il **flusso end-to-end via WebSocket** (offerta accettata e
broadcast, rifiuto senza sessione), lo **spoglio delle buste**
(`test_market.py`) e gli **scambi** (`test_trades.py`).

Con `whitenoise` installato le view renderizzano gli static dal manifest:
esegui prima `python manage.py collectstatic --noinput`, altrimenti i test che
aprono pagine falliscono con *Missing staticfiles manifest entry*.

---

## Limiti dell'MVP

- **Processo singolo**: il channel layer è in-memory, quindi va eseguito un solo
  worker/`runserver`. Per più processi serve `channels-redis` + Redis.
- **SQLite + concorrenza**: la serializzazione delle offerte è garantita dalla
  transazione atomica; su SQLite `select_for_update` è di fatto un lock globale
  in scrittura (ok per la scala LAN, non per alto carico). Per produzione usare
  PostgreSQL.
- Identità partecipante basata su **sessione browser**: chiudere il browser /
  cambiare dispositivo crea una nuova sessione. Nessun login forte per i
  bidder (per design dell'MVP).
- HTTP in chiaro sulla LAN (nessun HTTPS). Adatto a rete fidata.
- Nessuna gestione immagini/categorie/più lotti collegati.

---

## Prossimi miglioramenti consigliati

- `channels-redis` + Redis per scalare su più processi e tenere il timer in un
  unico ticker centralizzato (invece di uno per connessione).
- PostgreSQL per concorrenza reale ad alto carico.
- Autenticazione partecipanti con PIN verificato e lista pre-registrata.
- HTTPS (reverse proxy: nginx/Caddy) e hardening `ALLOWED_HOSTS`/CSRF.
- Export CSV del log offerte e report di chiusura.
- Self-hosting dei webfont per un look identico anche del tutto offline.

---

## Struttura del progetto

```
Asta live/
├── manage.py
├── requirements.txt
├── README.md
├── .env.example
├── .gitignore
├── liveauction/            # progetto Django
│   ├── settings.py         # config (Channels in-memory, SQLite, tunables)
│   ├── urls.py
│   ├── asgi.py             # routing HTTP + WebSocket
│   └── wsgi.py
└── auctions/               # app principale
    ├── models/             # Auction, Participant, Player, League, MarketSession/MarketBid, Trade, …
    ├── services/           # logica server-side: bidding, lifecycle, queue, market, trade, …
    ├── views/              # console (admin_*), app del fantallenatore (app.py, app_admin.py = Regia), bidder, screen
    ├── consumers.py        # WebSocket consumer (gruppo per asta + timer sync)
    ├── routing.py          # rotte WebSocket
    ├── urls.py
    ├── tests/
    └── templates/auctions/ # console (dashboard/ = parti della regia), app_*, bid, screen
```
