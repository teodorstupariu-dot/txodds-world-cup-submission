param(
    [string]$Report = "outputs/local_validation_report.json",
    [int]$TimeoutSeconds = 900
)

$ErrorActionPreference = "Stop"
python scripts/local_gate.py --report $Report --timeout $TimeoutSeconds
exit $LASTEXITCODE
