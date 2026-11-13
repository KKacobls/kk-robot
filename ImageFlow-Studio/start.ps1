$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "第一次執行：建立 Python 虛擬環境..." -ForegroundColor Cyan
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv .venv
    } else {
        & python -m venv .venv
    }
    if ($LASTEXITCODE -ne 0) { throw "建立虛擬環境失敗，請安裝 Python 3.10 以上" }
}
Write-Host "檢查 Python 套件..." -ForegroundColor Cyan
& $python -m pip install -r requirements.txt --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { throw "套件安裝失敗" }
& $python run.py
