[CmdletBinding()]
param([string]$ClusterName = "vo-lab3")

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$results = Join-Path $root "results\lab3_final\commands"
New-Item -ItemType Directory -Force -Path $results | Out-Null
$log = Join-Path $results "delete.log"
if (-not (Get-Command kind -ErrorAction SilentlyContinue)) {
    $packages = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "Microsoft\WinGet\Packages"
    $candidate = Get-ChildItem -LiteralPath $packages -Recurse -File -Filter "kind.exe" `
        -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($candidate) { Set-Alias -Name kind -Value $candidate.FullName -Scope Script }
}
if (-not (Get-Command kind -ErrorAction SilentlyContinue)) {
    throw "Required command is not available: kind"
}
Start-Transcript -LiteralPath $log -Force | Out-Null
try {
    & kind delete cluster --name $ClusterName
    if ($LASTEXITCODE -ne 0) {
        throw "kind delete cluster exited with code $LASTEXITCODE"
    }
    $remaining = @(& kind get clusters)
    if ($LASTEXITCODE -ne 0) {
        throw "kind get clusters exited with code $LASTEXITCODE"
    }
    if ($remaining -contains $ClusterName) {
        throw "Cluster $ClusterName is still present after deletion"
    }
    Write-Host "Cluster $ClusterName deleted and absence verified"
}
finally {
    Stop-Transcript | Out-Null
}
