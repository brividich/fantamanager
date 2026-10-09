# Privacy e GDPR — guida per chi gestisce l'istanza

> **BOZZA.** Questa guida e i testi di `/privacy/` e `/termini/` vanno fatti
> rivedere a un professionista prima di aprire il servizio a utenti esterni.

Chi installa FantaManager su un server (NAS, VPS) e lo apre ad altre persone è
il **titolare del trattamento**. Questa pagina riassume cosa fa il software e
cosa devi fare tu.

## 1. Da impostare nel `.env`

| Variabile | A cosa serve | Default |
|---|---|---|
| `FM_PRIVACY_OWNER` | Nome del titolare (persona o associazione) mostrato nell'informativa | vuoto: l'informativa dice «non ancora indicato» |
| `FM_PRIVACY_EMAIL` | Contatto per le richieste privacy | vuoto |
| `FM_RETENTION_BID_IP_DAYS` | Dopo quanti giorni si azzerano IP e user-agent delle offerte | `90` |
| `FM_RETENTION_UNVERIFIED_DAYS` | Dopo quanti giorni si cancellano gli account registrati e mai confermati, senza squadre né leghe | `30` |
| `FM_LEGAL_REQUIRED` | Consenso alla registrazione, email obbligatoria, riaccettazione | `True` sul server, `False` nell'app del PC |
| `BACKUP_KEEP`, `BACKUP_EVERY_HOURS` | Quanti backup tiene il servizio `backup` e ogni quante ore (anche mostrati nell'informativa) | `30`, `6` |

Le versioni dei testi stanno in `auctions/legal.py` (`PRIVACY_VERSION`,
`TERMS_VERSION`, `LEGAL_DATE`). Quando cambi un testo in modo sostanziale alza
la versione: chi aveva accettato la precedente la riaccetta al primo accesso.

## 2. Registro dei trattamenti (semplificato)

| Trattamento | Dati | Interessati | Finalità | Base giuridica | Conservazione |
|---|---|---|---|---|---|
| Account | username, nome, email, password (hash), date | utenti registrati, account creati dal presidente | far funzionare il servizio | contratto (6.1.b) | finché l'account esiste |
| Squadre della lega | nome squadra, email della squadra, codice/link | partecipanti invitati | organizzare la lega | legittimo interesse della lega (6.1.f) | finché la lega esiste; email tolta su richiesta (link nelle email) |
| Gioco | offerte, buste, formazioni, scambi, punteggi | partecipanti | il gioco | contratto | finché la lega esiste (anonimi se l'account è eliminato) |
| Sicurezza delle aste | IP e user-agent delle offerte | chi fa offerte | antifrode, contestazioni | legittimo interesse | `FM_RETENTION_BID_IP_DAYS` giorni |
| Log del server | IP, percorsi, username in accesso | visitatori | sicurezza, diagnosi | legittimo interesse | rotazione: 5 file da 5 MB più quello attivo |
| Prova del consenso | documento, versione, data, IP | utenti registrati | dimostrare il consenso | obbligo (art. 7.1) | finché l'account esiste |
| Registro azioni sensibili | chi, cosa, quando, su chi | utenti e admin | trasparenza, sicurezza | legittimo interesse | finché l'account esiste; poi anonimo |
| Backup | tutto il database | tutti | ripristino | legittimo interesse | ultimi `BACKUP_KEEP` |

## 3. Responsabili e destinatari

| Chi | Quando | Cosa riceve |
|---|---|---|
| Provider della posta (SMTP scelto in Supervisor → Posta) | ogni email | indirizzo e testo dell'email |
| Hosting (server, NAS, VPS) | sempre | database, backup, log |
| Cloudflare (tunnel `trycloudflare.com`) | solo quando una lega fa l'asta da un PC in sala aperta a chi gioca da fuori | il traffico delle pagine dell'asta |
| API-Football | aggiornamento dell'anagrafica dei calciatori | **nessun dato personale** (solo richieste del server) |

Le foto dei calciatori e gli stemmi passano dal server (`/img/…`, cache in
`MEDIA_ROOT/img-cache`), i font sono in `static/fonts`: il browser dei
visitatori non contatta Google né API-Football. Nessun cookie oltre a sessione
e CSRF, quindi nessun banner cookie. Se un giorno servono statistiche di visita,
solo strumenti senza cookie ospitati da te (Umami, Plausible self-hosted).

Firma un accordo di trattamento (DPA) con il provider della posta e con
l'hosting, se sono terzi.

## 4. Conservazione: cosa fa il software da solo

Lo scheduler (`run_scheduler`, servizio `scheduler` in docker-compose) esegue
una volta al giorno, e all'avvio, `python manage.py privacy_cleanup`:

- IP e user-agent delle offerte più vecchie di `FM_RETENTION_BID_IP_DAYS`: azzerati;
- sessioni scadute: cancellate;
- account **registrati da soli dopo questa versione**, mai confermati, senza
  squadre né leghe, più vecchi di `FM_RETENTION_UNVERIFIED_DAYS`: cancellati.
  Gli account nati prima non vengono mai toccati.

Link di conferma email (48 ore) e reset password (`PASSWORD_RESET_TIMEOUT`)
sono firmati con una scadenza: non c'è niente da cancellare.

L'app del PC non ha scheduler: fa la stessa pulizia a ogni avvio.

`python manage.py privacy_cleanup --dry-run` conta senza cambiare niente.

## 5. Diritti degli interessati

Da **Il mio account** (`/account/`, nell'app `/app/account/`) ognuno:

- scarica i propri dati (JSON);
- cambia email (con conferma del nuovo indirizzo);
- elimina l'account: le squadre restano alla lega senza riferimenti personali,
  offerte e storico restano senza IP né user-agent; rifiutato a chi è
  presidente di una lega con altre squadre (deve passarla prima).

Chi riceve le email di una lega può smettere dal link in fondo a ogni email:
l'email della squadra si azzera e il presidente lo vede nella pagina Squadre.

Richieste che il sito non copre da solo (rettifica di dati di gioco,
opposizione, limitazione): rispondi entro un mese dal contatto `FM_PRIVACY_EMAIL`.

## 6. Registro delle azioni sensibili

Supervisor → **Registro privacy** mostra ogni «Vedi come» (Supervisor e
Regia), ogni cambio di credenziali fatto da un admin per conto di un altro,
ogni export dei dati, le email tolte, le disiscrizioni e gli account eliminati.
L'interessato vede le righe che lo riguardano in Il mio account.

## 7. Violazione dei dati (data breach): entro 72 ore

1. **Contieni**: cambia le password del database e dei superadmin, rigenera
   `DJANGO_SECRET_KEY` (invalida sessioni e link) e, se coinvolta la posta,
   la password SMTP. Se serve spegni il servizio (`docker compose stop app`).
2. **Capisci cosa è successo**, guardando:
   - `logs/fantamanager.log` (e `.1`…`.5`): accessi (`Utente autenticato`),
     errori, richieste rifiutate (`Forbidden`), tentativi bloccati;
   - Supervisor → **Registro privacy**: «Vedi come» e credenziali cambiate;
   - Supervisor → Log di sistema; i log del reverse proxy (IP e orari);
   - `docker compose logs app scheduler` per i log dei container;
   - le date dei backup in `./backups` per sapere quali copie contengono i dati.
3. **Valuta il rischio**: quali dati (email? password hash? IP?), quante
   persone, se i dati erano cifrati.
4. **Entro 72 ore** dalla scoperta, se c'è un rischio per le persone, notifica
   il Garante (procedura online su garanteprivacy.it). Se il rischio è alto,
   avvisa anche gli interessati (le email delle squadre sono nella pagina
   Squadre di ogni lega).
5. **Documenta** cosa è successo, cosa hai fatto e quando, anche se non notifichi.

## 8. Log

`settings.LOGGING` scrive in `logs/fantamanager.log` a rotazione: 5 MB per
file, 5 file vecchi più quello attivo (al massimo circa 30 MB). Il software non
scrive nei log password né indirizzi email (le email inviate sono contate,
non elencate); ci sono gli username in accesso e uscita e gli IP delle
richieste rifiutate.
