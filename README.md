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

## Test

```bash
python manage.py test
```

Copre (21 test): offerta accettata in `LIVE`, rifiuto se chiusa / timer scaduto
/ partecipante inattivo / incremento non valido, ricalcolo del prezzo su due
offerte ravvicinate, log delle offerte rifiutate, rate limit, lifecycle
pause/resume/auto-close, **anti-sniping** (estende / non estende), le view
principali, e il **flusso end-to-end via WebSocket** (offerta accettata e
broadcast, rifiuto senza sessione).

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
    ├── models.py           # Auction, Participant, Bid
    ├── services.py         # logica bid/lifecycle (atomica, server-side)
    ├── consumers.py        # WebSocket consumer (gruppo per asta + timer sync)
    ├── routing.py          # rotte WebSocket
    ├── views.py            # dashboard, join, bid, screen, control endpoints
    ├── urls.py
    ├── admin.py
    ├── tests.py
    └── templates/auctions/ # base, admin_dashboard, join, bid, screen
```
