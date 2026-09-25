#!/usr/bin/env bash
#
# uninstall_mac.command — rimozione di FantaManager su macOS.
#
# Il .dmg si installa trascinando l'app in Applicazioni: il trascinamento nel
# Cestino non può eseguire codice, quindi i dati salvati in
# ~/Library/Application Support/FantaManager non verrebbero mai rimossi.
# Questo helper (incluso nel .dmg) rimuove l'app e chiede — come fa
# l'uninstaller di Windows — se eliminare anche i dati salvati.
#
# Doppio clic dal .dmg, oppure: bash uninstall_mac.command
set -euo pipefail

APP="/Applications/FantaManager.app"
DATA="$HOME/Library/Application Support/FantaManager"

# Chiusura di un'eventuale istanza in esecuzione (best-effort).
pkill -f "FantaManager.app" 2>/dev/null || true

if [ -d "$APP" ]; then
    echo "Rimuovo $APP ..."
    rm -rf "$APP"
else
    echo "FantaManager non risulta installato in /Applications (lo salto)."
fi

if [ -d "$DATA" ]; then
    # Prompt grafico Sì/No, equivalente al MsgBox dell'installer Windows.
    answer="$(osascript <<OSA 2>/dev/null || echo "No"
button returned of (display dialog "Vuoi eliminare anche i dati salvati (aste, leghe, immagini)?

$DATA

Scegli \"No\" per conservarli per una futura reinstallazione." buttons {"No", "Sì"} default button "No" with icon caution with title "Disinstalla FantaManager")
OSA
)"
    if [ "$answer" = "Sì" ]; then
        echo "Elimino i dati salvati in $DATA ..."
        rm -rf "$DATA"
    else
        echo "Dati conservati in $DATA"
    fi
else
    echo "Nessun dato salvato da rimuovere."
fi

echo ""
echo "FantaManager rimosso."
