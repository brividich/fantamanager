# FantaManager — Build degli installer (Windows .exe / macOS .dmg)

Questa cartella contiene tutto il necessario per creare un **installer a doppio
clic** che installa FantaManager su un PC nuovo senza dover toccare Python, pip
o riga di comando. Il database è **SQLite integrato** (un singolo file, nessun
server), quindi l'avvio è immediato e l'impatto sulle prestazioni è minimo.

## Cosa produce

| Piattaforma | File finale | Si costruisce su |
|-------------|-------------|------------------|
| Windows     | `dist\FantaManager-Setup.exe` | Windows |
| macOS       | `dist/FantaManager.dmg`        | **un Mac** (obbligatorio) |

> PyInstaller **non** fa cross-compiling: il `.dmg` per Mac va costruito su un
> Mac, l'`.exe` su Windows.

## Come funziona a runtime

- `run_app.py` è l'unico entry point. Avvia lo stack Django + Channels (daphne)
  tramite `runserver`, che serve HTTP, WebSocket, file statici e media in un
  solo processo.
- Al primo avvio applica le migrazioni e crea il database.
- I dati mutabili (DB + loghi caricati) vivono in una cartella utente
  scrivibile, **non** dentro il bundle:
  - Windows: `%APPDATA%\FantaManager\`
  - macOS:   `~/Library/Application Support/FantaManager/`
- Apre automaticamente il browser sulla dashboard e stampa l'indirizzo da usare
  dagli altri dispositivi (telefoni dei partecipanti) sulla stessa rete Wi-Fi.
- Se la porta 8000 è occupata da un altro programma, ne sceglie una libera: usa
  sempre l'indirizzo stampato nella finestra dell'app, non `localhost:8000`.

## Asta con allenatori fuori casa (accesso da internet)

Serve quando i partecipanti **non sono sulla tua Wi-Fi**, ma il PC che ospita
l'asta ha una connessione a internet. Nella regia c'è il pannello **“Accesso da
internet”**: un clic su *Attiva* e l'app ottiene un indirizzo pubblico
`https://….trycloudflare.com` che punta al server locale.

- Nessuna configurazione del router, nessuna porta da aprire, nessun account.
- Al primo utilizzo scarica `cloudflared` (~35 MB) nella cartella dati; dalle
  volte successive è immediato. Il download richiede internet.
- Il WebSocket passa dal tunnel, quindi timer e rilanci restano in tempo reale.
- Mentre è attivo:
  - la **regia** raggiunta dall'indirizzo pubblico chiede un **PIN a 6 cifre**
    mostrato nel pannello sul PC (in rete locale resta senza password);
  - si entra **solo** col proprio link personale: l'ingresso digitando un nome
    è disattivato e il maxischermo richiede il suo token;
  - i link personali in *Squadre & accessi* puntano già all'indirizzo pubblico
    (comodi da inviare, anche come QR);
  - eventuali errori mostrano una pagina generica invece del traceback.
- L'indirizzo cambia a ogni attivazione e smette di funzionare quando chiudi
  l'accesso o l'app. Chiudilo a fine asta.

## Build su Windows

Prerequisiti (una tantum sulla macchina di build, non sul PC finale):
- Python 3.11+ con il progetto e la sua `.venv`.
- [Inno Setup 6](https://jrsoftware.org/isdl.php) (per generare il Setup.exe).

```powershell
# Dalla cartella del progetto:
.\packaging\build_windows.ps1
```

Risultato: `dist\FantaManager-Setup.exe`. L'installer è **per-utente** (nessun
prompt di amministratore): installa in `%LOCALAPPDATA%\Programs\FantaManager`,
crea il collegamento nel menu Start e, se scelto, sul desktop.

## Build su macOS

**Serve un Mac.** PyInstaller non fa cross-compiling: un `.app` contiene binari
Mach-O per la macchina che lo costruisce, quindi da Windows non è possibile
produrlo. Due strade:

### A. Su un Mac

```bash
# Dalla cartella del progetto, su un Mac:
bash packaging/build_mac.sh
```

Lo script rifiuta di partire se non è su macOS, crea il suo ambiente in
`.venv-mac/` (una `.venv` copiata da Windows è inutilizzabile), costruisce il
bundle, **lo avvia per verificare che risponda** e solo dopo crea il `.dmg`.
L'architettura è quella del Mac usato: un Mac Apple Silicon produce un'app
arm64, un Mac Intel produce x86_64 (che gira su entrambi via Rosetta).

### B. Senza un Mac, con GitHub Actions

`.github/workflows/build-mac.yml` esegue la stessa build su un runner macOS
ospitato da GitHub:

1. carica il repository su GitHub;
2. scheda **Actions** → *Build macOS installer* → **Run workflow**
   (parte anche da sola quando crei un tag `v…`);
3. a fine build scarica l'artifact **FantaManager-macOS-dmg**.

Gratuito sui repository pubblici; sui privati consuma minuti Actions (macOS
costa più di Linux). L'app resta **non firmata** in entrambi i casi.

### Fermare l'app su macOS

Un `.app` avviato dal Finder **non apre finestre di terminale**: l'indirizzo per
i telefoni e il comando di arresto stanno dentro la regia. Nel pannello
*Accesso da internet* trovi l'indirizzo “Sullo stesso wifi” e il pulsante
**Chiudi FantaManager**, che è il modo previsto per fermare il server (su
Windows funziona anche chiudendo la finestra nera).

Risultato in entrambi i casi: `dist/FantaManager.dmg`. L'utente lo apre e trascina FantaManager in
*Applicazioni*. L'app **non è firmata**: al primo avvio fare *tasto destro →
Apri* (oppure *Impostazioni di Sistema → Privacy e sicurezza → Apri comunque*).
Per distribuirla senza avvisi serve un Apple Developer ID (firma + notarizzazione).

Il `.dmg` include anche **Disinstalla FantaManager.command**: doppio clic per
rimuovere l'app e — come l'uninstaller di Windows — scegliere via finestra Sì/No
se eliminare anche i dati salvati in `~/Library/Application Support/FantaManager`.

## Test su un PC nuovo

1. Copiare `FantaManager-Setup.exe` (o il `.dmg`) sul PC pulito.
2. Doppio clic → installare → avviare.
3. Si apre il browser sulla dashboard. Sul telefono, sulla stessa Wi-Fi, aprire
   l'indirizzo "Altri dispositivi" mostrato nella finestra dell'app.
4. Per ripartire da zero: chiudere l'app ed eliminare la cartella dati
   (`%APPDATA%\FantaManager` o `~/Library/Application Support/FantaManager`).

## Note

- **Icona**: per personalizzarla, mettere `packaging/icon.ico` (Windows) e
  `packaging/icon.icns` (macOS); lo spec e l'installer la useranno in automatico.
- **PostgreSQL**: non incluso (non serve per uso su un PC). I settings lo
  supportano già: basta impostare `POSTGRES_DB`/`POSTGRES_*` e avere il driver
  `psycopg`, senza modifiche al codice.
- **Antivirus**: i `.exe` PyInstaller possono dare falsi positivi. Per la
  distribuzione pubblica conviene firmare l'eseguibile (code signing).
