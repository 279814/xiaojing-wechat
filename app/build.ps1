param(
    # Defaults to state\client_venv, the environment `python -m app.main` runs on.
    [string]$Python = ""
)

# Not "Stop": Windows PowerShell 5.1 turns PyInstaller's stderr INFO lines into
# terminating errors once output is redirected. Exit codes are checked instead.
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

if (-not $Python) {
    $clientPy = Join-Path $Root "state\client_venv\Scripts\python.exe"
    if (Test-Path $clientPy) {
        $Python = $clientPy
    } else {
        if (!(Test-Path ".venv")) {
            & python -m venv .venv
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        }
        $Python = Join-Path $Root ".venv\Scripts\python.exe"
        & $Python -m pip install -r ".\app\requirements.txt"
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
}
Write-Host "Build Python: $Python"

& $Python -m pip show -q pyinstaller
if ($LASTEXITCODE -ne 0) {
    & $Python -m pip install "pyinstaller>=6.8,<7"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

& $Python -m PyInstaller ".\app\autosale.spec" --noconfirm --clean
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$dist = Join-Path $Root "dist\Autosale"
$exe = Join-Path $dist "Autosale.exe"
if (!(Test-Path $exe)) {
    Write-Error "PyInstaller finished but $exe is missing"
    exit 1
}

Set-Content -ErrorAction Stop -LiteralPath (Join-Path $dist "Start Autosale.bat") -Encoding ASCII -Value @(
    '@echo off',
    'cd /d %~dp0',
    'start "" "%~dp0Autosale.exe"'
)

Write-Host ""
Write-Host "Self-check: importing every module and reading WeChat contacts (read-only)"
$proc = Start-Process -FilePath $exe -ArgumentList "--self-check" -WorkingDirectory $dist -PassThru
$null = $proc.Handle
if (-not $proc.WaitForExit(180000)) {
    Stop-Process -Id $proc.Id -Force
    Write-Error "Self-check did not finish within 180 seconds"
    exit 1
}
$report = Join-Path $dist "self_check.json"
if (!(Test-Path $report)) {
    Write-Error "Self-check wrote no report (exit code $($proc.ExitCode))"
    exit 1
}
Get-Content -LiteralPath $report -Encoding UTF8
$code = $proc.ExitCode
# The report and log hold this machine's details; keep them out of the folder that gets shipped.
Remove-Item -LiteralPath $report -Force
Get-ChildItem -LiteralPath $dist -Filter "autosale.log*" | Remove-Item -Force
if ($code -ne 0) {
    Write-Error "Self-check failed. See the report above."
    exit 1
}

Write-Host ""
Write-Host "Build completed: dist\Autosale\Autosale.exe"
