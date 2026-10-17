"""Launch a command in its own process group, with a console but no window, Ctrl+C disabled; log to a file.

A test that sends a console Ctrl event to its group cannot interrupt the run, and
xdist workers still get valid standard handles.

usage: run_nowin.py <log> <cwd> -- <cmd...>     (prints the pid)
"""

import subprocess
import sys

log, cwd = sys.argv[1:3]
cmd = sys.argv[sys.argv.index("--") + 1 :]

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000

out = open(log, "wb")  # noqa: SIM115 -- handed to the child, which outlives this script
p = subprocess.Popen(
    cmd,
    cwd=cwd,
    creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
    stdin=subprocess.DEVNULL,
    stdout=out,
    stderr=subprocess.STDOUT,
)
print(p.pid)
