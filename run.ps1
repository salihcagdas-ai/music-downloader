param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'Set up Python 3.11+ first: python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt'
}
Push-Location -LiteralPath $PSScriptRoot
try {
    if ($NoBrowser) { & $pythonPath app.py --no-browser }
    else { & $pythonPath app.py }
    $runExitCode = $LASTEXITCODE
} finally { Pop-Location }
exit $runExitCode
