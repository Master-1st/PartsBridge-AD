$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonw = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'

if (-not (Test-Path -LiteralPath $pythonw)) {
    throw '尚未安装。请先在此目录运行 .\install.ps1。'
}

Start-Process -FilePath $pythonw -ArgumentList '-m', 'lcsc_altium_loader.gui' -WorkingDirectory $projectRoot
