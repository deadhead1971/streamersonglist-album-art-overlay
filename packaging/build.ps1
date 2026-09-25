<#
.SYNOPSIS
  Build the Windows app and its installer: dist\AlbumArtOverlay\ (the folder
  that gets installed) and dist\AlbumArtOverlay-Setup-<version>.exe.

.DESCRIPTION
  The installed app is the app's plain source run by python.org's official
  embeddable Python. Its only executables are python.exe and pythonw.exe,
  signed by the Python Software Foundation. A PyInstaller build was tried
  first; Defender's machine-learning detection quarantined its unsigned exe
  on launch (Trojan:Script/Wacatac.C!ml, 2026-09-25), and a verdict like that
  can land on any release, because each build is a new, unknown file.

  1. The embeddable Python matching -Python's exact version, downloaded once
     and cached in build\. Its signature is verified before anything else.
  2. tkinter (for the Browse... dialogs), which the embeddable Python leaves
     out, copied from -Python's own install.
  3. The pinned dependencies (requirements-app.txt) into its site-packages.
  4. The app's source, loaders, icon and the installed.txt marker that makes
     app/config.py keep data in %LOCALAPPDATA%, then everything precompiled.
  5. THIRD-PARTY-NOTICES.txt, --self-check, and an HTTP smoke test.
  6. The installer (Inno Setup 6; skipped with -NoInstaller), SHA256SUMS.txt.
  Any failure stops the build with a non-zero exit.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File packaging\build.ps1
  powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Python "C:\Python312\python.exe"
#>
param(
    # A full python.org Python (not the embeddable one): the version to ship,
    # the source of tkinter, and the pip that installs the dependencies.
    # Release builds use 3.12.
    [string]$Python = "python",
    [switch]$NoInstaller,
    # Port for the smoke test; not 5050, so a running copy doesn't interfere.
    [int]$SmokePort = 5059
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
$Build = Join-Path $Repo "build"
$Dist = Join-Path $Repo "dist"
$AppDir = Join-Path $Dist "AlbumArtOverlay"
$PyDir = Join-Path $AppDir "python"
$EmbedPy = Join-Path $PyDir "python.exe"
$EmbedPyw = Join-Path $PyDir "pythonw.exe"

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Check-Exit($what) {
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" }
}

$Version = (Select-String -Path (Join-Path $Repo "app\__init__.py") `
    -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
Write-Host "Album Art Overlay $Version"

# --------------------------------------------------------------------------
Step "Build Python"
$info = & $Python -c "import platform, sys; print(platform.python_version()); print(sys.base_prefix); print(platform.architecture()[0])"
Check-Exit "running $Python"
$PyVersion, $BasePrefix, $Arch = $info
if ($Arch -ne "64bit") { throw "$Python is $Arch; the app ships as 64-bit" }
Write-Host "  $PyVersion at $BasePrefix"

# --------------------------------------------------------------------------
Step "Embeddable Python $PyVersion"
New-Item -ItemType Directory -Force $Build | Out-Null
$zip = Join-Path $Build "python-$PyVersion-embed-amd64.zip"
if (-not (Test-Path $zip)) {
    $url = "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-embed-amd64.zip"
    Write-Host "  downloading $url"
    Invoke-WebRequest $url -OutFile "$zip.part" -UseBasicParsing
    Move-Item "$zip.part" $zip
}
if (Test-Path $AppDir) { Remove-Item -Recurse -Force $AppDir }
New-Item -ItemType Directory -Force $PyDir | Out-Null
Expand-Archive $zip -DestinationPath $PyDir

# The point of this whole layout: the executables are the PSF's, as signed.
foreach ($f in Get-ChildItem $PyDir -Filter "python*.exe") {
    $sig = Get-AuthenticodeSignature $f.FullName
    if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch "O=Python Software Foundation") {
        throw "$($f.Name) is not validly signed by the Python Software Foundation ($($sig.Status))"
    }
    Write-Host "  $($f.Name): signed by the Python Software Foundation"
}

# ._pth replaces sys.path wholesale; entries are relative to this file.
# "..": the program folder, so `-m app.desktop` works whatever the cwd.
$pth = Get-ChildItem $PyDir -Filter "python*._pth" | Select-Object -First 1
$stdlib = (Get-ChildItem $PyDir -Filter "python*.zip" | Select-Object -First 1).Name
Set-Content -Path $pth.FullName -Encoding ASCII -Value @($stdlib, ".", "Lib", "site-packages", "..")

# --------------------------------------------------------------------------
Step "tkinter"
foreach ($dll in @("_tkinter.pyd") + (Get-ChildItem "$BasePrefix\DLLs" -Filter "t*86t.dll" | ForEach-Object Name)) {
    Copy-Item "$BasePrefix\DLLs\$dll" $PyDir
}
Copy-Item -Recurse "$BasePrefix\Lib\tkinter" "$PyDir\Lib\tkinter"
Remove-Item -Recurse -Force "$PyDir\Lib\tkinter\test" -ErrorAction SilentlyContinue
# Tcl finds python\tcl by itself. Only the runtime libraries: tcl8.6, tk8.6
# and tcl8 (its modules); not the headers, .lib files or Tix.
New-Item -ItemType Directory -Force "$PyDir\tcl" | Out-Null
foreach ($lib in "tcl8", "tcl8.6", "tk8.6") {
    Copy-Item -Recurse "$BasePrefix\tcl\$lib" "$PyDir\tcl\$lib"
}

# --------------------------------------------------------------------------
Step "Dependencies"
# --no-deps: requirements-app.txt pins the complete set, so nothing unpinned
# can slip in. --only-binary: no compiler, and no build scripts run.
& $Python -m pip install --quiet --disable-pip-version-check --no-warn-script-location `
    --no-deps --only-binary=:all: --target "$PyDir\site-packages" `
    -r (Join-Path $PSScriptRoot "requirements-app.txt"); Check-Exit "pip install"
Remove-Item -Recurse -Force "$PyDir\site-packages\bin" -ErrorAction SilentlyContinue

# --------------------------------------------------------------------------
Step "App"
& robocopy (Join-Path $Repo "app") (Join-Path $AppDir "app") /E /XD __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "copying app\ failed (robocopy $LASTEXITCODE)" }
& robocopy (Join-Path $Repo "obs") (Join-Path $AppDir "obs") /E /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "copying obs\ failed (robocopy $LASTEXITCODE)" }
$global:LASTEXITCODE = 0
Copy-Item (Join-Path $Repo "config.example.json"), (Join-Path $Repo "LICENSE") $AppDir
Copy-Item (Join-Path $PSScriptRoot "icon.ico") $AppDir
# app/config.py reads this marker's presence as "installed": data goes to
# %LOCALAPPDATA%\AlbumArtOverlay instead of this (replaceable) folder.
Set-Content -Path (Join-Path $AppDir "installed.txt") -Encoding ASCII -Value @(
    "This folder is an installed copy of Album Art Overlay $Version.",
    "Your settings, artwork library and log are not in here: they are in",
    "%LOCALAPPDATA%\AlbumArtOverlay, which upgrading and uninstalling leave alone.")
# Precompiled with the shipped interpreter, so nothing is written into the
# program folder at run time and the first start is quicker.
& $EmbedPy -m compileall -q -j 0 (Join-Path $AppDir "app") "$PyDir\Lib" "$PyDir\site-packages" | Out-Null
Check-Exit "compileall"

# --------------------------------------------------------------------------
Step "Third-party notices"
& $EmbedPy (Join-Path $PSScriptRoot "notices.py") (Join-Path $AppDir "THIRD-PARTY-NOTICES.txt")
Check-Exit "notices"

# A throwaway data folder with a space and an accent in its name, like a real
# user profile can have. (The accent is built from its code point: PowerShell
# 5.1 reads a script without a BOM as ANSI, so a literal one would arrive
# mangled.)
$Scratch = Join-Path ([IO.Path]::GetTempPath()) ("AlbumArtOverlay build " + [char]0x00E9 + " " + [guid]::NewGuid())

# Everything in the program folder now; the runs below must not add to it.
$Shipped = Get-ChildItem $AppDir -Recurse -File | ForEach-Object FullName

Step "Self-check"
& $EmbedPy -m app.desktop --self-check --data-dir "$Scratch\check"
if ($LASTEXITCODE -ne 0) { throw "self-check failed ($LASTEXITCODE failure(s))" }

Step "Smoke test"
# pythonw, exactly as the Start-menu shortcut runs it.
$app = Start-Process -FilePath $EmbedPyw -PassThru -WorkingDirectory $AppDir -ArgumentList @(
    "-m", "app.desktop", "--no-tray", "--background", "--port", $SmokePort,
    "--data-dir", "`"$Scratch\smoke`"")
$base = "http://127.0.0.1:$SmokePort"
try {
    $up = $false
    for ($i = 0; $i -lt 60 -and -not $up; $i++) {
        Start-Sleep -Milliseconds 500
        try { Invoke-WebRequest "$base/api/runtime/status" -UseBasicParsing -TimeoutSec 2 | Out-Null; $up = $true } catch { }
        if ($app.HasExited) { throw "the app exited during startup (code $($app.ExitCode))" }
    }
    if (-not $up) { throw "the app never answered on $base" }
    foreach ($path in "/api/runtime/status", "/settings", "/overlay/queue",
                      "/static/style.css", "/overlay/fallback.png", "/favicon.ico") {
        $r = Invoke-WebRequest "$base$path" -UseBasicParsing -TimeoutSec 10
        Write-Host ("  {0}  {1}" -f $r.StatusCode, $path)
    }
    foreach ($loader in "overlay_queue.html", "overlay_current.html", "overlay_wall.html") {
        if (-not (Test-Path (Join-Path $Scratch "smoke\obs\$loader"))) { throw "loader $loader not copied to the data folder" }
    }
    Invoke-RestMethod "$base/api/quit" -Method Post -ContentType "application/json" -Body "{}" | Out-Null
    if (-not $app.WaitForExit(15000)) { throw "the app did not quit" }
    Write-Host "  quit cleanly"
} finally {
    if (-not $app.HasExited) { $app.Kill() }
}
Remove-Item -Recurse -Force $Scratch -ErrorAction SilentlyContinue

# Running the app must not have written into the program folder: a new
# __pycache__ there means the precompile missed something, and anything the
# app writes there is lost on the next upgrade.
$added = Get-ChildItem $AppDir -Recurse -File | ForEach-Object FullName |
    Where-Object { $Shipped -notcontains $_ }
if ($added) { throw "running the app wrote into the program folder: $($added -join ', ')" }
Write-Host "  nothing written into the program folder"

$Artifacts = @()
if (-not $NoInstaller) {
    Step "Installer"
    $iscc = @(
        (Get-Command iscc.exe -ErrorAction SilentlyContinue | ForEach-Object Source),
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if (-not $iscc) { throw "Inno Setup 6 not found (winget install JRSoftware.InnoSetup), or use -NoInstaller" }
    & $iscc /Q "/DAppVersion=$Version" "/DAppSourceDir=$AppDir" "/O$Dist" `
        (Join-Path $PSScriptRoot "installer.iss"); Check-Exit "Inno Setup"
    $Artifacts += Join-Path $Dist "AlbumArtOverlay-Setup-$Version.exe"

    Step "Checksums"
    $sums = foreach ($f in $Artifacts) {
        "{0}  {1}" -f (Get-FileHash $f -Algorithm SHA256).Hash.ToLower(), (Split-Path -Leaf $f)
    }
    $sums | Set-Content -Path (Join-Path $Dist "SHA256SUMS.txt") -Encoding ASCII
    $sums
}

$size = (Get-ChildItem $AppDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("`nBuilt {0} in {1} ({2:N0} MB installed)" -f $Version, $Dist, $size) -ForegroundColor Green
