param(
    [int]$Count = 10,
    [string]$Config = '.azure\demo-config.json',
    [switch]$Yes
)
$ErrorActionPreference = 'Stop'
$arguments = @('azure-demo', '--count', "$Count", '--config', $Config)
if ($Yes) { $arguments += '--yes' }
& llm-batch @arguments
exit $LASTEXITCODE
