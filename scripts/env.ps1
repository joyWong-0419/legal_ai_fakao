[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$projectCache = Join-Path $projectRoot ".cache"

foreach ($directory in @(
    (Join-Path $projectCache "pip"),
    (Join-Path $projectCache "pycache"),
    (Join-Path $projectCache "rapidocr"),
    (Join-Path $projectCache "tmp"),
    (Join-Path $projectRoot "vendor\models")
)) {
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
}

# These variables constrain every project command without changing user- or
# system-level PATH/configuration. RapidOCR 3.9.2 ships its small CPU models
# inside the project .venv, so this pilot performs no model download.
$env:PIP_CACHE_DIR = Join-Path $projectCache "pip"
$env:PYTHONPYCACHEPREFIX = Join-Path $projectCache "pycache"
$env:PYTHONUTF8 = "1"
$env:RAPIDOCR_CACHE_DIR = Join-Path $projectCache "rapidocr"
$env:TEMP = Join-Path $projectCache "tmp"
$env:TMP = $env:TEMP

[pscustomobject]@{
    project_root = $projectRoot
    pip_cache = $env:PIP_CACHE_DIR
    rapidocr_cache = $env:RAPIDOCR_CACHE_DIR
    model_directory = Join-Path $projectRoot "vendor\models"
    temporary_directory = $env:TEMP
}
