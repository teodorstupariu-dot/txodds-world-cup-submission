param(
    [string]$Links = "submission_links.json"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $RepoRoot
try {
    python scripts\finalize_submission_packets.py --links $Links
    if ($LASTEXITCODE -ne 0) {
        throw "Final submission packet generation failed"
    }
    Write-Host ""
    Write-Host "Final submission packets created:"
    Write-Host "  exports\proofguard-agent-SUBMISSION_FINAL.md"
    Write-Host "  exports\finalitygate-resolver-SUBMISSION_FINAL.md"
    Write-Host "  exports\FINAL_SUBMISSION_REPORT.json"
}
finally {
    Pop-Location
}
