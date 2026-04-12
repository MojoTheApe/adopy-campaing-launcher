# Полный отчёт агента анализа → стабильный Markdown в output/latest_analytics.md
# Запуск из корня fb_agents:  .\run_quick_analytics.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$out = Join-Path $PSScriptRoot "output\latest_analytics.md"
New-Item -ItemType Directory -Force -Path (Split-Path $out) | Out-Null
python run.py --country all --top 2 --output $out
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "Report written to: $out"
