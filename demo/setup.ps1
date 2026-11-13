param([ValidatePattern('^[a-z0-9][a-z0-9-]{0,40}$')][string]$Cluster = 'opsguard-lab', [switch]$WithAlerts)
$ErrorActionPreference = 'Stop'
Push-Location (Split-Path $PSScriptRoot -Parent)
try {
    docker info --format '{{.ServerVersion}}'
    if ($LASTEXITCODE -ne 0) { throw 'Start Docker Desktop in Linux container mode first.' }
    $demoClusters = @(kind get clusters)
    if ($LASTEXITCODE -ne 0) { throw 'kind could not list clusters.' }
    if ($Cluster -notin $demoClusters) {
        kind create cluster --name $Cluster
        if ($LASTEXITCODE -ne 0) { throw 'kind cluster creation failed.' }
    }
    foreach ($demoVersion in @('v1', 'v2')) {
        docker build --provenance=false -t "opsguard-demo:$demoVersion" --build-arg "APP_VERSION=$demoVersion" demo/fixture
        if ($LASTEXITCODE -ne 0) { throw "Image build failed for $demoVersion" }
    }
    kind load docker-image opsguard-demo:v1 opsguard-demo:v2 --name $Cluster
    if ($LASTEXITCODE -ne 0) { throw 'Image loading failed.' }
    if ($WithAlerts) {
        $demoAlertReady = $false
        try { $demoAlertReady = (Invoke-WebRequest http://127.0.0.1:9093/-/ready -TimeoutSec 3).StatusCode -eq 200 } catch {}
        if (-not $demoAlertReady) {
            $demoAlertConfig = Join-Path (Get-Location) 'benchmark/fixtures/selector/alertmanager.yml'
            docker run -d --rm --name opsguard-demo-alertmanager -p 127.0.0.1:9093:9093 --mount "type=bind,source=$demoAlertConfig,target=/etc/alertmanager/alertmanager.yml,readonly" prom/alertmanager:v0.28.1 --config.file=/etc/alertmanager/alertmanager.yml
            if ($LASTEXITCODE -ne 0) { throw 'Alertmanager startup failed. Check whether port 9093 is occupied.' }
        }
    }
    Write-Host "Demo images are loaded into $Cluster. Build demo-ui and start python -m opsguard.demo --cluster $Cluster."
} finally { Pop-Location }
