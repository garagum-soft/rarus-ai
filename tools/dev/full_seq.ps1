param([string]$Tag)
# Full pytest on the Rarus engine, then on the untouched Raven copy, one after the other.
$sp = "C:\Users\Smart\AppData\Local\Temp\claude\C--235235-agent-orchestration\8cbbab59-9964-4bc5-8a72-09204d5ac69f\scratchpad"
$runner = "C:\Users\Smart\AppData\Local\Temp\claude\C--235235-agent-orchestration\46732747-3200-415e-afb7-4cf35d656f08\scratchpad\run_nowin.py"
function Run-One($tag, $venv, $cwd, $fake) {
    $env:USERPROFILE = $fake; $env:HOME = $fake; $env:RAVEN_HOME = "$fake\.raven"; $env:PYTHONUTF8 = "1"
    $py = "$sp\$venv\Scripts\python.exe"
    $pidText = & $py $runner "$sp\$tag.log" $cwd -- $py -m pytest -v -p no:cacheprovider -rf --no-header --timeout=180 --timeout-method=thread
    $procId = [int]($pidText | Select-Object -Last 1)
    Wait-Process -Id $procId -ErrorAction SilentlyContinue
}
Run-One "$Tag.mine" "rarus_venv" "C:\235235\rarus-ai\engine" "$sp\fakehome2"
"mine done" | Set-Content "$sp\$Tag.mine.done"
Run-One "$Tag.base" "base_venv" "$sp\raven_base" "$sp\fakehome"
"base done" | Set-Content "$sp\$Tag.base.done"

