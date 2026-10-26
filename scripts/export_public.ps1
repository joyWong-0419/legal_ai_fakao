# 按白名单导出公开仓库快照。
#
# 用法：
#   pwsh -NoProfile -File scripts\export_public.ps1 -Target E:\github\legal_ai_fakao
#
# 设计：
# - 只复制白名单内的路径，其余（书源、索引、个人笔记、方法论文档）一律不碰；
# - 单文件超过 50 MB 直接失败——GitHub 单文件上限 100 MB，超了推不上去；
# - 目标目录必须为空，除非显式 -Force；
# - 不复制 .git，导出的是文件快照而不是仓库副本。

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Target,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$targetPath = [System.IO.Path]::GetFullPath($Target)
$maxFileMB = 50

$includeRoots = @('legal_ai', 'law', 'scripts', '.claude', 'test')
$includeFiles = @('README.md', 'CLAUDE.md', '.gitignore', '.gitattributes', '.mcp.json')

# 路径包含这些片段就跳过（书源、派生数据、缓存、构建产物）
$excludePatterns = @(
    'legal_ai\\data\\',
    'legal_ai\\reports\\private\\',
    'legal_ai\\reports\\ocr_full_',
    '__pycache__',
    '\.pyc$',
    '\.egg-info\\',
    '\.pytest_cache\\',
    '\.sqlite$',
    '\.log$',
    '\.pid$'
)

function Test-Excluded([string]$relativePath) {
    foreach ($pattern in $excludePatterns) {
        if ($relativePath -match $pattern) { return $true }
    }
    return $false
}

if ((Test-Path -LiteralPath $targetPath) -and -not $Force) {
    $existing = @(Get-ChildItem -LiteralPath $targetPath -Force -ErrorAction SilentlyContinue)
    if ($existing.Count -gt 0) {
        throw "目标目录非空：$targetPath（确认要覆盖请加 -Force）"
    }
}
if ($Force -and (Test-Path -LiteralPath $targetPath)) {
    Write-Host "清空目标目录：$targetPath"
    Get-ChildItem -LiteralPath $targetPath -Force | Remove-Item -Recurse -Force
}
New-Item -ItemType Directory -Path $targetPath -Force | Out-Null

$copied = New-Object System.Collections.Generic.List[object]
$tooBig = New-Object System.Collections.Generic.List[object]
$skipped = 0

foreach ($entry in $includeRoots) {
    $sourceDir = Join-Path $root $entry
    if (-not (Test-Path -LiteralPath $sourceDir)) { continue }
    $files = Get-ChildItem -LiteralPath $sourceDir -Recurse -File -Force
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($root.Length).TrimStart('\')
        if (Test-Excluded $relative) { $skipped++; continue }
        if ($file.Length -gt $maxFileMB * 1MB) {
            $tooBig.Add([pscustomobject]@{ Path = $relative; MB = [math]::Round($file.Length / 1MB, 1) })
            continue
        }
        $destination = Join-Path $targetPath $relative
        $destinationDir = Split-Path -Parent $destination
        if (-not (Test-Path -LiteralPath $destinationDir)) {
            New-Item -ItemType Directory -Path $destinationDir -Force | Out-Null
        }
        Copy-Item -LiteralPath $file.FullName -Destination $destination -Force
        $copied.Add([pscustomobject]@{ Path = $relative; Bytes = $file.Length })
    }
}

foreach ($entry in $includeFiles) {
    $sourceFile = Join-Path $root $entry
    if (-not (Test-Path -LiteralPath $sourceFile)) { continue }
    Copy-Item -LiteralPath $sourceFile -Destination (Join-Path $targetPath $entry) -Force
    $copied.Add([pscustomobject]@{ Path = $entry; Bytes = (Get-Item -LiteralPath $sourceFile).Length })
}

$totalMB = [math]::Round((($copied | Measure-Object -Property Bytes -Sum).Sum / 1MB), 2)
Write-Host "导出完成：$($copied.Count) 个文件，$totalMB MB"
Write-Host "跳过（派生数据/缓存）：$skipped 个"

$byTop = $copied | Group-Object { ($_.Path -split '\\')[0] } | Sort-Object Name
foreach ($group in $byTop) {
    $groupMB = [math]::Round((($group.Group | Measure-Object -Property Bytes -Sum).Sum / 1MB), 2)
    Write-Host ("  {0,-18} {1,4} 个  {2,7} MB" -f $group.Name, $group.Count, $groupMB)
}

if ($tooBig.Count -gt 0) {
    Write-Host ""
    Write-Host "以下文件超过 $maxFileMB MB，未复制（GitHub 单文件上限 100 MB）：" -ForegroundColor Yellow
    $tooBig | ForEach-Object { Write-Host ("  {0}  ({1} MB)" -f $_.Path, $_.MB) }
    exit 2
}

exit 0
