[CmdletBinding()]
param(
    [string]$ClusterName = "vo-lab3",
    [switch]$KeepExistingCluster
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$results = Join-Path $root "results\lab3_final\commands"
New-Item -ItemType Directory -Force -Path $results | Out-Null
$log = Join-Path $results "deploy.log"

function Invoke-Native {
    param([Parameter(Mandatory)][string]$Program, [Parameter(ValueFromRemainingArguments)][string[]]$Arguments)
    Write-Host ("> " + $Program + " " + ($Arguments -join " "))
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Program exited with code $LASTEXITCODE"
    }
}

function Assert-Command {
    param([Parameter(Mandatory)][string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command is not available: $Name"
    }
}

function Enable-WinGetAlias {
    param([Parameter(Mandatory)][string]$Name)
    if (Get-Command $Name -ErrorAction SilentlyContinue) { return }
    $packages = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "Microsoft\WinGet\Packages"
    if (-not (Test-Path -LiteralPath $packages)) { return }
    $candidate = Get-ChildItem -LiteralPath $packages -Recurse -File -Filter "$Name.exe" `
        -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($candidate) {
        Set-Alias -Name $Name -Value $candidate.FullName -Scope Script
    }
}

Start-Transcript -LiteralPath $log -Force | Out-Null
try {
    Enable-WinGetAlias kind
    Enable-WinGetAlias helm
    foreach ($name in @("docker", "kind", "kubectl", "helm")) {
        Assert-Command $name
    }
    if (-not (Test-Path -LiteralPath (Join-Path $root ".env"))) {
        throw "Missing .env in repository root"
    }

    $existing = @(& kind get clusters 2>$null)
    if ($LASTEXITCODE -ne 0) { throw "kind get clusters failed" }
    if ($existing -contains $ClusterName) {
        if (-not $KeepExistingCluster) {
            throw "Cluster $ClusterName already exists; delete it or pass -KeepExistingCluster"
        }
    } else {
        Invoke-Native kind create cluster --name $ClusterName --config (Join-Path $root "k8s\kind-cluster.yaml")
    }

    Invoke-Native kubectl config use-context "kind-$ClusterName"
    Invoke-Native kubectl label node "$ClusterName-control-plane" role=infra --overwrite
    Invoke-Native kubectl apply -f (Join-Path $root "k8s\namespace.yaml")

    # The generated Secret YAML is passed directly to kubectl and never printed.
    $envFile = Join-Path $root ".env"
    $secretYaml = & kubectl -n lab3 create secret generic postgres-secret `
        "--from-env-file=$envFile" --dry-run=client -o yaml
    if ($LASTEXITCODE -ne 0) { throw "Failed to generate postgres-secret" }
    $secretYaml | & kubectl apply -f - | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Failed to apply postgres-secret" }

    $schemaFile = Join-Path $root "db\init.sql"
    $schemaYaml = & kubectl -n lab3 create configmap db-schema `
        "--from-file=schema.sql=$schemaFile" --dry-run=client -o yaml
    if ($LASTEXITCODE -ne 0) { throw "Failed to generate db-schema ConfigMap" }
    $schemaYaml | & kubectl apply -f - | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Failed to apply db-schema ConfigMap" }

    Invoke-Native kubectl apply -f (Join-Path $root "k8s\postgres.yaml")
    Invoke-Native kubectl -n lab3 rollout status statefulset/postgres --timeout=300s
    Invoke-Native kubectl apply -f (Join-Path $root "k8s\migration-job.yaml")
    Invoke-Native kubectl -n lab3 wait --for=condition=complete job/db-migration --timeout=300s

    Invoke-Native docker build -t vo-web:lab3 $root
    Invoke-Native kind load docker-image vo-web:lab3 --name $ClusterName
    Invoke-Native kubectl apply -f (Join-Path $root "k8s\web.yaml")
    Invoke-Native kubectl -n lab3 rollout status deployment/web --timeout=300s

    Invoke-Native helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/ --force-update
    Invoke-Native helm repo update metrics-server
    Invoke-Native helm upgrade --install metrics-server metrics-server/metrics-server `
        --version 3.14.0 --namespace kube-system `
        --values (Join-Path $root "k8s\metrics-server-values.yaml") --wait --timeout 10m

    # Apply the generated namespace manifest without a temporary file.
    $monitoringNamespace = & kubectl create namespace monitoring --dry-run=client -o yaml
    if ($LASTEXITCODE -ne 0) { throw "Failed to generate monitoring namespace" }
    $monitoringNamespace | & kubectl apply -f - | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Failed to apply monitoring namespace" }

    Invoke-Native helm upgrade --install monitoring `
        oci://ghcr.io/prometheus-community/charts/kube-prometheus-stack `
        --version 92.7.0 --namespace monitoring `
        --values (Join-Path $root "k8s\monitoring-values.yaml") --wait --timeout 15m
    & kubectl -n lab3 delete podmonitor web postgres-exporter --ignore-not-found | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Failed to remove legacy PodMonitor objects" }
    Invoke-Native kubectl apply -f (Join-Path $root "k8s\monitoring-resources.yaml")
    Invoke-Native kubectl -n lab3 rollout status deployment/postgres-exporter --timeout=300s

    $topDeadline = (Get-Date).AddMinutes(5)
    do {
        & kubectl top pods -n lab3 2>$null
        if ($LASTEXITCODE -eq 0) { break }
        Start-Sleep -Seconds 10
    } while ((Get-Date) -lt $topDeadline)
    if ($LASTEXITCODE -ne 0) { throw "kubectl top pods did not become available" }

    Invoke-Native kubectl get nodes --output=wide
    Invoke-Native kubectl get pods --all-namespaces --output=wide
    Invoke-Native kubectl -n lab3 get "statefulset,deployment,service,pvc,job"
    Invoke-Native kubectl -n monitoring get "pods,service"
    Write-Host "Lab 3 cluster is ready: http://localhost:8080 and http://localhost:9090"
}
finally {
    Stop-Transcript | Out-Null
}
