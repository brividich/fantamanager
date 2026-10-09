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

### Rilascio
- L'immagine Docker si pubblica solo dopo test verdi e un avvio di prova
  (`/healthz/`, `check --deploy`); tag `:main`, `:sha-…`, `:X.Y.Z`.
