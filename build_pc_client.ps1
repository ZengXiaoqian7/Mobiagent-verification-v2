$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $RepoRoot

$PyInstallerVersion = python -m PyInstaller --version 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller 未安装。请先运行：python -m pip install pyinstaller"
}
Write-Host "PyInstaller $PyInstallerVersion"

python -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --name MobiAgentVerifierPC `
    --paths $RepoRoot `
    --add-data "$RepoRoot\examples;examples" `
    --add-data "$RepoRoot\msyh.ttf;." `
    --hidden-import runner.mobiagent.mobiagent `
    --exclude-module mem0 `
    --exclude-module matplotlib `
    --exclude-module IPython `
    --exclude-module torch `
    --exclude-module torchvision `
    --exclude-module paddle `
    --exclude-module paddleocr `
    --exclude-module transformers `
    --exclude-module llama_index `
    "$RepoRoot\pc_client_entry.py"

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller 构建失败，退出码：$LASTEXITCODE"
}

Write-Host "构建完成：$RepoRoot\dist\MobiAgentVerifierPC\MobiAgentVerifierPC.exe"
