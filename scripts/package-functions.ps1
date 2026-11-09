$ErrorActionPreference = 'Stop'
& python (Join-Path $PSScriptRoot 'package-functions.py')
exit $LASTEXITCODE
