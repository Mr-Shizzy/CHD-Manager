<#
  Builds the portable release:  dist\CHD Manager\  (CHD Manager.exe + chdman.exe + licenses)
  and a zip of it:              dist\CHD-Manager-<version>-win64.zip

  Needs:  Python 3 with tkinter (tested with 3.12; the python.org install has it) and
          7-Zip (only to unpack MAME's download).  Nothing else is installed except PyInstaller (via pip).

  Run from this folder:
      powershell -ExecutionPolicy Bypass -File build.ps1

  Options:
      -ChdmanPath C:\path\to\chdman.exe    use this chdman instead of downloading the latest one from MAME
#>
param([string]$ChdmanPath = "")

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$root = $PSScriptRoot

function Fail($msg) { Write-Host "`nERROR: $msg" -ForegroundColor Red; exit 1 }

# ---- 1. Python
try { $pyver = & python --version 2>&1 } catch { Fail "Python was not found. Install Python 3 from python.org (tick 'Add python.exe to PATH')." }
Write-Host "Using $pyver"
& python -c "import tkinter" 2>$null
if ($LASTEXITCODE -ne 0) { Fail "This Python has no tkinter. Reinstall Python from python.org with the 'tcl/tk' option ticked." }

# ---- 2. PyInstaller
Write-Host "Installing/updating PyInstaller (pip)..."
& python -m pip install --quiet --upgrade pyinstaller
if ($LASTEXITCODE -ne 0) { Fail "Could not install PyInstaller." }

# ---- 3. chdman
New-Item -ItemType Directory -Force "$root\build" | Out-Null
$chdman = "$root\build\chdman.exe"
if ($ChdmanPath) {
    if (-not (Test-Path $ChdmanPath)) { Fail "chdman not found at $ChdmanPath" }
    Copy-Item $ChdmanPath $chdman -Force
    Write-Host "Using chdman from $ChdmanPath"
} else {
    Write-Host "Looking up the latest MAME release on GitHub..."
    $rel = Invoke-RestMethod "https://api.github.com/repos/mamedev/mame/releases/latest" -Headers @{ 'User-Agent' = 'CHD-Manager-build' }
    $asset = $rel.assets | Where-Object { $_.name -match '^mame\d+b_(x64|64bit)\.exe$' } | Select-Object -First 1
    $sumsAsset = $rel.assets | Where-Object { $_.name -eq 'SHA256SUMS' } | Select-Object -First 1
    if (-not $asset -or -not $sumsAsset) { Fail "Could not find the Windows download or SHA256SUMS in release $($rel.tag_name). MAME may have renamed it; download chdman by hand and use -ChdmanPath." }
    foreach ($u in @($asset.browser_download_url, $sumsAsset.browser_download_url)) {
        if ($u -notlike 'https://github.com/mamedev/mame/*') { Fail "Unexpected download address: $u" }
    }
    $sevenZip = @("$env:ProgramFiles\7-Zip\7z.exe", "${env:ProgramFiles(x86)}\7-Zip\7z.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $sevenZip) { $c = Get-Command 7z -ErrorAction SilentlyContinue; if ($c) { $sevenZip = $c.Source } }
    if (-not $sevenZip) { Fail "7-Zip is needed to unpack MAME's download (it is opened, never run). Install it from 7-zip.org, or pass -ChdmanPath." }

    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("chdman_build_" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory $tmp | Out-Null
    try {
        Write-Host ("Downloading {0} ({1} MB) from github.com/mamedev/mame..." -f $asset.name, [math]::Round($asset.size / 1MB))
        $dl = Join-Path $tmp $asset.name
        Invoke-WebRequest $asset.browser_download_url -OutFile $dl -UseBasicParsing
        $sums = (Invoke-WebRequest $sumsAsset.browser_download_url -UseBasicParsing).Content
        if ($sums -is [byte[]]) { $sums = [Text.Encoding]::UTF8.GetString($sums) }   # GitHub serves it as raw bytes
        $want = ($sums -split "`n" | Where-Object { $_ -match [regex]::Escape($asset.name) } | Select-Object -First 1) -replace '^\s*([0-9a-fA-F]{64}).*$', '$1'
        $got = (Get-FileHash $dl -Algorithm SHA256).Hash
        if (-not $want -or $want.ToLower() -ne $got.ToLower()) { Fail "Checksum mismatch - download discarded." }
        Write-Host "SHA-256 verified."
        & $sevenZip e $dl "-o$tmp\x" chdman.exe -y | Out-Null
        if (-not (Test-Path "$tmp\x\chdman.exe")) { Fail "Could not extract chdman.exe." }
        Copy-Item "$tmp\x\chdman.exe" $chdman -Force
        Write-Host "Got chdman from MAME $($rel.tag_name)."
    } finally {
        Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# ---- 4. exe
Write-Host "Building the exe..."
& python -m PyInstaller --noconfirm --clean --onefile --windowed --name "CHD Manager" `
    --icon "$root\assets\icon.ico" --add-data "${chdman};." `
    --distpath "$root\build\exe" --workpath "$root\build\work" --specpath "$root\build" "$root\CHD_Manager.pyw"
if ($LASTEXITCODE -ne 0 -or -not (Test-Path "$root\build\exe\CHD Manager.exe")) { Fail "PyInstaller failed." }

# ---- 5. portable folder + zip
$ver = (Select-String -Path "$root\CHD_Manager.pyw" -Pattern '^APP_VER\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
$out = "$root\dist\CHD Manager"
Remove-Item "$root\dist" -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $out | Out-Null
Copy-Item "$root\build\exe\CHD Manager.exe" $out
Copy-Item $chdman "$out\chdman.exe"
Copy-Item "$root\LICENSE", "$root\THIRD_PARTY_NOTICES.txt", "$root\README.md" $out
$zip = "$root\dist\CHD-Manager-$ver-win64.zip"
Compress-Archive -Path $out -DestinationPath $zip -Force
Write-Host "`nDone." -ForegroundColor Green
Write-Host "  Folder: $out"
Write-Host "  Zip:    $zip  (SHA-256 $((Get-FileHash $zip -Algorithm SHA256).Hash))"
