@echo off
REM ============================================================
REM  Live Auction Server - avvio rapido
REM  1) Libera la porta 8000 coi privilegi correnti (chiude anche
REM     i reloader Django uccidendo il processo padre).
REM  2) Se la porta resta occupata (processo protetto/elevato),
REM     richiede privilegi admin via UAC e riprova.
REM  3) Avvia il server ASGI/Channels sulla LAN col venv.
REM ============================================================
setlocal
cd /d "%~dp0"

set PORT=8000

echo.
echo [1/3] Libero la porta %PORT% ...
call :free_port

call :port_busy
if "%PORTBUSY%"=="1" (
    if /i "%~1"=="ELEVATED" (
        echo.
        echo  ATTENZIONE: porta %PORT% ancora occupata anche da amministratore.
        echo  Chiudi manualmente il processo che la usa, poi riprova.
        echo.
    ) else (
        echo.
        echo [2/3] Porta occupata da un processo protetto: richiedo privilegi admin...
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList 'ELEVATED' -Verb RunAs"
        exit /b
    )
)

REM Seleziona l'interprete: usa il virtualenv del progetto se presente.
set PY=.venv\Scripts\python.exe
if not exist "%PY%" (
    echo.
    echo  ATTENZIONE: virtualenv .venv non trovato, uso il python di sistema.
    echo  Se manca 'daphne':  python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    set PY=python
)

echo.
echo [3/3] Avvio del server su http://0.0.0.0:%PORT% ...
echo     (Ctrl+C per fermare. Trova l'IP del PC con: ipconfig)
echo.
"%PY%" manage.py runserver 0.0.0.0:%PORT%

endlocal
exit /b

REM ------------------------------------------------------------
REM  Subroutine: chiude i processi in ascolto sulla porta.
REM  Per i server con autoreloader uccide il processo PADRE
REM  (cosi' il figlio non viene rigenerato).
REM ------------------------------------------------------------
:free_port
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$port=%PORT%;" ^
  "$conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue;" ^
  "if(-not $conns){ Write-Host '    nessun processo in ascolto sulla porta '$port; }" ^
  "foreach($c in $conns){" ^
  "  $procId=$c.OwningProcess;" ^
  "  $parent=(Get-CimInstance Win32_Process -Filter ('ProcessId='+$procId) -ErrorAction SilentlyContinue).ParentProcessId;" ^
  "  $pname=(Get-Process -Id $parent -ErrorAction SilentlyContinue).ProcessName;" ^
  "  if($pname -match 'python|daphne'){" ^
  "    Write-Host ('    chiudo reloader PID '+$parent+' (e figlio '+$procId+')');" ^
  "    taskkill /F /T /PID $parent ^>$null 2^>^&1;" ^
  "  } else {" ^
  "    Write-Host ('    chiudo PID '+$procId);" ^
  "    taskkill /F /T /PID $procId ^>$null 2^>^&1;" ^
  "  }" ^
  "}"
timeout /t 1 /nobreak >nul
goto :eof

REM ------------------------------------------------------------
REM  Subroutine: PORTBUSY=1 se la porta e' ancora in LISTENING.
REM ------------------------------------------------------------
:port_busy
set PORTBUSY=0
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }"
if errorlevel 1 set PORTBUSY=1
goto :eof
