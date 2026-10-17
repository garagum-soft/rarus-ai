param([string]$Tag, [string[]]$Files = @())
# Full (or partial) pytest on the Rarus engine only, detached, 180 s per-test timeout.
$sp = "C:\Users\Smart\AppData\Local\Temp\claude\C--235235-agent-orchestration\8cbbab59-9964-4bc5-8a72-09204d5ac69f\scratchpad"
$runner = "C:\Users\Smart\AppData\Local\Temp\claude\C--235235-agent-orchestration\46732747-3200-415e-afb7-4cf35d656f08\scratchpad\run_nowin.py"
$fake = "$sp\fakehome2"
$env:USERPROFILE = $fake; $env:HOME = $fake; $env:PYTHONUTF8 = "1"
$env:RAVEN_HOME = "$fake\.raven"; $env:RARUS_HOME = "$fake\.rarus"
$py = "$sp\rarus_venv\Scripts\python.exe"
$pidText = & $py $runner "$sp\$Tag.log" "C:\235235\rarus-ai\engine" -- $py -m pytest -v -p no:cacheprovider -rf --no-header --timeout=180 --timeout-method=thread @Files
$procId = [int]($pidText | Select-Object -Last 1)
Wait-Process -Id $procId -ErrorAction SilentlyContinue
"done" | Set-Content "$sp\$Tag.done"
