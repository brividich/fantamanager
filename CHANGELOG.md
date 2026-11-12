# Changelog

Una sezione per versione, la più recente in cima. Le versioni seguono il
[versionamento semantico](https://semver.org/lang/it/): `X.Y.Z`, dove `X`
cambia quando serve fare qualcosa a mano per aggiornare, `Y` per le funzioni
nuove e `Z` per le correzioni.

Il numero vive in `liveauction/__init__.py` (`__version__`) e in
`packaging/installer.iss` (`MyAppVersion`): `ReleaseTests` controlla che
coincidano con l'ultima sezione qui sotto.

**Rilasciare una versione**: aggiorna i tre punti, poi
`git tag vX.Y.Z && git push origin vX.Y.Z`. Il workflow «Build and Publish
Docker Image» pubblica `ghcr.io/brividich/fantamanager:X.Y.Z` (e `:X.Y`,
`:latest`) solo dopo test verdi e un avvio di prova dell'immagine.

## [0.9.0] — da rilasciare

Prima versione numerata (prima l'etichetta era 1.2.0, senza rilasci).

### Sicurezza
- «Collega account» non collega né modifica account di altri (superuser,
  staff, presidenti di altre leghe, iscritti che non giocano nelle tue leghe).
- Un solo `fmEsc` per i dati del server nell'HTML; Content-Security-Policy
  minima su ogni pagina.
- I link nelle email partono da `FM_SITE_URL`, mai dall'Host della richiesta.
- Compose: porta su `127.0.0.1`, `DJANGO_BEHIND_PROXY` spento di default,
  `DJANGO_ALLOWED_HOSTS` obbligatoria, niente origini CSRF con `*`.
- Asta in sala: azioni del PC solo sull'app desktop, indirizzi pubblici,
  tetti su risposta, squadre, giocatori e registro; indirizzo live solo
  `*.trycloudflare.com`.
- Excel «bomba» rifiutato prima di aprirlo; PIN della regia bloccato per
  client, non per tutti.

### Asta live
- Lo scheduler lascia i lotti al ticker della sala quando è vivo (battito
  `Auction.ticker_seen_at`); senza ticker chiude con un margine di 3 s.

### Lega
- Nuova pagina **Impostazioni lega**, in console (barra in alto) e nell'app
  (Regia, menu e Altro): tutte le regole di una lega in un posto, crediti,
  sistema di gioco, rosa, scambi, contratti e tetto si cambiano lì; mercati,
  periodi degli scambi, punteggi e voto base, competizioni, squadre e listone
  sono riassunti com'è adesso, col link alla loro pagina; co-admin.
- Le **rose** della lega anche nell'app: la scheda Rose della Regia è lo
  stesso pannello della Dashboard della console (una squadra o tutte,
  reparti, posti liberi, Assegna, Svincola, Crediti), e dopo un'azione si
  torna sulla stessa squadra.
- La versione dice anche la build: commit e data dell'immagine Docker in
  app → Altro e accanto alla versione nella console, così si vede se il
  server si è aggiornato.

### Mercato
- Verbale di spoglio (PDF e CSV, console e app): nuovo «Allegato A» con ogni
  singola offerta, giocatore per giocatore, con l'orario di consegna al
  millesimo di secondo e il distacco dalla prima offerta; evidenziate le
  offerte a pari importo, dove l'orario può decidere. Visibile solo dopo lo
  spoglio. Corretta la data di spoglio, che restava «In corso».
- Report di mercato da stampare o scaricare (PDF e CSV), dalla console e
  dall'app delle squadre: **registro dell'asta live** (ogni rilancio di ogni
  lotto con l'orario al millesimo, i secondi rimasti al timer, i rilanci
  rifiutati col motivo e le buste di spareggio), **report degli scambi** (chi
  cede cosa, tempi di risposta e di ratifica, bilancio per squadra) e
  **report di mercato della stagione** (ogni movimento squadra per squadra
  con la spesa netta progressiva).
- Buste: a consegna chiusa, appena l'admin guarda le offerte, «Pareggi in
  vista» dice quali giocatori finirebbero in parità e cosa farà la regola
  (con «vince chi ha offerto prima»: chi vince, con orari e distacco).
- Fra gli svincolati non compare più chi non è nel listone: chi è sparito
  dal listone caricato (anche senza «Rimuovi svincolati non più in lista»),
  chi è uscito dalla Serie A nel listone generale e chi è stato perso con la
  cessione all'estero, anche dopo lo svincolo. Non si compra (buste, mercato
  libero, waiver, asta) e torna se ricompare in un listone. Nella scheda
  Listone di Calciatori lo stato «Fuori listone» li mostra all'admin.
- Asta live: un lotto uscito dal listone mentre è sul banco non accetta più
  rilanci né buste; «indietro» e «salta al ruolo» non lo riportano sul
  banco, «rimanda» lo toglie dalla coda e il «prossimo» annunciato lo salta.
  Le code fatte prima dell'aggiornamento vengono ripulite.

### Barra e squadre
- Le voci della barra della console hanno il nome della pagina a cui portano:
  «Lega», «Impostazioni lega», «Giornate e voti», «Leghe e salvataggi»
  (prima «Configurazione», con dentro i salvataggi: spariscono «Carica
  sessione» e «Riprendi sessione»), «Portale». Unite «Stagione e storico» e
  «Importa ed esporta», con le schede in pagina. Stessi nomi negli Strumenti
  della Regia e nel menu dell'app.
- Una squadra creata per sbaglio si elimina dalla sua Scheda (pagina
  Squadre): i giocatori tornano svincolati; una squadra che ha già giocato,
  offerto o scambiato resta.

### Tema chiaro e scuro
- Il tema chiaro copre tutta la console e tutta l'app (prima solo la cornice
  dell'app: molte schede restavano scure o con testi bianchi sul bianco).
- «Tema» con tre scelte, Scuro, Chiaro e Automatico (come il dispositivo), in
  «Il mio account» (console e app) e in Altro; scorciatoia ☀/☾ nella barra
  della console e nel menu dell'app. Anche il colore della barra del telefono
  e i controlli nativi (menu a tendina, date) seguono il tema.
- Il maxischermo resta scuro.

### Backup e ripristino
- Su PostgreSQL (NAS, Docker) il Supervisor ripristina un backup dal browser:
  prima salva lo stato attuale, poi ripristina tutto in una transazione.
  Chi ripristina resta collegato anche se la copia è di prima del suo login.
- Su PostgreSQL «Crea Snapshot» non dice più «completato con successo:
  .richiesta»: dice che il dump è chiesto al servizio `backup`, mostra la
  richiesta in attesa finché il file non compare in elenco (la pagina si
  aggiorna da sola) e, se il servizio non risponde, è fermo o il `pg_dump`
  non riesce, lo dice con cosa fare. Il servizio `backup` del compose lascia
  un battito (`.servizio`) e l'errore dell'ultimo dump (`.errore`) in
  `./backups`, e una richiesta non riuscita resta in attesa: per averli,
  aggiorna il `docker-compose.yml`.

### Rilascio
- L'immagine Docker si pubblica solo dopo test verdi e un avvio di prova
  (`/healthz/`, `check --deploy`); tag `:main`, `:sha-…`, `:X.Y.Z`.
