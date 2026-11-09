[CmdletBinding()]
param([string]$ClusterName = "vo-lab4")

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$out = Join-Path $root "results\lab4_final\commands"
New-Item -ItemType Directory -Force -Path $out | Out-Null
$log = Join-Path $out "delete.log"
if (-not (Get-Command kind -ErrorAction SilentlyContinue)) {
    $packages = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "Microsoft\WinGet\Packages"
    $candidate = Get-ChildItem -LiteralPath $packages -Recurse -File -Filter "kind.exe" `
        -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($candidate) { Set-Alias kind $candidate.FullName -Scope Script }
}
Start-Transcript -LiteralPath $log -Force | Out-Null
try {
    & kind delete cluster --name $ClusterName
    if ($LASTEXITCODE -ne 0) { throw "kind delete завершився з кодом $LASTEXITCODE" }
    if (@(& kind get clusters) -contains $ClusterName) { throw "Кластер не видалено" }
    Write-Host "Кластер $ClusterName видалено; відсутність підтверджено"
}
finally { Stop-Transcript | Out-Null }

