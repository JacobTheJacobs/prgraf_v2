# Install prgraf PR blast-radius template into a target git repo.
# Usage:
#   .\install-template.ps1 -TargetRepo "C:\path\to\your\repo"

param(
    [Parameter(Mandatory = $true)]
    [string]$TargetRepo
)

$ErrorActionPreference = "Stop"
$Source = $PSScriptRoot
$Target = Resolve-Path $TargetRepo

if (-not (Test-Path (Join-Path $Target ".git"))) {
    throw "Target is not a git repo: $Target"
}

$WorkflowDir = Join-Path $Target ".github\workflows"
$PackageDir = Join-Path $Target "tools\prgraf"

New-Item -ItemType Directory -Force -Path $WorkflowDir | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path $PackageDir) | Out-Null

Copy-Item (Join-Path $Source "template\.github\workflows\pr-blast-radius.yml") `
    (Join-Path $WorkflowDir "pr-blast-radius.yml") -Force

if (Test-Path $PackageDir) {
    Remove-Item -Recurse -Force $PackageDir
}

# Copy package tree, excluding local junk
$exclude = @("temp_repos", "template", "__pycache__", ".git", "prgraf-report.md", "*.egg-info")
robocopy $Source $PackageDir /E /NFL /NDL /NJH /NJS /nc /ns /np `
    /XD temp_repos template __pycache__ .git .venv `
    /XF prgraf-report.md | Out-Null
# robocopy exit codes 0-7 are success
if ($LASTEXITCODE -ge 8) {
    throw "robocopy failed with exit code $LASTEXITCODE"
}

Write-Host "Installed prgraf PR template into:"
Write-Host "  $WorkflowDir\pr-blast-radius.yml"
Write-Host "  $PackageDir"
Write-Host ""
Write-Host "Next: commit these files, open a PR, and the review comment will post automatically."
