param([string]$Name, [string[]]$Files, [int]$WaitMin = 9)
# Runs the given test files on the Rarus engine and on the untouched Raven copy, then diffs failures.
$sp = "C:\Users\Smart\AppData\Local\Temp\claude\C--235235-agent-orchestration\8cbbab59-9964-4bc5-8a72-09204d5ac69f\scratchpad"
$runner = "C:\Users\Smart\AppData\Local\Temp\claude\C--235235-agent-orchestration\46732747-3200-415e-afb7-4cf35d656f08\scratchpad\run_nowin.py"
function Start-Run($tag, $venv, $cwd, $fake) {
    $env:USERPROFILE = $fake; $env:HOME = $fake; $env:RAVEN_HOME = "$fake\.raven"; $env:RARUS_HOME = "$fake\.rarus"; $env:PYTHONUTF8 = "1"
    $py = "$sp\$venv\Scripts\python.exe"
    & $py $runner "$sp\$tag.log" $cwd -- $py -m pytest -q -p no:cacheprovider -rf --no-header @Files | Out-Null
}
Start-Run "$Name.mine" "rarus_venv" "C:\235235\rarus-ai\engine" "$sp\fakehome2"
Start-Run "$Name.base" "base_venv" "$sp\raven_base" "$sp\fakehome"
$end = (Get-Date).AddMinutes($WaitMin)
$a = ""; $b = ""
while ((Get-Date) -lt $end) {
    Start-Sleep 10
    $a = Get-Content "$sp\$Name.mine.log" -Raw -ErrorAction SilentlyContinue
    $b = Get-Content "$sp\$Name.base.log" -Raw -ErrorAction SilentlyContinue
    if (($a -match '\d+ (passed|failed|error)') -and ($b -match '\d+ (passed|failed|error)')) { break }
}
"mine: " + (($a -split "`n" | Where-Object { $_ -match '\d+ (passed|failed|error)' } | Select-Object -Last 1))
"base: " + (($b -split "`n" | Where-Object { $_ -match '\d+ (passed|failed|error)' } | Select-Object -Last 1))
foreach ($t in "mine", "base") {
    Get-Content "$sp\$Name.$t.log" | Where-Object { $_ -match '^(FAILED|ERROR) ' } |
        ForEach-Object { ($_ -replace ' - .*', '' -replace '^(FAILED|ERROR) ', '') } | Sort-Object -Unique |
        Set-Content "$sp\$Name.$t.fail.txt"
}
"diff (<= only in mine, => only in base):"
Compare-Object (Get-Content "$sp\$Name.mine.fail.txt") (Get-Content "$sp\$Name.base.fail.txt") |
    ForEach-Object { "$($_.SideIndicator) $($_.InputObject)" }
