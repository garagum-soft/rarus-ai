$sp = "C:\Users\Smart\AppData\Local\Temp\claude\C--235235-agent-orchestration\8cbbab59-9964-4bc5-8a72-09204d5ac69f\scratchpad"
$env:PYTHONPATH = "C:\235235\rarus-ai\app"; $env:PYTHONUTF8 = "1"
$env:HOLD_HOME = "$sp\hold_home"
Remove-Item $env:HOLD_HOME -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory $env:HOLD_HOME | Out-Null
$env:RARUS_HOME = $env:HOLD_HOME; $env:RARUS_SECRET_STORE = "plain"; $env:RARUS_SITE = "http://127.0.0.1:47900"
& "$sp\rarus_venv\Scripts\python.exe" -c "
from raven.providers import rarus_session
rarus_session.save_session('sk-ab-' + '0123456789abcdef' * 3, email='demo@example.com', label='demo')
from raven.config.update import set_default_model
set_default_model('rarus/deepseek-flash', provider='rarus')
"
& "$sp\rarus_venv\Scripts\python.exe" "$sp\engine_hold.py" 1200 *> "$sp\hold.txt"
