param(
    [int[]]$Rates = @(10, 100, 500),
    [int]$Repeats = 3,
    [int]$WarmupSeconds = 10,
    [int]$DurationSeconds = 30
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ResultsDirectory = Join-Path $ProjectRoot 'results'
$RawDirectory = Join-Path $ResultsDirectory 'raw'
$SeedFile = Join-Path $ResultsDirectory 'seed.json'
$seedCreated = $false

function Assert-LastExitCode {
    param([string]$Message)
    if ($LASTEXITCODE -ne 0) {
        throw $Message
    }
}

function Wait-LabReady {
    param([int]$TimeoutSeconds = 120)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $response = Invoke-RestMethod -Uri 'http://localhost:8080/healthz' -TimeoutSec 2
            if ($response.status -eq 'готовий') {
                return
            }
        }
        catch {
        }
        Start-Sleep -Milliseconds 100
    }
    throw "Стенд не став готовим протягом $TimeoutSeconds секунд."
}

function Get-Median {
    param([double[]]$Values)
    $ordered = @($Values | Sort-Object)
    if ($ordered.Count -eq 0) {
        throw 'Неможливо обчислити медіану порожнього набору.'
    }
    $middle = [math]::Floor($ordered.Count / 2)
    if ($ordered.Count % 2 -eq 1) {
        return [double]$ordered[$middle]
    }
    return ([double]$ordered[$middle - 1] + [double]$ordered[$middle]) / 2
}

function Invoke-Generator {
    param([string[]]$GeneratorArguments)
    & docker compose --profile load run --rm -T --no-deps load-generator @GeneratorArguments
    Assert-LastExitCode 'Контейнер генератора завершився з помилкою.'
}

function Invoke-Analyzer {
    & docker compose --profile load run --rm -T --no-deps --entrypoint python load-generator /load/analyze_results.py --results /results | Out-Null
    Assert-LastExitCode 'Аналіз результатів завершився з помилкою.'
}

function Set-WebScale {
    param([int]$Instances)
    docker compose up -d --scale "web=$Instances"
    Assert-LastExitCode "Не вдалося запустити $Instances реплік web-сервісу."
    docker compose restart proxy
    Assert-LastExitCode 'Не вдалося перезапустити балансувальник.'
    Wait-LabReady
}

Set-Location $ProjectRoot
New-Item -ItemType Directory -Path $RawDirectory -Force | Out-Null

$knownResultFiles = @(
    (Join-Path $ResultsDirectory 'startup_times.json'),
    (Join-Path $ResultsDirectory 'image_size.json'),
    (Join-Path $ResultsDirectory 'network_check.json'),
    (Join-Path $ResultsDirectory 'summary.json'),
    (Join-Path $RawDirectory 'generator-noop.json'),
    (Join-Path $RawDirectory 'closed-loop.json'),
    $SeedFile
)
foreach ($instances in @(1, 2)) {
    foreach ($rate in $Rates) {
        for ($repeat = 1; $repeat -le $Repeats; $repeat++) {
            $knownResultFiles += Join-Path $RawDirectory "open-r$instances-rate$rate-rep$repeat.json"
        }
    }
}
foreach ($path in $knownResultFiles) {
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Force
    }
}

Write-Host 'Збірка образів перед вимірюваннями...'
docker compose --profile load build web load-generator
Assert-LastExitCode 'Не вдалося зібрати образи.'

$composeImages = @(docker compose config --images)
Assert-LastExitCode 'Не вдалося отримати перелік образів Docker Compose.'
$imageName = @($composeImages | Where-Object { $_ -match '(^|-)web$' })[0]
if ([string]::IsNullOrWhiteSpace($imageName)) {
    throw 'Не вдалося визначити актуальний образ web-сервісу.'
}
$imageBytes = [int64](docker image inspect $imageName --format '{{.Size}}')
Assert-LastExitCode 'Не вдалося виміряти розмір образу.'
@{
    bytes = $imageBytes
    mebibytes = [math]::Round($imageBytes / 1MB, 3)
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $ResultsDirectory 'image_size.json') -Encoding utf8

Write-Host 'Три вимірювання часу старту з порожнім томом...'
$startupTimes = @()
for ($attempt = 1; $attempt -le 3; $attempt++) {
    docker compose down -v
    Assert-LastExitCode 'Не вдалося зупинити стенд і видалити тестовий том.'

    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    docker compose up -d --scale web=1
    Assert-LastExitCode 'Не вдалося запустити стенд.'
    Wait-LabReady
    $stopwatch.Stop()
    $elapsed = [math]::Round($stopwatch.Elapsed.TotalMilliseconds, 3)
    $startupTimes += $elapsed
    Write-Host "  Спроба ${attempt}: $elapsed мс"
}
@{
    runs_ms = $startupTimes
    median_ms = [math]::Round((Get-Median $startupTimes), 3)
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $ResultsDirectory 'startup_times.json') -Encoding utf8

Write-Host 'Перевірка внутрішньої мережі PostgreSQL...'
$hostCanConnect = $false
$tcpClient = [System.Net.Sockets.TcpClient]::new()
try {
    $connectTask = $tcpClient.ConnectAsync('127.0.0.1', 5432)
    if ($connectTask.Wait(1000)) {
        $hostCanConnect = $tcpClient.Connected
    }
}
catch {
    $hostCanConnect = $false
}
finally {
    $tcpClient.Dispose()
}
$containerCheck = docker compose exec -T web python -c "import socket; connection=socket.create_connection(('db', 5432), 2); print('CONNECTED'); connection.close()"
$containerCanConnect = $LASTEXITCODE -eq 0 -and ($containerCheck -contains 'CONNECTED')
@{
    host_port_5432_accessible = $hostCanConnect
    web_container_can_connect_to_db = $containerCanConnect
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $ResultsDirectory 'network_check.json') -Encoding utf8

try {
    Write-Host 'Створення 100 тестових записів...'
    Invoke-Generator @('seed', '--count', '100', '--output', '/results/seed.json')
    $seedCreated = $true

    Write-Host 'Окрема перевірка межі генератора на /bench/noop...'
    Invoke-Generator @(
        'open',
        '--path', '/bench/noop',
        '--rate', '500',
        '--duration', '10',
        '--output', '/results/raw/generator-noop.json'
    )

    foreach ($instances in @(1, 2)) {
        Write-Host "Конфігурація: $instances web-реплік."
        Set-WebScale $instances

        foreach ($rate in $Rates) {
            for ($repeat = 1; $repeat -le $Repeats; $repeat++) {
                Write-Host "  $rate запитів/с, повтор $repeat/${Repeats}: прогрів $WarmupSeconds с"
                Invoke-Generator @(
                    'open',
                    '--seed-file', '/results/seed.json',
                    '--rate', [string]$rate,
                    '--duration', [string]$WarmupSeconds
                )

                Write-Host "  $rate запитів/с, повтор $repeat/${Repeats}: вимірювання $DurationSeconds с"
                Invoke-Generator @(
                    'open',
                    '--seed-file', '/results/seed.json',
                    '--rate', [string]$rate,
                    '--duration', [string]$DurationSeconds,
                    '--output', "/results/raw/open-r$instances-rate$rate-rep$repeat.json"
                )
            }
        }
    }

    Invoke-Analyzer
    $selection = Get-Content -Raw (Join-Path $ResultsDirectory 'summary.json') | ConvertFrom-Json
    $closedConcurrency = [int]$selection.closed_test_selection.concurrency
    $closedRate = [int]$selection.closed_test_selection.rate_rps

    Write-Host "Закритий контур: рівень $closedRate запитів/с, паралельність $closedConcurrency."
    Set-WebScale 1
    Invoke-Generator @(
        'closed',
        '--seed-file', '/results/seed.json',
        '--concurrency', [string]$closedConcurrency,
        '--duration', [string]$WarmupSeconds
    )
    Invoke-Generator @(
        'closed',
        '--seed-file', '/results/seed.json',
        '--concurrency', [string]$closedConcurrency,
        '--duration', [string]$DurationSeconds,
        '--output', '/results/raw/closed-loop.json'
    )

    Invoke-Analyzer
}
finally {
    if ($seedCreated -and (Test-Path -LiteralPath $SeedFile)) {
        try {
            Write-Host 'Видалення 100 тестових записів...'
            Invoke-Generator @('cleanup', '--seed-file', '/results/seed.json')
        }
        catch {
            Write-Warning "Не вдалося автоматично прибрати тестові записи: $_"
        }
    }
    if (Test-Path -LiteralPath $SeedFile) {
        Remove-Item -LiteralPath $SeedFile -Force
    }
    Set-WebScale 2
}

Write-Host 'Повна серія вимірювань успішно завершена.'
