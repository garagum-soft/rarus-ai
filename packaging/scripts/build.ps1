# Build Rarus AI: page -> engine wheel -> frozen program -> gates -> installer.
#
#   powershell -File packaging\scripts\build.ps1 [-Version <x.y.z>] [-SkipPage] [-SkipInstaller] [-GatesOnly]
#
# -GatesOnly stops after step 3 (page, wheel, shell and packaging tests): what CI runs on every push.
#
# Every step checks its result and stops the build when it fails. Needs: node (npm) on PATH for the
# page (-SkipPage uses the ui-web/dist that is already built), `uv` for the build venv, and
# `makensis` for the last step: on PATH, or unpacked at build\tools\nsis\nsis-3.13 (without it the
# build stops after the gates and says so, which is what a machine with no NSIS gets). Nothing here
# downloads an installer or a toolchain: Python packages come from the package index through uv,
# the rest must already be on the machine.
param(
  [string]$Version = "",
  [switch]$SkipPage,
  [switch]$SkipInstaller,
  [switch]$GatesOnly
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$build = Join-Path $root "build"
if (-not $Version) {
  # the version lives in one place, the shell package
  $Version = [regex]::Match((Get-Content (Join-Path $root "app\rarus_app\__init__.py") -Raw), '__version__ = "([^"]+)"').Groups[1].Value
  if (-not $Version) { throw "could not read __version__ from app\rarus_app\__init__.py" }
}
$venv = Join-Path $build "venv"
$py = Join-Path $venv "Scripts\python.exe"

function Step($n, $text) { Write-Host "[$n/8] $text" -ForegroundColor Cyan }
function Run($exe, [string[]]$arguments) {
  & $exe @arguments
  if ($LASTEXITCODE -ne 0) { throw "failed ($LASTEXITCODE): $exe $($arguments -join ' ')" }
}

Step 1 "page: npm ci, vite build, build.py (assembles ui-web/dist, checks the boot snapshot)"
if ($SkipPage) {
  Write-Host "-SkipPage: using the page that is already built" -ForegroundColor Yellow
} else {
  Push-Location (Join-Path $root "engine\ui-web")
  try {
    if (-not (Test-Path "node_modules")) { Run "npm" @("ci") }
    Run "npm" @("run", "build")
    Run "uv" @("run", "--python", "3.12", "--no-project", "python", "build.py")
  } finally { Pop-Location }
}
if (-not (Test-Path (Join-Path $root "engine\ui-web\dist\index.html"))) { throw "the page was not built" }

Step 2 "engine wheel (carries the page, templates and data files)"
New-Item -ItemType Directory -Force $build | Out-Null
Remove-Item (Join-Path $build "wheels") -Recurse -Force -ErrorAction SilentlyContinue
Run "uv" @("build", "--wheel", "--out-dir", (Join-Path $build "wheels"), (Join-Path $root "engine"))
$wheel = Get-ChildItem (Join-Path $build "wheels") -Filter "raven-*.whl" | Select-Object -First 1
if (-not $wheel) { throw "no engine wheel was built" }

Step 3 "gates: engine tests that guard the contract, shell tests, page gates"
Run "uv" @("venv", $venv, "--python", "3.12", "--allow-existing")
# The wheel keeps its version number between builds, so the two local packages are reinstalled every
# time: without that the venv would keep the code of an earlier build.
Run "uv" @("pip", "install", "--python", $py, "--reinstall-package", "raven", "--reinstall-package", "rarus-app",
  $wheel.FullName, "$root\app[gui]", "pyinstaller", "pytest", "pytest-asyncio")
$env:PYTHONUTF8 = "1"
Run $py @("-m", "pytest", "-q", "-p", "no:cacheprovider", (Join-Path $root "app\tests"), (Join-Path $root "packaging\tests"))
if ($GatesOnly) {
  Write-Host "-GatesOnly: the gates passed, stopping before PyInstaller" -ForegroundColor Yellow
  exit 0
}

Step 4 "PyInstaller: onedir RarusAI"
Remove-Item (Join-Path $build "dist") -Recurse -Force -ErrorAction SilentlyContinue
Run $py @("-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", (Join-Path $build "dist"), "--workpath", (Join-Path $build "work"),
  (Join-Path $root "packaging\pyinstaller\rarus.spec"))
$dist = Join-Path $build "dist\RarusAI"
if (-not (Test-Path (Join-Path $dist "RarusAI.exe"))) { throw "the program was not built" }

# The WebView2 Evergreen bootstrapper is a signed Microsoft file, kept out of the repository (see
# packaging/redist/README.md). It rides in redist\ so the installer can run it on a machine that has
# no WebView2; only a file with a valid Microsoft signature is taken.
$bootstrapper = Join-Path $root "packaging\redist\MicrosoftEdgeWebview2Setup.exe"
if (Test-Path $bootstrapper) {
  $signature = Get-AuthenticodeSignature $bootstrapper
  if ($signature.Status -ne "Valid" -or $signature.SignerCertificate.Subject -notlike "CN=Microsoft Corporation,*") {
    throw "the WebView2 bootstrapper has no valid Microsoft signature: $($signature.Status)"
  }
  New-Item -ItemType Directory -Force (Join-Path $dist "redist") | Out-Null
  Copy-Item $bootstrapper (Join-Path $dist "redist")
} else {
  Write-Host "packaging\redist\MicrosoftEdgeWebview2Setup.exe is missing: the installer will not carry the WebView2 bootstrapper" -ForegroundColor Yellow
}

Step 5 "gate: no key shapes and no internal addresses in the build (spec 12.4)"
Run $py @((Join-Path $PSScriptRoot "check_build.py"), $dist)

Step 6 "smoke: the program answers --engine --help"
Run (Join-Path $dist "RarusAI.exe") @("--engine", "--help")

$makensis = (Get-Command makensis -ErrorAction SilentlyContinue).Source
if (-not $makensis) {
  $local = Join-Path $build "tools\nsis\nsis-3.13\Bin\makensis.exe"
  if (Test-Path $local) { $makensis = $local }
}
if ($SkipInstaller -or -not $makensis) {
  Write-Host "No NSIS (makensis) found, or -SkipInstaller: the installer step is skipped. The program is in $dist" -ForegroundColor Yellow
  exit 0
}

Step 7 "NSIS: RarusSetup-$Version.exe"
$setup = Join-Path $build "RarusSetup-$Version.exe"
Run $makensis @("/DVERSION=$Version", "/DSOURCE=$dist", "/DOUTFILE=$setup", (Join-Path $root "packaging\nsis\rarus.nsi"))
Run $py @((Join-Path $PSScriptRoot "check_build.py"), $setup)

Step 8 "SHA-256 and size"
$hash = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
$size = (Get-Item $setup).Length
"$hash  $(Split-Path $setup -Leaf)  $size" | Set-Content (Join-Path $build "RarusSetup-$Version.sha256") -Encoding ascii
Write-Host "done: $setup  sha256=$hash  size=$size" -ForegroundColor Green
