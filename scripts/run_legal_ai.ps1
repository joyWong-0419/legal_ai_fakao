[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = "Stop"
$environment = . (Join-Path $PSScriptRoot "env.ps1")
$repoRoot = $environment.project_root
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Project-local .venv was not found. Run scripts\\bootstrap_ocr.ps1 first."
}

Push-Location $repoRoot
try {
    & $venvPython -m legal_ai @Arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
