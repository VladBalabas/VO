[CmdletBinding()]
param([string]$ClusterName = "vo-lab4", [switch]$KeepExistingCluster)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$out = Join-Path $root "results\lab4_final\commands"
New-Item -ItemType Directory -Force -Path $out | Out-Null
$log = Join-Path $out "deploy.log"

function Enable-WinGetAlias([string]$Name) {
    if (Get-Command $Name -ErrorAction SilentlyContinue) { return }
    $packages = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "Microsoft\WinGet\Packages"
    $candidate = Get-ChildItem -LiteralPath $packages -Recurse -File -Filter "$Name.exe" `
        -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($candidate) { Set-Alias -Name $Name -Value $candidate.FullName -Scope Script }
}
function Invoke-Native([string]$Program, [Parameter(ValueFromRemainingArguments)][string[]]$Arguments) {
    Write-Host ("> " + $Program + " " + ($Arguments -join " "))
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program завершився з кодом $LASTEXITCODE" }
}

Start-Transcript -LiteralPath $log -Force | Out-Null
try {
    Enable-WinGetAlias kind
    Enable-WinGetAlias helm
    foreach ($name in @("docker", "kind", "kubectl", "helm")) {
        if (-not (Get-Command $name -ErrorAction SilentlyContinue)) { throw "Не знайдено $name" }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $root ".env"))) { throw "Немає .env" }

    $clusters = @(& kind get clusters 2>$null)
    if ($clusters -contains $ClusterName) {
        if (-not $KeepExistingCluster) { throw "Кластер $ClusterName вже існує" }
    } else {
        Invoke-Native kind create cluster --name $ClusterName --config (Join-Path $root "k8s\kind-cluster.yaml")
    }
    Invoke-Native kubectl config use-context "kind-$ClusterName"
    Invoke-Native kubectl label node "$ClusterName-control-plane" role=infra --overwrite
    Invoke-Native kubectl apply -f (Join-Path $root "k8s\lab4\namespace.yaml")

    $secret = & kubectl -n lab4 create secret generic postgres-secret `
        "--from-env-file=$(Join-Path $root '.env')" --dry-run=client -o yaml
    if ($LASTEXITCODE -ne 0) { throw "Не вдалося сформувати Secret" }
    $secret | & kubectl apply -f - | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Не вдалося застосувати Secret" }

    $schema = & kubectl -n lab4 create configmap db-schema `
        "--from-file=schema.sql=$(Join-Path $root 'db\init.sql')" --dry-run=client -o yaml
    if ($LASTEXITCODE -ne 0) { throw "Не вдалося сформувати ConfigMap схеми" }
    $schema | & kubectl apply -f - | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Не вдалося застосувати ConfigMap схеми" }

    Invoke-Native kubectl apply -f (Join-Path $root "k8s\lab4\postgres.yaml")
    Invoke-Native kubectl -n lab4 rollout status statefulset/postgres --timeout=300s
    Invoke-Native kubectl -n lab4 delete job db-migration --ignore-not-found
    Invoke-Native kubectl apply -f (Join-Path $root "k8s\lab4\migration-job.yaml")
    Invoke-Native kubectl -n lab4 wait --for=condition=complete job/db-migration --timeout=300s

    Invoke-Native docker build -t vo-web:lab4 -f (Join-Path $root "Dockerfile") $root
    Invoke-Native docker build -t vo-worker:lab4 -f (Join-Path $root "Dockerfile.worker") $root
    Invoke-Native kind load docker-image vo-web:lab4 vo-worker:lab4 --name $ClusterName
    Invoke-Native kubectl apply -f (Join-Path $root "k8s\lab4\web.yaml")
    Invoke-Native kubectl apply -f (Join-Path $root "k8s\lab4\worker.yaml")
    Invoke-Native kubectl -n lab4 rollout status deployment/web --timeout=300s
    Invoke-Native kubectl -n lab4 rollout status deployment/worker --timeout=300s

    Invoke-Native helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/ --force-update
    Invoke-Native helm repo update metrics-server
    Invoke-Native helm upgrade --install metrics-server metrics-server/metrics-server --version 3.14.0 `
        --namespace kube-system --values (Join-Path $root "k8s\metrics-server-values.yaml") `
        --wait --timeout 10m
    $monitoringNamespace = & kubectl create namespace monitoring --dry-run=client -o yaml
    $monitoringNamespace | & kubectl apply -f - | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Не вдалося створити namespace monitoring" }
    Invoke-Native helm upgrade --install monitoring `
        oci://ghcr.io/prometheus-community/charts/kube-prometheus-stack --version 92.7.0 `
        --namespace monitoring --values (Join-Path $root "k8s\monitoring-values.yaml") `
        --wait --timeout 15m
    Invoke-Native kubectl apply -f (Join-Path $root "k8s\lab4\monitoring-resources.yaml")
    Invoke-Native kubectl -n lab4 rollout status deployment/postgres-exporter --timeout=300s

    Invoke-Native kubectl get nodes --output=wide
    Invoke-Native kubectl -n lab4 get pods --output=wide
    Invoke-Native kubectl -n lab4 get "statefulset,deployments,services,pvc,jobs"
    $infraEvidence = [ordered]@{
        at = [DateTimeOffset]::UtcNow.ToString('o')
        commands = @(
            'kubectl get nodes --output=wide'
            'kubectl -n lab4 get pods --output=wide'
            'kubectl -n lab4 get statefulset,deployments,services,pvc,jobs'
        )
        nodes = (& kubectl get nodes --output=wide | Out-String)
        pods = (& kubectl -n lab4 get pods --output=wide | Out-String)
        resources = (& kubectl -n lab4 get "statefulset,deployments,services,pvc,jobs" | Out-String)
    }
    $infraEvidence | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath `
        (Join-Path $root 'results\lab4_final\infra_check.json') -Encoding utf8
    Write-Host "ЛР4 готова: http://localhost:8080, Prometheus http://localhost:9090"
}
finally { Stop-Transcript | Out-Null }

