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
  👉 **`http://<IP_DEL_TUO_NAS>:8000/`**
- **Command Center / Dashboard Regia**:  
  👉 **`http://<IP_DEL_TUO_NAS>:8000/dashboard/`**
  *(PIN predefinito di sblocco: `123456`)*
- **Partecipanti all'Asta da Smartphone**:  
  Tutti i partecipanti collegati al Wi-Fi di casa possono accedere inserendo nel browser del proprio telefono:  
  👉 **`http://<IP_DEL_TUO_NAS>:8000/`**

---

## ⚠️ Note Utili e Risoluzione Problemi

### 1. Se la porta 8000 è già occupata sul Synology
Se sul tuo NAS hai già un altro servizio che usa la porta 8000 (es. Portainer):
- Modifica la riga `ports` nel file `docker-compose.yml` su File Station:
  ```yaml
  ports:
    - "8090:8000"
  ```
- Salva e l'app sarà raggiungibile su `http://<IP_DEL_TUO_NAS>:8090/`.

### 2. Permessi di Scrittura su Synology DSM
Container Manager esegue i container con permessi specifici. Se nei log vedi errori di permesso sul database (`sqlite3.OperationalError: attempt to write a readonly database`):
1. In **File Station**, fai clic destro sulla cartella `/docker/fantamanager/data`.
2. Scegli **Proprietà** ➔ scheda **Autorizzazione**.
3. Assicurati che il gruppo **Everyone** o **Authenticated Users** abbia permessi di **Lettura e Scrittura**, spuntando *"Applica a questa cartella, alle sottocartelle e ai file"*.

### 3. Backup dei Dati
Poiché i dati sono salvati nella cartella `/docker/fantamanager/data/`, puoi includere questa cartella nelle tue routine di **Synology Hyper Backup** per avere copie di backup automatiche e programmate di tutta la tua lega del fantacalcio!
