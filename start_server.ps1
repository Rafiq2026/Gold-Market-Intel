# GoldBrainAI - WATCHDOG (launched hidden at logon by the Startup .vbs).
# Keeps TWO things alive while the PC is on:
#   1) the local analyzer server on :8008 (dashboard + EA /predict)
#   2) push_quote.py -> sends the live BROKER price + buy/sell flow to the CLOUD
#      dashboard, so the online site matches MetaTrader (falls back to Yahoo when off).
$ErrorActionPreference = 'SilentlyContinue'
$py    = 'C:\Python314\pythonw.exe'
$dir   = 'C:\Users\ziaal\Meta Trader 5\GoldBrainAI\python'
$cloud = 'https://gold-market-intel.onrender.com'

while ($true) {
    # 1) local server up?
    $up = $false
    try {
        $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8008/health' -TimeoutSec 4 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $up = $true }
    } catch { }
    if (-not $up) {
        Start-Process -FilePath $py -ArgumentList 'ai_server.py' -WorkingDirectory $dir -WindowStyle Hidden
    }

    # 2) cloud price/flow pusher running?
    $push = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
            Where-Object { $_.CommandLine -like '*push_quote.py*' }
    if (-not $push) {
        Start-Process -FilePath $py -ArgumentList 'push_quote.py', $cloud -WorkingDirectory $dir -WindowStyle Hidden
    }

    Start-Sleep -Seconds 120
}
