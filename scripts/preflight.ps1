$ErrorActionPreference = 'Stop'
& python (Join-Path $PSScriptRoot 'preflight.py') @args
exit $LASTEXITCODE
