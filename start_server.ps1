# GoldBrainAI analyzer server - WATCHDOG.
# Launched hidden at Windows logon (via the .vbs in the Startup folder).
# Loops forever: every 2 minutes it checks that the server answers on :8008,
# and (re)starts it windowless if it is down. Keeps the dashboard from ever
# going "mute" again - covers both startup and a mid-session crash.
$ErrorActionPreference = 'SilentlyContinue'
$py  = 'C:\Python314\pythonw.exe'
$dir = 'C:\Users\ziaal\Meta Trader 5\GoldBrainAI\python'

while ($true) {
    $up = $false
    try {
        $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8008/health' -TimeoutSec 4 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $up = $true }
    } catch { }

    if (-not $up) {
        Start-Process -FilePath $py -ArgumentList 'ai_server.py' -WorkingDirectory $dir -WindowStyle Hidden
    }

    Start-Sleep -Seconds 120
}
