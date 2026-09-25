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

### 2. (Opzionale) Configura le variabili d'ambiente
Puoi creare un file `.env` copiando il template:
```bash
cp .env.docker.example .env
```
*(Se non crei il file, verranno usati i valori predefiniti sicuri con porta 8000).*

### 3. Compila e avvia il container
```bash
docker compose up -d --build
```

### 4. Apri l'applicazione nel browser
- **Home / Portale di Accesso (Scelta & Login)**:  
  👉 **[http://localhost:8000/](http://localhost:8000/)**
- **Command Center / Dashboard (Regia)**:  
  👉 **[http://localhost:8000/dashboard/](http://localhost:8000/dashboard/)**
- **Collegamento smartphone partecipanti sullo stesso WiFi**:  
  👉 `http://<IP_DEL_TUO_PC>:8000/`

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
