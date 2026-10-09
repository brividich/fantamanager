# 🐳 FantaManager — Guida Docker & Docker Compose

Questa guida spiega come compilare, avviare e gestire **FantaManager** all'interno di un container **Docker** con il server asincrono **Daphne ASGI**, supporto completo ai WebSocket in tempo reale e persistenza dei dati.

---

## 📋 Prerequisiti

Assicurati di avere installato:
- **Docker** e **Docker Compose** (su macOS/Windows tramite [Docker Desktop](https://www.docker.com/products/docker-desktop/), su Linux tramite `docker-ce` e `docker-compose-plugin`).

Verifica l'installazione con:
```bash
docker --version
docker compose version
```

---

## 🚀 Avvio Rapido (Quickstart)

### 1. Clona o apri la directory del progetto
```bash
cd asta
```

### 2. Configura le variabili d'ambiente (obbligatorio)
Crea il file `.env` copiando il template e scrivi i valori della tua installazione:
```bash
cp .env.docker.example .env
```
Senza `DJANGO_ALLOWED_HOSTS` il compose non parte e lo dice. Le righe da guardare:

| Variabile | Cosa scrivere |
| --- | --- |
| `DJANGO_ALLOWED_HOSTS` | Il tuo dominio (e l'IP del NAS se lo apri anche in LAN), separati da virgola. Mai `*`. |
| `FM_SITE_URL` | L'indirizzo pubblico https, es. `https://fantamanager.example.it`: da qui partono i link delle email. |
| `DJANGO_BEHIND_PROXY` | `True` **solo** dietro un reverse proxy (porta su `127.0.0.1`). Altrimenti `False`. |
| `FM_BIND` | Vuota = la porta risponde solo a `127.0.0.1` (dietro il proxy). `0.0.0.0` per un uso solo in LAN, senza proxy. |

Lascia **vuota** la riga `DJANGO_SECRET_KEY=`: al primo avvio il container genera
una chiave vera e la conserva in `./data/.secret_key`. Un valore copiato da un file
del repository è pubblico: il container lo ignora e lo segnala nel log.

### 3. Compila e avvia il container
```bash
docker compose up -d --build
```

### 4. Apri l'applicazione nel browser
- **Home / Portale di Accesso (Scelta & Login)**:  
  👉 **[http://localhost:8088/](http://localhost:8088/)** (dalla macchina stessa) o il tuo `FM_SITE_URL`
- **Command Center / Dashboard (Regia)**:  
  👉 **[http://localhost:8088/dashboard/](http://localhost:8088/dashboard/)**
- **Collegamento smartphone partecipanti**:  
  👉 dal dominio (`FM_SITE_URL`); in LAN senza proxy, con `FM_BIND=0.0.0.0`: `http://<IP_DEL_TUO_PC>:8088/`

---

## 💾 Persistenza dei Dati (Volumi & PostgreSQL)

Lo stack Docker include **PostgreSQL 16 Alpine** come motore di database principale e ad alta concorrenza, oltre ai volumi per file media e log:

| Risorsa / Percorso | Tipo | Contenuto |
| :--- | :--- | :--- |
| `postgres_data` | Volume Docker | Database PostgreSQL (tabelle, utenti, leghe, rose, aste e rilanci) |
| `./data/` | Bind mount | Dati locali, listoni Excel e file di export |
| `./media/` | Bind mount | Foto dei calciatori, loghi delle squadre caricati |
| `./logs/` | Bind mount | Log applicativi e storico rilanci (`fantamanager.log`) |
| `./backups/` | Bind mount | Dump SQL e snapshot automatici |

> [!TIP]
> **Backup & Ripristino PostgreSQL**:  
> Per creare un backup completo di PostgreSQL:
> ```bash
> docker compose exec db pg_dump -U fantamanager fantamanager > backups/backup_$(date +%Y%m%d).sql
> ```
> Per ripristinare un dump SQL:
> ```bash
> docker compose exec -T db psql -U fantamanager fantamanager < backups/backup_YYYYMMDD.sql
> ```

---

## 🛠️ Comandi Utili per la Gestione

### Visualizzare i log in tempo reale
```bash
docker compose logs -f app
```

### Fermare il container
```bash
docker compose down
```

### Riavviare il container
```bash
docker compose restart
```

### Eseguire la suite di test nel container
```bash
docker compose run --rm app python manage.py test auctions
```

### Creare un account amministratore Django
```bash
docker compose run --rm app python manage.py createsuperuser
```

### Eseguire un comando personalizzato di Django
```bash
docker compose run --rm app python manage.py <comando>
```

---

## 🔒 Sicurezza & Accesso Remoto

- **PIN Regia Predefinito**: `123456` (configurabile o generato dal tunnel Cloudflare per proteggere la regia quando esposta su internet).
- **Partecipanti all'Asta**: Area libera, non richiede credenziali per fare rilanci o aprire il maxischermo su HDMI.
- **Porta personalizzata**: Puoi avviare su una porta differente (es. 8080) specificando `PORT=8080 docker compose up -d`.

---

## ⏱️ Scheduler

Il servizio `scheduler` (stessa immagine, comando `python manage.py run_scheduler`)
fa quello che prima aspettava una pagina aperta:

- chiude i lotti dell'asta scaduti e passa al successivo anche se nessuno è collegato;
- apre e chiude le sessioni di mercato alle loro date;
- blocca le formazioni alla **scadenza della giornata** (pagina Giornate → «Scadenza», oppure
  «🗓️ Scadenze dal calendario» che prende il primo calcio d'inizio di ogni turno da API-Football).

Senza scheduler l'app funziona come prima: una scadenza passata si applica alla prima visita
della pagina Formazione o Giornate. Un giro solo: `docker compose exec app python manage.py run_scheduler --once`.

## 🔐 Variabili di sicurezza

| Variabile | Default | A cosa serve |
| --- | --- | --- |
| `DJANGO_BEHIND_PROXY` | `False` | Fidarsi di `X-Forwarded-*` dal reverse proxy: `True` solo con la porta su `127.0.0.1` |
| `FM_SITE_URL` | vuota | Indirizzo pubblico https: link delle email e origine CSRF fidata. Senza, con `ALLOWED_HOSTS=*` le email con link non partono |
| `FM_BIND` | `127.0.0.1` | Su quale indirizzo della macchina il compose pubblica la porta |
| `DJANGO_PROXY_HOPS` | `1` | Quanti proxy aggiungono un indirizzo a `X-Forwarded-For` |
| `FM_MAX_REQUEST_BYTES` | 25 MB | Dimensione massima di una richiesta (upload compresi) |
| `FM_FIELD_ENCRYPTION_KEY` | derivata dalla `SECRET_KEY` | Chiave Fernet per i segreti salvati nel DB (password SMTP) |
| `FM_REMOTE_STANDINGS` | `True` | Lettura della classifica dal link della lega (solo indirizzi pubblici) |

Il container gira come utente `app` (uid 1000): all'avvio l'entrypoint passa a quell'utente le
cartelle `data`, `media`, `logs`, `backups`. Se il NAS non lo permette resta root e lo scrive nel log
(`FM_RUN_AS_ROOT=1` per restare root di proposito). Dopo questo aggiornamento serve
`docker compose build` (o il pull dell'immagine), non solo il riavvio.

## 🌐 Servizio pubblico (profilo server)

Per un servizio su internet aperto a più leghe (non il NAS di casa) avvia con
`DJANGO_SETTINGS_MODULE=liveauction.settings_server`. Il profilo si rifiuta di partire con DEBUG
acceso, `DJANGO_ALLOWED_HOSTS=*`, senza `FM_SITE_URL` https, senza PostgreSQL o con la password d'esempio del database; forza
link squadra con token, cookie solo HTTPS, log su stdout, cache su Redis se c'è `REDIS_URL`, e
spegne il tunnel cloudflared e la lettura delle classifiche da siti terzi.
