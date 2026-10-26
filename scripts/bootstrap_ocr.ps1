[CmdletBinding()]
param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$environment = . (Join-Path $PSScriptRoot "env.ps1")
$repoRoot = $environment.project_root
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$lockFile = Join-Path $repoRoot "legal_ai\requirements-ocr.lock"

if (-not (Test-Path -LiteralPath $venvPython)) {
    & $Python -m venv (Join-Path $repoRoot ".venv")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

# Installs only into this repository's .venv and .cache\pip. RapidOCR's light
# CPU models are packaged with its wheel; no model files go to a user profile.
& $venvPython -m pip install --cache-dir $env:PIP_CACHE_DIR -r $lockFile
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $venvPython -m pip install --no-deps --no-build-isolation -e (Join-Path $repoRoot "legal_ai")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $venvPython -c "import pymupdf, onnxruntime, sys; from importlib.metadata import version; print({'python': sys.executable, 'pymupdf': pymupdf.__version__, 'rapidocr': version('rapidocr'), 'onnxruntime': onnxruntime.__version__})"
exit $LASTEXITCODE
