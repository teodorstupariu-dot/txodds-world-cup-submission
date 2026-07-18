param(
    [switch]$SkipLegacyBaseline,
    [switch]$SkipExports,
    [int]$TimeoutSeconds = 1800
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot

function Invoke-ProjectGate {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectPath,
        [Parameter(Mandatory = $true)][string]$GateScript,
        [Parameter(Mandatory = $true)][string]$Name,
        [int]$Timeout
    )

    $FullPath = Join-Path $RepoRoot $ProjectPath
    if (-not (Test-Path $FullPath)) {
        throw "Missing project path: $FullPath"
    }

    Write-Host ""
    Write-Host "=== $Name ==="
    Push-Location $FullPath
    try {
        $Python = Join-Path $FullPath ".venv\Scripts\python.exe"
        if (-not (Test-Path $Python)) {
            python -m venv .venv
            if ($LASTEXITCODE -ne 0) { throw "${Name}: virtual environment creation failed" }
        }

        & $Python -m pip install --upgrade pip
        if ($LASTEXITCODE -ne 0) { throw "${Name}: pip upgrade failed" }

        & $Python -m pip install -e ".[dev]"
        if ($LASTEXITCODE -ne 0) { throw "${Name}: dependency installation failed" }

        & $Python $GateScript --timeout $Timeout
        if ($LASTEXITCODE -ne 0) { throw "${Name}: local gate failed" }
    }
    finally {
        Pop-Location
    }
}

Write-Host "GitHub Actions must remain disabled. All validation below runs locally."

if (-not $SkipLegacyBaseline) {
    Invoke-ProjectGate `
        -ProjectPath "worldcup_2026" `
        -GateScript "scripts\local_gate.py" `
        -Name "Legacy integrated baseline" `
        -Timeout $TimeoutSeconds
}

Invoke-ProjectGate `
    -ProjectPath "products\proofguard-agent" `
    -GateScript "scripts\local_gate.py" `
    -Name "ProofGuard Autonomous Agent" `
    -Timeout $TimeoutSeconds

Invoke-ProjectGate `
    -ProjectPath "products\finalitygate-resolver" `
    -GateScript "scripts\local_gate.py" `
    -Name "FinalityGate World Cup Resolver" `
    -Timeout $TimeoutSeconds

if (-not $SkipExports) {
    Write-Host ""
    Write-Host "=== Standalone submission exports ==="
    Push-Location $RepoRoot
    try {
        python scripts\export_submission_projects.py
        if ($LASTEXITCODE -ne 0) { throw "Standalone project export failed" }
    }
    finally {
        Pop-Location
    }
}

Write-Host ""
Write-Host "ALL LOCAL GATES PASSED"
Write-Host "Reports:"
Write-Host "  worldcup_2026\outputs\local_validation_report.json"
Write-Host "  products\proofguard-agent\outputs\local_validation_report.json"
Write-Host "  products\finalitygate-resolver\outputs\local_validation_report.json"
if (-not $SkipExports) {
    Write-Host "Exports:"
    Write-Host "  exports\proofguard-agent-source.zip"
    Write-Host "  exports\finalitygate-resolver-source.zip"
    Write-Host "  exports\EXPORT_REPORT.json"
}
