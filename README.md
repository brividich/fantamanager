# FantaManager

Il gestionale per le **leghe di fantacalcio dinasty**: quelle che tengono le
rose da una stagione all'altra, con contratti, rinnovi, mercati e scambi.
FantaManager porta la lega tutto l'anno — asta, mercato di riparazione a
buste, scambi, formazioni, voti e classifiche — e fa l'asta dal vivo in sala,
con i telefoni dei fantallenatori e un maxischermo.

Ci sono due facce, con le stesse funzioni:

- la **console** (`/dashboard/`) per chi organizza la lega, dal PC;
- l'**app** (`/app/`) per i fantallenatori dal telefono, con la **Regia**
  (`/app/regia/`) per chi gestisce la lega.

---

## Cosa fa

- **Asta live**: timer e offerte decisi solo dal server, anti-sniping,
  asta alle buste per i lotti contesi, maxischermo (`/screen/<id>/`), pagina
  offerte sul telefono, regia con coda dei giocatori; riconnessione automatica
  e nessuna offerta persa se il database rallenta.
- **Mercato**: buste segrete con priorità e taglio condizionato, anteprima
  dello spoglio, pareggi; svincolati, waiver, clausole, rinnovi.
- **Scambi** fra squadre, con ratifica dell'admin, prestiti e regole della lega.
- **Contratti dinasty**: anni di contratto, rinnovi col dado, rescissioni,
  giocatori usciti dalla Serie A con compenso, tetto salariale.
- **Stagione**: formazione per ogni giornata, voti da file o live
  (API-Football, anche voto algoritmico), punteggi con le regole della lega,
  competizioni (campionato, coppe, formati speciali), storico e albo d'oro.
- **Squadre e accessi**: account dei fantallenatori, link e QR personali,
  inviti via email, scheda squadra e lista rinnovi in Excel o PDF.
- **Asta in sala**: una lega che vive sul sito si scarica sul PC della sala
  per l'asta (anche senza internet) e i risultati tornano al sito alla fine.

Ogni lega è separata: mercati, sessioni, scambi e regole valgono solo per la
lega in cui sono creati. In comune c'è solo l'anagrafica dei calciatori.

---

## Tre modi di installarlo

### 1. App desktop (Windows, macOS)

Un installer a doppio clic, con database SQLite integrato: il PC fa da server
e i telefoni si collegano dallo stesso wifi; «Accesso da internet» apre un
tunnel per chi gioca da fuori. Come si costruisce l'installer:
[`packaging/BUILD.md`](packaging/BUILD.md).

### 2. Docker su un NAS o un server

PostgreSQL 16, scheduler e backup automatici, dietro un reverse proxy HTTPS.

```bash
cp .env.docker.example .env      # poi scrivi DJANGO_ALLOWED_HOSTS e FM_SITE_URL
docker compose up -d --build     # oppure: docker compose -f docker-compose.ghcr.yml up -d
```

Guide passo passo: [`DOCKER.md`](DOCKER.md) e, per Synology,
[`SYNOLOGY_NAS.md`](SYNOLOGY_NAS.md).

Il server dell'asta va tenuto a **un solo processo** Daphne: il ticker che
chiude i lotti gira dentro quel processo finché qualcuno è collegato, e lo
scheduler (servizio a parte) chiude i lotti rimasti senza nessuno. Dopo un
riavvio, **Supervisor → Info Server & Health** elenca le aste rimaste a metà
con un tasto «Riprendi».

### 3. Sviluppo

Python 3.11 o più recente.

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env                                  # facoltativo
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver 0.0.0.0:8000
```

`runserver` usa Daphne (Channels), quindi i WebSocket dell'asta funzionano
subito. Poi apri `http://localhost:8000/dashboard/`; i telefoni sullo stesso
wifi usano l'IP del PC (`http://192.168.x.y:8000/app/`). Le regole per chi
sviluppa (parità console/app, sicurezza, asta live) sono in
[`CLAUDE.md`](CLAUDE.md).

---

## Le pagine principali

| Indirizzo | Cosa c'è |
|---|---|
| `/dashboard/` | Console: la lega, l'asta dal vivo (regia), squadre, giocatori, contratti, mercato, giornate, impostazioni |
| `/dashboard/setup/` | Nuova lega guidata: listone e rose, squadre, prima asta e inviti |
| `/app/` | App del fantallenatore: rosa, formazione, lega, mercato, scambi; `/app/regia/` per chi gestisce la lega |
| `/join/`, `/bid/<id>/` | Ingresso all'asta e pagina delle offerte |
| `/screen/<id>/` | Maxischermo dell'asta |
| `/supervisor/` | Il superadmin: utenti, posta, salute del server, backup, voto algoritmico |
| `/healthz/` | `200` se processo e database rispondono (per Docker e il proxy) |

---

## Fonti dei dati

FantaManager non legge le pagine dei siti di fantacalcio al posto
dell'utente e non ridistribuisce i loro file. I dati arrivano da:

- **file caricati dalla lega**: listone/quotazioni, rose (export «Rose Lega»
  di Fantapazz, Excel, `.csv` di Leghe Fantacalcio), statistiche e voti di
  giornata. Ognuno usa i file che ha scaricato dal proprio sito;
- **API-Football** (`APIFOOTBALL_KEY`): anagrafica dei calciatori con foto,
  club di chi esce dalla Serie A e **voti live** (il rating API-Football
  arrotondato al mezzo punto; i voti ufficiali restano quelli del file di fine
  giornata). Per i voti live della stagione in corso serve un piano a
  pagamento: il gratuito non la copre e non regge un aggiornamento al minuto;
- **statistiche del server** (facoltative): con `FANTAMANAGER_STATS_FILE`
  l'admin indica un proprio file di statistiche (per Docker, in `./data`) e
  ogni import del listone lo applica da solo. `FANTAMANAGER_STATS_SEASON` è
  l'etichetta mostrata in console.

Il `.gitignore` esclude `.xlsx`/`.xls`/`.csv` dalla radice, da `data/` e da
`auctions/data/`: quei file restano sulla macchina di chi li usa.

---

## Sicurezza: se lo esponi su internet

- **`FM_SITE_URL`**: l'indirizzo pubblico https (`https://fanta.tuodominio.it`).
  Da qui partono i link delle email (reset password, inviti) ed è un'origine
  fidata per i form. Senza, e con `DJANGO_ALLOWED_HOSTS=*`, le email con un
  link non partono.
- **`DJANGO_ALLOWED_HOSTS`**: i tuoi domini, mai `*` (nel compose è
  obbligatoria).
- **Reverse proxy**: HTTPS davanti al container; la porta del container resta
  su `127.0.0.1` (di default nel compose) e `DJANGO_BEHIND_PROXY=True` solo in
  quel caso, altrimenti chiunque potrebbe falsificare l'indirizzo del client.
- **`liveauction.settings_server`**: il profilo per un servizio pubblico
  aperto a più leghe. Si rifiuta di partire con DEBUG acceso, host `*`, senza
  `FM_SITE_URL` https, senza PostgreSQL o con la password d'esempio; forza
  i link con token e i cookie solo HTTPS.
- Le altre variabili (`FM_MAX_REQUEST_BYTES`, `FM_FIELD_ENCRYPTION_KEY`, …):
  tabella in [`DOCKER.md`](DOCKER.md#-variabili-di-sicurezza).

---

## Backup e ripristino

### Database e backup sul NAS

Con `docker-compose.yml` / `docker-compose.ghcr.yml` il database è
**PostgreSQL 16** (servizio `db`, dati nel volume Docker `postgres_data`);
SQLite resta per l'app desktop e lo sviluppo in locale.

I backup li fa il servizio **`backup`** dello stesso compose: `pg_dump` della
stessa versione del server, all'avvio, ogni `BACKUP_EVERY_HOURS` ore (6) e
entro un minuto dall'avvio e dalla fine di ogni asta. Finiscono in `./backups` come
`pg-AAAAMMGG-HHMMSS.sql.gz` (ora UTC); restano i `BACKUP_KEEP` più recenti
(30). L'ultimo backup si vede in **Supervisor → Info Server & Health**.
Copia ogni tanto `./backups` fuori dal NAS: un backup sullo stesso disco non
protegge dal disco che si rompe.

Ripristino (sostituisce **tutto** il contenuto del database con quello del
file scelto):

```bash
docker stop fantamanager                       # niente scritture durante il ripristino
gunzip -c backups/pg-20260927-180000.sql.gz \
  | docker exec -i fantamanager-db psql -U fantamanager -d fantamanager -v ON_ERROR_STOP=1
docker start fantamanager
```

(con utente o database diversi nel `.env`, usa i tuoi `POSTGRES_USER` /
`POSTGRES_DB`).

La password d'esempio di Postgres (`fantamanager_secret_pass`) è nel
repository pubblico: il database non ha porte aperte fuori da Docker, ma
conviene metterne una tua nel `.env` (`POSTGRES_PASSWORD`). Su un'installazione
già avviata va cambiata anche dentro Postgres
(`docker exec -it fantamanager-db psql -U fantamanager -c "ALTER USER fantamanager PASSWORD '...'"`),
perché il valore del `.env` conta solo alla prima creazione del database.

**Healthcheck.** `GET /healthz/` risponde `200` se il processo e il database
rispondono, `503` se il database no. Lo usano Docker e può usarlo il reverse
proxy.

### App desktop: copie automatiche

Le copie stanno in `backups/` accanto al database, e ne restano tre gruppi:

- **automatiche** (`db-…-auto.sqlite3`): durante un'asta, ogni 5 minuti e solo
  se nel frattempo qualcosa è cambiato; restano le ultime 10;
- **di evento** (avvio e chiusura dell'app, avvio e fine di un'asta, copia
  manuale dal Supervisor): restano le ultime 20;
- in più la più recente di ciascuno degli ultimi 7 giorni.

Così le copie automatiche di una serata lunga non cancellano quella di prima
dell'asta.

**Ripristino** da **Supervisor → Report → Snapshot di Backup**, tasto
«Ripristina» sulla copia scelta. Si fa con le aste in pausa o chiuse; prima
viene salvato lo stato attuale (copia «prima del ripristino»), quindi un
ripristino sbagliato si annulla ripristinando quella. Dopo il ripristino girano
le migrazioni, così anche una copia di una versione precedente funziona.

---

## Test

```bash
python manage.py collectstatic --noinput   # una volta: i template usano il manifest degli static
python manage.py test auctions
```

La CI li esegue su ogni PR due volte: con SQLite e con PostgreSQL 16, il
database del server, con `DJANGO_DEBUG=False`. In locale, per Postgres basta
impostare `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`
(l'utente deve poter creare il database di test). L'immagine Docker si
pubblica solo dopo test verdi e un avvio di prova; le versioni e come si
rilasciano sono in [`CHANGELOG.md`](CHANGELOG.md).
