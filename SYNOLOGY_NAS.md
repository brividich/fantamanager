# 📦 Guida Installazione FantaManager su Synology NAS (Container Manager)

Questa guida illustra passo-passo come installare ed eseguire **FantaManager** su un NAS Synology dotato di **DSM 7.2+** utilizzando l'applicazione ufficiale **Container Manager** (o Docker per versioni precedenti).

---

## 🎯 Panoramica del Pacchetto

Il file compresso **`fantamanager-synology.zip`** contiene l'intera applicazione pronta per il NAS:
- Tutto il codice sorgente e i template.
- Il database attuale pre-popolato in `data/db.sqlite3` (con rose, crediti e configurazioni).
- I file multimediali e audio calciatori in `media/`.
- La configurazione `docker-compose.yml` e `Dockerfile` ottimizzata per Linux x86_64/ARM64.
- Le cartelle di persistenza (`data/`, `media/`, `logs/`, `backups/`) già pronte.

---

## 🛠️ Metodo 1: Installazione con Interfaccia Grafica Synology (Consigliato)

Nessun comando da terminale richiesto: tutto tramite il browser di DSM.

### 1. Carica ed estrai il file sul NAS
1. Accedi al tuo Synology DSM.
2. Apri l'app **File Station**.
3. Naviga nella cartella condivisa **`docker`** (se non esiste, assicurati di aver installato *Container Manager* dal Centro Pacchetti).
4. Crea una nuova cartella chiamata `fantasy-contracts` all'interno di `docker`:
   ```text
   /docker/fantasy-contracts/
   ```
5. Trascina ed estrai il file **`fantamanager-synology.zip`** all'interno di questa cartella, in modo che file come `docker-compose.yml`, `Dockerfile` e la cartella `data/` si trovino direttamente in `/docker/fantasy-contracts/`.

### 1b. Scrivi il file `.env` (obbligatorio)
Nella cartella del progetto copia `.env.docker.example` e rinominalo in **`.env`**
(con *Impostazioni → Mostra file nascosti* lo vedi). Scrivi i valori del tuo NAS:

```text
DJANGO_ALLOWED_HOSTS=fanta.tuodominio.it,192.168.1.10
FM_SITE_URL=https://fanta.tuodominio.it
DJANGO_BEHIND_PROXY=True
```

- `DJANGO_ALLOWED_HOSTS`: il dominio con cui apri l'app da fuori e, se la apri anche in casa
  dall'IP, l'IP del NAS. Senza questa riga il progetto non parte e lo dice.
- `FM_SITE_URL`: l'indirizzo pubblico **https** (solo schema e dominio). Da qui partono i link
  delle email (reset password, inviti).
- `DJANGO_BEHIND_PROXY=True` **solo** se usi il reverse proxy del passo 4: la porta del container
  risponde solo al NAS stesso (`127.0.0.1`) e solo il proxy ci arriva.

Se usi l'app **solo in casa, senza proxy e senza dominio**: `DJANGO_ALLOWED_HOSTS=192.168.1.10`
(l'IP del NAS), `FM_BIND=0.0.0.0`, `DJANGO_BEHIND_PROXY=False` e lascia vuota `FM_SITE_URL`.

---

### 2. Crea il Progetto in Container Manager
1. Apri l'applicazione **Container Manager** da DSM.
2. Nel menu laterale a sinistra, fai clic su **Progetto** (Project).
3. Clicca sul pulsante in alto **Crea**:
   - **Nome del progetto**: `fantasy-contracts`
   - **Percorso**: Fai clic su *Sfoglia* e seleziona `/docker/fantasy-contracts`
   - **Sorgente**: Seleziona **"Usa docker-compose.yml esistente"** (il sistema caricherà in automatico il file presente nella cartella).
4. Fai clic su **Successivo**.
5. *(Opzionale)* Configurazione Portale Web: lascia i valori predefiniti e fai clic su **Successivo**.
6. Spunta la casella **"Avvia il progetto dopo averlo creato"** e clicca su **Fine**.

Container Manager eseguirà il download dell'immagine base Python, installerà le dipendenze e avvierà il container. Il processo richiede 1-2 minuti al primo avvio.

---

### 3. Chiave API-Football (facoltativa)
Serve solo al pulsante «Rileva» dei giocatori usciti dalla Serie A (pagina Contratti).
1. In **File Station** apri `/docker/fantasy-contracts/` (la cartella del progetto,
   quella con `docker-compose.yml`).
2. Se non c'è ancora un file **`.env`**, crealo (tasto destro → *Crea* → file di testo
   chiamato `.env`, oppure copia `.env.docker.example` e rinominalo in `.env`).
   Con *Impostazioni → Mostra file nascosti* lo vedi anche dopo.
3. Aggiungi la riga (senza spazi né virgolette):
   ```text
   APIFOOTBALL_KEY=la_tua_chiave
   ```
   La chiave la trovi nella dashboard di api-sports.io (*Account → My Access*).
   Se il `.env` ha una riga `DJANGO_SECRET_KEY=` con un valore d'esempio, lasciala
   vuota: la chiave vera la genera il container e la tiene in `data/.secret_key`.
4. In **Container Manager → Progetto → fantasy-contracts** premi **Azione → Compila**
   (o *Arresta* e poi *Avvia*): Docker rilegge il `.env` solo ricreando il container.

Le migrazioni del database partono da sole a ogni avvio del container.

---

### 4. Accesso da internet: il reverse proxy di DSM
Il container risponde solo al NAS stesso (porta `8088` su `127.0.0.1`); da fuori ci si arriva in
HTTPS attraverso il proxy di DSM.
1. **Pannello di controllo → Portale di accesso → Avanzate → Proxy inverso → Crea**.
2. *Origine*: protocollo **HTTPS**, nome host `fanta.tuodominio.it`, porta `443`.
3. *Destinazione*: protocollo **HTTP**, nome host `localhost`, porta `8088`.
4. *Intestazione personalizzata → Crea → WebSocket* (serve all'asta live), poi **Salva**.
5. **Pannello di controllo → Sicurezza → Certificato**: assegna un certificato (Let's Encrypt) a
   `fanta.tuodominio.it`.
6. Apri `https://fanta.tuodominio.it`: se vedi la pagina di accesso, è tutto a posto.

---

## 💻 Metodo 2: Installazione Rapida tramite SSH

Se preferisci usare il terminale:

1. Connettiti in SSH al tuo Synology:
   ```bash
   ssh tuo_utente@IP_DEL_TUO_NAS
   ```
2. Spostati nella cartella docker ed estrai lo zip:
   ```bash
   cd /volume1/docker
   mkdir -p fantamanager
   unzip fantamanager-synology.zip -d fantamanager/
   cd fantamanager
   ```
3. Avvia la compilazione e l'esecuzione in background:
   ```bash
   sudo docker compose up -d --build
   ```
4. Controlla che il container sia attivo:
   ```bash
   sudo docker compose ps
   sudo docker compose logs -f app
   ```

---

## 🌐 Come Accedere all'Applicazione

Una volta che il container è in stato **In esecuzione** (colore verde):

- **Home / Portale di Accesso (Scelta & Login)**:  
  👉 **`https://fanta.tuodominio.it/`** (il tuo `FM_SITE_URL`)
- **Command Center / Dashboard Regia**:  
  👉 **`https://fanta.tuodominio.it/dashboard/`**
- **Partecipanti all'Asta da Smartphone**:  
  dallo stesso indirizzo, da casa o da fuori.  
  Solo in LAN senza proxy (`FM_BIND=0.0.0.0`): **`http://<IP_DEL_TUO_NAS>:8088/`**

---

## 🔄 Aggiornare l'app sul NAS

App e scheduler usano la stessa immagine, senza il codice della cartella montato sopra.
- Con `docker-compose.yml` (immagine costruita sul NAS): copia i file nuovi nella cartella (o
  `git pull`), poi **Container Manager → Progetto → Azione → Compila** (via SSH:
  `sudo docker compose up -d --build`). Il solo riavvio non basta più.
- Con `docker-compose.ghcr.yml` (immagine già pronta): *Azione → Compila* scarica l'immagine nuova
  (via SSH: `sudo docker compose -f docker-compose.ghcr.yml pull` e `up -d`).

## ⚠️ Note Utili e Risoluzione Problemi

### 1. Se la porta 8088 è già occupata sul Synology
Se sul tuo NAS hai già un altro servizio che usa la porta 8088:
- Nel file `.env` scrivi `PORT=8090` (non serve toccare `docker-compose.yml`).
- Nel reverse proxy (passo 4) metti come destinazione `localhost:8090`, poi *Compila* il progetto.

### 2. Permessi di Scrittura su Synology DSM
Container Manager esegue i container con permessi specifici. Se nei log vedi errori di permesso sul database (`sqlite3.OperationalError: attempt to write a readonly database`):
1. In **File Station**, fai clic destro sulla cartella `/docker/fantamanager/data`.
2. Scegli **Proprietà** ➔ scheda **Autorizzazione**.
3. Assicurati che il gruppo **Everyone** o **Authenticated Users** abbia permessi di **Lettura e Scrittura**, spuntando *"Applica a questa cartella, alle sottocartelle e ai file"*.

### 3. Backup dei Dati
Poiché i dati sono salvati nella cartella `/docker/fantamanager/data/`, puoi includere questa cartella nelle tue routine di **Synology Hyper Backup** per avere copie di backup automatiche e programmate di tutta la tua lega del fantacalcio!
