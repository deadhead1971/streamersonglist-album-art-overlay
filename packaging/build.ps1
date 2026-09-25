<#
.SYNOPSIS
  Build the Windows app and its installer: dist\AlbumArtOverlay\ (the
  PyInstaller folder) and dist\AlbumArtOverlay-Setup-<version>.exe.

.DESCRIPTION
  1. A clean venv in build\venv with the exact pins in requirements-build.txt.
  2. PyInstaller, from AlbumArtOverlay.spec.
  3. THIRD-PARTY-NOTICES.txt, generated from the bundled packages' licences.
  4. --self-check: the pieces a bundle can lose without anything failing.
  5. An HTTP smoke test of the frozen app against a throwaway data folder.
  6. The installer, with Inno Setup 6 (skipped with -NoInstaller).
  7. SHA256SUMS.txt.
  Any failure stops the build with a non-zero exit.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File packaging\build.ps1
  powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Python "C:\Python312\python.exe"
#>
param(
    # The interpreter the venv is made from. Release builds use 3.12.
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
$Exe = Join-Path $AppDir "AlbumArtOverlay.exe"

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Check-Exit($what) {
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" }
}

$Version = (Select-String -Path (Join-Path $Repo "app\__init__.py") `
    -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
Write-Host "Album Art Overlay $Version"

Step "Clean venv"
$Venv = Join-Path $Build "venv"
if (Test-Path $Venv) { Remove-Item -Recurse -Force $Venv }
& $Python -m venv $Venv; Check-Exit "venv"
$VPy = Join-Path $Venv "Scripts\python.exe"
& $VPy -m pip install --quiet --disable-pip-version-check `
    -r (Join-Path $PSScriptRoot "requirements-build.txt"); Check-Exit "pip install"
& $VPy --version

Step "PyInstaller"
& $VPy -m PyInstaller (Join-Path $PSScriptRoot "AlbumArtOverlay.spec") `
    --noconfirm --clean --log-level WARN `
    --distpath $Dist --workpath (Join-Path $Build "pyinstaller"); Check-Exit "PyInstaller"

Step "Third-party notices"
& $VPy (Join-Path $PSScriptRoot "notices.py") `
    (Join-Path $AppDir "THIRD-PARTY-NOTICES.txt"); Check-Exit "notices"

# A throwaway data folder with a space and an accent in its name, like a real
# user profile can have.
$Scratch = Join-Path ([IO.Path]::GetTempPath()) ("AlbumArtOverlay build é " + [guid]::NewGuid())

Step "Self-check"
# A windowed exe run without a console has no stdout; it writes the report
# into the data folder instead.
$p = Start-Process -FilePath $Exe -ArgumentList @("--self-check", "--data-dir", "`"$Scratch\check`"") `
    -Wait -PassThru -NoNewWindow
$report = Join-Path $Scratch "check\self-check.txt"
if (Test-Path $report) { Get-Content $report -Encoding UTF8 }
if ($p.ExitCode -ne 0) { throw "self-check failed ($($p.ExitCode) failure(s))" }

Step "Smoke test"
$app = Start-Process -FilePath $Exe -PassThru -ArgumentList @(
    "--no-tray", "--background", "--port", $SmokePort, "--data-dir", "`"$Scratch\smoke`"")
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
}

Step "Checksums"
$sums = foreach ($f in $Artifacts + $Exe) {
    "{0}  {1}" -f (Get-FileHash $f -Algorithm SHA256).Hash.ToLower(), (Split-Path -Leaf $f)
}
$sums | Set-Content -Path (Join-Path $Dist "SHA256SUMS.txt") -Encoding ASCII
$sums

Write-Host "`nBuilt $Version in $Dist" -ForegroundColor Green
