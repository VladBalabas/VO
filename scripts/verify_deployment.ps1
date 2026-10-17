param(
    [string]$ProjectName = "vo-lr1-check-$PID"
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ResultsDirectory = Join-Path $ProjectRoot 'results'
$journal = [System.Collections.Generic.List[object]]::new()
$previousProject = $env:COMPOSE_PROJECT_NAME
$started = $false

function Invoke-Compose {
    param([string[]]$Arguments)
    $quotedArguments = $Arguments | ForEach-Object {
        if ($_ -match '\s') { '"' + $_.Replace('"', '`"') + '"' } else { $_ }
    }
    $command = "docker compose -p $ProjectName " + ($quotedArguments -join ' ')
    Write-Host $command
    $output = @(& docker compose -p $ProjectName @Arguments 2>&1 | ForEach-Object { "$_" })
    $exitCode = $LASTEXITCODE
    $journal.Add(@{ command = $command; exit_code = $exitCode; output = $output })
    $output | ForEach-Object { Write-Host $_ }
    if ($exitCode -ne 0) { throw "Команда завершилась із кодом ${exitCode}: $command" }
    return $output
}

function Wait-Ready {
    $deadline = [DateTime]::UtcNow.AddSeconds(120)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $response = Invoke-RestMethod 'http://127.0.0.1:8080/healthz' -TimeoutSec 2
            if ($response.status -eq 'готовий') { return }
        }
        catch { }
        Start-Sleep -Milliseconds 200
    }
    throw 'Стенд не став готовим протягом 120 секунд.'
}

function Invoke-Api {
    param([string]$Method, [string]$Path, [object]$Payload = $null)
    $parameters = @{
        Method = $Method
        Uri = "http://127.0.0.1:8080$Path"
        TimeoutSec = 15
        SkipHttpErrorCheck = $true
    }
    $command = "Invoke-WebRequest -Method $Method -Uri '$($parameters.Uri)'"
    if ($null -ne $Payload) {
        $json = $Payload | ConvertTo-Json -Compress
        $parameters.ContentType = 'application/json; charset=utf-8'
        $parameters.Body = [System.Text.Encoding]::UTF8.GetBytes($json)
        $command += " -ContentType 'application/json; charset=utf-8' -Body '$json'"
    }
    $response = Invoke-WebRequest @parameters
    $body = if ($response.Content) { $response.Content | ConvertFrom-Json } else { $null }
    $entry = @{ command = $command; status = [int]$response.StatusCode; body = $body }
    $journal.Add($entry)
    Write-Host $command
    Write-Host "HTTP $($entry.status)"
    Write-Host ($body | ConvertTo-Json -Depth 12)
    return $entry
}

function Assert-Condition {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

Set-Location $ProjectRoot
try {
    Assert-Condition ($ProjectName -match '^vo-lr1-check-[a-zA-Z0-9-]+$') "Для перевірки потрібне окреме ім’я vo-lr1-check-*."
    $existing = @(Invoke-Compose @('ps', '--all', '--quiet')) | Where-Object { $_ -match '^[0-9a-f]{12,64}$' }
    Assert-Condition ($existing.Count -eq 0) "Проєкт уже має контейнери. Виберіть нове ім’я для ізольованої перевірки."
    $volumes = @(& docker volume ls --quiet --filter "label=com.docker.compose.project=$ProjectName")
    Assert-Condition ($LASTEXITCODE -eq 0 -and $volumes.Count -eq 0) "Проєкт уже має томи. Виберіть нове ім’я."
    New-Item -ItemType Directory -Path $ResultsDirectory -Force | Out-Null
    $env:COMPOSE_PROJECT_NAME = $ProjectName
    $started = $true
    Invoke-Compose @('up', '--build', '-d', '--scale', 'web=1') | Out-Null
    Wait-Ready
    Invoke-Compose @('--profile', 'test', 'run', '--rm', '--no-deps', 'api-test') | Out-Null
    $emptyBefore = Invoke-Api 'GET' '/referrals?limit=20&offset=0'
    Assert-Condition ($emptyBefore.status -eq 200 -and $emptyBefore.body.total -eq 0) 'Після CRUD-перевірки база має бути порожньою.'
    $payload = @{
        patient_code = 'VOLUME-CHECK'
        modality = 'MRI'
        priority = 2
        doctor = 'Перевірка збереження даних'
        issued_at = [DateTime]::Now.ToString('yyyy-MM-dd')
        status = 'created'
    }
    $created = Invoke-Api 'POST' '/referrals' $payload
    Assert-Condition ($created.status -eq 201) 'Не вдалося створити контрольний запис.'
    $referralId = $created.body.id
    Invoke-Compose @('down') | Out-Null
    Invoke-Compose @('up', '-d', '--scale', 'web=1') | Out-Null
    Wait-Ready
    $preserved = Invoke-Api 'GET' "/referrals/$referralId"
    Assert-Condition ($preserved.status -eq 200 -and $preserved.body.patient_code -eq 'VOLUME-CHECK') 'Контрольний запис не зберігся після down/up.'
    Invoke-Compose @('down', '-v') | Out-Null
    Invoke-Compose @('up', '-d', '--scale', 'web=1') | Out-Null
    Wait-Ready
    $emptyAfter = Invoke-Api 'GET' '/referrals?limit=20&offset=0'
    Assert-Condition ($emptyAfter.status -eq 200 -and $emptyAfter.body.total -eq 0 -and $emptyAfter.body.items.Count -eq 0) 'Після down -v/up база не порожня.'

    $hostCommand = 'Test-NetConnection -ComputerName 127.0.0.1 -Port 5432 -InformationLevel Detailed'
    Write-Host $hostCommand
    $hostCheck = Test-NetConnection -ComputerName 127.0.0.1 -Port 5432 -InformationLevel Detailed -WarningAction SilentlyContinue
    $hostOutput = ($hostCheck | Select-Object ComputerName, RemoteAddress, RemotePort, TcpTestSucceeded | Format-List | Out-String).Trim()
    Write-Host $hostOutput
    $journal.Add(@{ command = $hostCommand; output = $hostOutput; tcp_test_succeeded = $hostCheck.TcpTestSucceeded })
    Assert-Condition (-not $hostCheck.TcpTestSucceeded) 'На хості доступний порт 5432: потрібна окрема перевірка конфлікту з локальним PostgreSQL.'
    $containerCommand = "import socket; connection=socket.create_connection(('db', 5432), 2); print('db:5432 CONNECTED'); connection.close()"
    $containerOutput = @(Invoke-Compose @('exec', '-T', 'web', 'python', '-c', $containerCommand))
    Assert-Condition ($containerOutput -contains 'db:5432 CONNECTED') 'Підключення web до бази не підтверджене.'
    $psOutput = @(Invoke-Compose @('ps'))
    @{
        generated_at = [DateTime]::UtcNow.ToString('o')
        project_name = $ProjectName
        host_command = $hostCommand
        host_output = $hostOutput
        host_port_5432_accessible = [bool]$hostCheck.TcpTestSucceeded
        container_command = "docker compose -p $ProjectName exec -T web python -c `"$containerCommand`""
        container_output = $containerOutput
        web_container_can_connect_to_db = $true
        compose_ps = $psOutput
    } | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath (Join-Path $ResultsDirectory 'network_check.json') -Encoding utf8
}
finally {
    try {
        if ($started) {
            try { Invoke-Compose @('down', '-v') | Out-Null }
            finally {
                @{
                    generated_at = [DateTime]::UtcNow.ToString('o')
                    project_name = $ProjectName
                    steps = $journal
                } | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath (Join-Path $ResultsDirectory 'deployment_check.json') -Encoding utf8
            }
        }
    }
    finally { $env:COMPOSE_PROJECT_NAME = $previousProject }
}

Write-Host 'CRUD, збереження тому, очищення та мережева ізоляція підтверджені.'
