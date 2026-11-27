# One command for Vikas's remaining laptop steps (S3-P4 validation, S4-P4 judge runs, push).
# Run from the OPS-Guard folder in PowerShell, with Docker Desktop running:
#   powershell -ExecutionPolicy Bypass -File scripts\run_vikas_s3p4.ps1
# Options: -SkipValidation  -SkipTasks  -SkipJudges  -SkipPush  -JudgeModel <groq model id>
param(
    [switch]$SkipValidation, [switch]$SkipTasks, [switch]$SkipJudges, [switch]$SkipPush,
    [string]$Cluster = "opsguard-lab",
    [string]$Image = "python:3.13-slim",
    [string]$JudgeModel = "openai/gpt-oss-20b",
    [ValidateRange(0, 300)][int]$JudgeTaskDelaySeconds = 60
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
$env:PYTHONUNBUFFERED = "1"
$Branch = "feat/s3-p4-effect-model"
$Py = ".\.venv\Scripts\python.exe"
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Log = "results\vikas-run-$Stamp.log"
New-Item -ItemType Directory -Force -Path results | Out-Null
function Step($text) {
    Write-Host "`n=== $text ===" -ForegroundColor Cyan
    Add-Content -LiteralPath $Log -Value "=== $text ===" -Encoding UTF8
}
function Run($exe, [string[]]$arguments) {
    $previousPreference = $ErrorActionPreference
    try {
        # Windows PowerShell wraps native stderr as error records, including progress.
        $ErrorActionPreference = "Continue"
        & $exe @arguments 2>&1 | ForEach-Object {
            $line = $_.ToString()
            Add-Content -LiteralPath $Log -Value $line -Encoding UTF8
            Write-Output $line
        }
        $commandExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($commandExitCode -ne 0) { throw "failed: $exe $($arguments -join ' ')" }
}

Step "Checking the branch and tools"
if (-not (Test-Path ".git")) { throw "Run this from the OPS-Guard folder." }
git checkout $Branch
if ($LASTEXITCODE -ne 0) { throw "Could not switch to $Branch. Commit or stash your changes first." }
if (-not (Test-Path $Py)) {
    Step "Creating the Python environment"
    py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Install Python 3.12 or create .venv with Python 3.10 or newer." }
}
Run $Py @("-m", "pip", "install", "-q", "-e", ".[test,report,groq]")
Run $Py @("-m", "pytest", "-q", "tests/impact", "tests/bench/test_impact_tasks.py", "tests/bench/test_impact_analysis.py")

if (-not ($SkipValidation -and $SkipTasks)) {
    foreach ($tool in "docker", "kind", "kubectl") {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "$tool is not on PATH. Install it, then rerun." }
    }
    docker info *> $null
    if ($LASTEXITCODE -ne 0) { throw "Docker Desktop is not running. Start it, wait until it says Running, then rerun." }
    if (-not ((kind get clusters) -contains $Cluster)) {
        Step "Creating the kind cluster $Cluster (a few minutes)"
        Run "kind" @("create", "cluster", "--name", $Cluster)
    }
    Step "Loading $Image into the cluster"
    Run "docker" @("pull", $Image)
    $imageArchive = "results/fixture-linux-amd64.tar"
    # Export one platform so Docker's multi-platform index has no missing manifests.
    Run "docker" @("image", "save", "--platform", "linux/amd64", "--output", $imageArchive, $Image)
    Run "kind" @("load", "image-archive", $imageArchive, "--name", $Cluster)
}

if (-not $SkipValidation) {
    Step "S3-P4: effect-model validation on kind (about 45-60 minutes)"
    Run $Py @("-m", "opsguard.impact.validate", "run", "--fixture", "checkout-payments", "--kind-cluster", $Cluster,
              "--image", $Image, "--repeats", "2", "--output", "results/effect-validation/checkout-payments")
}

if (-not $SkipTasks) {
    Step "S3-P4: task validation on kind (45 tasks, about 60-90 minutes)"
    Run $Py @("-m", "opsguard.bench.impact_tasks", "validate-kind", "--kind-cluster", $Cluster, "--image", $Image,
              "--output", "results/impact-tasks-validation")
}

if (-not $SkipJudges) {
    $hasKey = $env:GROQ_API_KEY -or ((Test-Path ".env") -and (Select-String -Path ".env" -Pattern "^GROQ_API_KEY=" -Quiet))
    if (-not $hasKey) {
        throw "Put GROQ_API_KEY in the environment or ignored .env file before running the judges."
    } else {
        Step "S4-P4: B9 judge and B10 self-check on the development tasks"
        $developmentTaskCode = @'
from opsguard.bench.impact_tasks import load_tasks
from opsguard.bench.convert import split_for
for task in load_tasks():
    if split_for(task["family"]) == "dev":
        print(task["id"])
'@
        $developmentTasks = @($developmentTaskCode | & $Py -)
        if ($LASTEXITCODE -ne 0 -or $developmentTasks.Count -eq 0) { throw "Could not select development tasks." }
        $judgeBatchRoot = "results/judges-dev/tasks-$Stamp"
        foreach ($task in $developmentTasks) {
            Step "Reviewing development task $task"
            Run $Py @("-m", "opsguard.bench.impact_tasks", "run", "--backend", "memory", "--condition", "b9-judge",
                      "--condition", "b10-self-check", "--judge-model", $JudgeModel,
                      "--task", $task, "--output", "$judgeBatchRoot/$task")
            if ($task -ne $developmentTasks[-1]) { Start-Sleep -Seconds $JudgeTaskDelaySeconds }
        }
        $combineJudgeCode = @'
import json
import sys
from pathlib import Path
from opsguard.bench.impact_tasks import summarize

batch, model = Path(sys.argv[1]), sys.argv[2]
rows = []
for task in sys.argv[3:]:
    source = batch / task / "results.jsonl"
    rows.extend(json.loads(line) for line in source.read_text().splitlines() if line)
for row in rows:
    row.update(provider="groq", model_id=model, proposal_source="scripted", split="dev")
root = batch.parent
(root / "results.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
(root / "summary.json").write_text(json.dumps(summarize(rows), indent=2) + "\n", encoding="utf-8")
(root / "manifest.json").write_text(json.dumps({"provider": "groq", "judge_model_id": model,
    "proposal_source": "scripted", "split": "dev", "tasks": sys.argv[3:], "batch": str(batch)}, indent=2),
    encoding="utf-8")
if len(rows) != 4 * len(sys.argv[3:]) or any(row.get("error") for row in rows):
    raise SystemExit("Judge run has missing records or provider errors; inspect the saved results.")
'@
        $combineJudgeCode | & $Py - $judgeBatchRoot $JudgeModel @developmentTasks
        if ($LASTEXITCODE -ne 0) { throw "Judge results need review before pushing." }
    }
}

if (-not $SkipPush) {
    if (-not $SkipValidation) {
        $effectScores = Get-Content "results/effect-validation/checkout-payments/scores.json" -Raw | ConvertFrom-Json
        if ($effectScores.summary.harness_errors -ne 0 -or $effectScores.summary.static_recall -lt 0.95) {
            throw "Effect validation has errors or recall below 0.95; inspect scores.json before pushing."
        }
    }
    if (-not $SkipTasks) {
        $taskScores = Get-Content "results/impact-tasks-validation/summary.json" -Raw | ConvertFrom-Json
        if ($taskScores.tasks -ne 45 -or $taskScores.harness_errors -ne 0 -or $taskScores.failed.Count -ne 0) {
            throw "Task validation is incomplete; inspect summary.json before pushing."
        }
    }
    if (-not $SkipJudges) {
        $judgeRows = @(Get-Content "results/judges-dev/results.jsonl" | ForEach-Object { $_ | ConvertFrom-Json })
        if ($judgeRows.Count -ne 4 * $developmentTasks.Count -or @($judgeRows | Where-Object { $_.error }).Count -ne 0) {
            throw "Judge run has missing records or provider errors; inspect results.jsonl before pushing."
        }
    }
    Step "Pushing $Branch to GitHub"
    $tracked = git ls-files | Select-String -Pattern "(^|/)(\.env|env\.env|.*\.kubeconfig)$"
    if ($tracked) { throw "A secrets file is tracked by git ($tracked). Remove it from git before pushing." }
    Run "git" @("push", "-u", "origin", $Branch)
    Write-Host "Open the pull request: https://github.com/rohans-19/OPS-Guard/compare/main...$($Branch)?expand=1" -ForegroundColor Green
}

Step "Done"
Write-Host "Result files:" -ForegroundColor Green
Write-Host "  results\effect-validation\checkout-payments\scores.json"
Write-Host "  results\impact-tasks-validation\summary.json"
Write-Host "  results\judges-dev\summary.json"
Write-Host "Full log: $Log"
