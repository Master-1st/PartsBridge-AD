$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPath = Join-Path $projectRoot '.venv'
$pythonPath = Join-Path $venvPath 'Scripts\python.exe'

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw '未找到 Windows Python Launcher（py.exe）。请先安装 Python 3.12。'
}

& py -3.12 -c "import sys; assert sys.version_info[:2] == (3, 12)" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw '未找到 Python 3.12。请安装 Python 3.12 后重试。'
}

if (-not (Test-Path -LiteralPath $pythonPath)) {
    & py -3.12 -m venv $venvPath
    if ($LASTEXITCODE -ne 0) {
        throw '创建 Python 3.12 虚拟环境失败。'
    }
}

& $pythonPath -c "import sys; assert sys.version_info[:2] == (3, 12), sys.version"
if ($LASTEXITCODE -ne 0) {
    throw '.venv 不是 Python 3.12 环境。请移走该目录后重新运行 install.ps1。'
}

& $pythonPath -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw '升级 pip 失败。'
}

& $pythonPath -m pip install -e $projectRoot
if ($LASTEXITCODE -ne 0) {
    throw '安装元件库桥失败。'
}

$launcher = Join-Path $venvPath 'Scripts\lcsc-altium.exe'
Write-Host "安装完成：$launcher"
Write-Host "桌面版：.\start-gui.ps1"
Write-Host "示例：& '$launcher' search STM32F103C8T6 --limit 5"
