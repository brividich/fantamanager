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

### Mercato
- Verbale di spoglio (PDF e CSV, console e app): nuovo «Allegato A» con ogni
  singola offerta, giocatore per giocatore, con l'orario di consegna al
  millesimo di secondo e il distacco dalla prima offerta; evidenziate le
  offerte a pari importo, dove l'orario può decidere. Visibile solo dopo lo
  spoglio. Corretta la data di spoglio, che restava «In corso».

### Rose e contratti
- Nuova pagina **Rose**, uguale nella console (barra in alto) e nell'app
  (Regia → Strumenti → Rose): le rose di tutte le squadre in una schermata,
  oppure una squadra alla volta. L'admin aggiunge un giocatore libero, cambia
  costo, squadra o anni di contratto, svincola scegliendo il rimborso e regola
  i crediti. I manager la aprono dalla Rosa («Tutte le rose»), in sola lettura.
- Contratti: «Imposta» (anni scritti a mano) c'è per ogni giocatore, anche per
  chi è «da tirare» o in rinnovo; vuoto lo rimette «da tirare»; il massimo
  segue il dado della lega (prima era fisso a 4).
- Tutto registrato: ogni cambio di crediti (Dashboard e Rose) è una riga
  «Crediti» nei Movimenti, con il budget prima e dopo; le azioni di Rose e i
  contratti scritti a mano riportano chi li ha fatti. Nei Movimenti nuovo filtro
  «Modifiche e crediti», e gli svincoli si leggono come rimborso (+FM): prima
  alcuni apparivano come spesa.

### Backup e ripristino
- Su PostgreSQL (NAS, Docker) il Supervisor ripristina un backup dal browser:
  prima salva lo stato attuale, poi ripristina tutto in una transazione.
  Chi ripristina resta collegato anche se la copia è di prima del suo login.

### Rilascio
- L'immagine Docker si pubblica solo dopo test verdi e un avvio di prova
  (`/healthz/`, `check --deploy`); tag `:main`, `:sha-…`, `:X.Y.Z`.
