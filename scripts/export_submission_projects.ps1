$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $RepoRoot
try {
    python scripts\export_submission_projects.py
    if ($LASTEXITCODE -ne 0) {
        throw "Standalone project export failed"
    }
    Write-Host ""
    Write-Host "Standalone source exports created:"
    Write-Host "  exports\proofguard-agent-source.zip"
    Write-Host "  exports\finalitygate-resolver-source.zip"
    Write-Host "  exports\EXPORT_REPORT.json"
}
finally {
    Pop-Location
}
