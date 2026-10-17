param([int]$Wait = 35, [string]$Shot = "")
$sp = "C:\Users\Smart\AppData\Local\Temp\claude\C--235235-agent-orchestration\8cbbab59-9964-4bc5-8a72-09204d5ac69f\scratchpad"
$home2 = "$sp\win_home"
Remove-Item $home2 -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory $home2 | Out-Null
$env:RARUS_HOME = $home2; $env:RAVEN_HOME = $home2; $env:RARUS_SECRET_STORE = "plain"; $env:PYTHONUTF8 = "1"
$env:PYTHONPATH = "C:\235235\rarus-ai\app"
$p = Start-Process -FilePath "$sp\rarus_venv\Scripts\python.exe" -ArgumentList @("-m", "rarus_app") -WorkingDirectory "C:\235235\rarus-ai\app" -PassThru -RedirectStandardOutput "$sp\win_out.txt" -RedirectStandardError "$sp\win_err.txt"
Start-Sleep $Wait
"alive: " + (-not $p.HasExited)
if ($Shot) {
  Add-Type -AssemblyName System.Windows.Forms, System.Drawing
  $b = [System.Windows.Forms.SystemInformation]::VirtualScreen
  $bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size)
  $bmp.Save($Shot)
}
Get-Content "$sp\win_err.txt" -Tail 15
if (Test-Path "$home2\logs\engine.log") { "--- engine.log"; Get-Content "$home2\logs\engine.log" -Tail 8 }
Get-Process | Where-Object { $_.MainWindowTitle -like "*Rarus*" } | Select-Object Name, MainWindowTitle | Format-Table | Out-String
if (-not $p.HasExited) { & taskkill /PID $p.Id /T /F | Out-Null }
