#!/usr/bin/env pwsh

# Install k8sgpt and ensure it's on PATH (Windows PowerShell)
# Usage:
#   $env:DRY_RUN=1; ./scripts/install-k8sgpt.ps1   # dry run
#   ./scripts/install-k8sgpt.ps1                    # real install

$ErrorActionPreference = 'Stop'

function Log($msg) { Write-Host "[install-k8sgpt] $msg" }

$DRY_RUN = $env:DRY_RUN
if (-not $DRY_RUN) { $DRY_RUN = '0' }
function Run($cmd) {
  if ($DRY_RUN -eq '1') { Write-Host "+ $cmd" }
  else { Invoke-Expression $cmd }
}

function Ensure-Command($name) {
  if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
    throw "Missing required command: $name"
  }
}

function Choose-BinDir {
  $localBin = "$env:LOCALAPPDATA\\Microsoft\\WindowsApps"
  if (Test-Path $localBin) { return $localBin }
  $homeBin = "$env:USERPROFILE\\.local\\bin"
  if (-not (Test-Path $homeBin)) { Run "New-Item -ItemType Directory -Force -Path '$homeBin' | Out-Null" }
  return $homeBin
}

function Ensure-OnPath($binDir) {
  $paths = [Environment]::GetEnvironmentVariable('Path', 'User')
  if (-not $paths) { $paths = '' }
  if ($paths -notlike "*${binDir}*") {
    Log "Adding $binDir to user PATH"
    $newPaths = if ($paths) { "$binDir;$paths" } else { $binDir }
    [Environment]::SetEnvironmentVariable('Path', $newPaths, 'User')
    Log "Restart the shell to pick up the updated PATH."
  }
}

function Get-LatestDownloadUrl {
  Ensure-Command curl
  $api = 'https://api.github.com/repos/k8sgpt-ai/k8sgpt/releases/latest'
  $json = (curl -sL $api) | Out-String
  $urls = Select-String -InputObject $json -Pattern '"browser_download_url"\s*:\s*"([^"]+)"' -AllMatches | ForEach-Object { $_.Matches.Groups[1].Value }
  $arch = $env:PROCESSOR_ARCHITECTURE
  $archTokens = if ($arch -match 'ARM64') { 'arm64|aarch64' } else { 'amd64|x86_64' }
  $url = $urls | Where-Object { $_ -match 'Windows' -and $_ -match $archTokens -and $_ -match '\.(zip)$' } | Select-Object -First 1
  if (-not $url) { throw 'Could not determine a suitable download URL from latest release assets' }
  return $url
}

function Install-FromUrl($url, $binDir) {
  $tmp = New-Item -ItemType Directory -Path ([System.IO.Path]::GetTempPath() + [System.IO.Path]::GetRandomFileName())
  try {
    $file = Join-Path $tmp.FullName (Split-Path $url -Leaf)
    Log "Downloading $url"
    Run "curl -fL '$url' -o '$file'"

    $extractDir = Join-Path $tmp.FullName 'extracted'
    Run "New-Item -ItemType Directory -Force -Path '$extractDir' | Out-Null"

    if ($file -match '\.zip$') {
      # Expand-Archive handles .zip natively
      Run "powershell -NoProfile -Command \"Expand-Archive -Force -Path '$file' -DestinationPath '$extractDir'\""
    } else {
      Ensure-Command tar
      Run "tar -xzf '$file' -C '$extractDir' 2>$null"
    }

    $bin = Get-ChildItem -Path $extractDir -Recurse -Filter 'k8sgpt.exe' | Select-Object -First 1
    if (-not $bin) { throw 'Failed to locate k8sgpt.exe in the downloaded archive' }

    $target = Join-Path $binDir 'k8sgpt.exe'
    Log "Installing to $target"
    Run "Copy-Item -Force '$($bin.FullName)' '$target'"
  } finally {
    Remove-Item -Recurse -Force $tmp.FullName -ErrorAction SilentlyContinue
  }
}

function Main {
  $binDir = Choose-BinDir
  if (Get-Command k8sgpt -ErrorAction SilentlyContinue) {
    Log "k8sgpt already installed at $(Get-Command k8sgpt | Select-Object -ExpandProperty Source)"
    Ensure-OnPath $binDir
    return
  }
  $url = Get-LatestDownloadUrl
  Install-FromUrl $url $binDir
  Ensure-OnPath $binDir
  Log 'k8sgpt installation complete.'
}

Main
