$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$source = Join-Path $root 'src\llm_batch_starter'
$target = Join-Path $root 'services\orchestrator\llm_batch_starter'
if (Test-Path -LiteralPath $target) {
    Remove-Item -LiteralPath $target -Recurse -Force
}
Copy-Item -LiteralPath $source -Destination $target -Recurse
& python (Join-Path $PSScriptRoot 'verify-shared-package.py')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host 'Packaged the shared application into the Functions deployment directory.'
