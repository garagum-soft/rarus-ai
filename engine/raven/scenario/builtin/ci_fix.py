"""Wait for CI, and while it is red, have an agent fix the code and push, up to `cycles` times.

Each cycle waits for the branch's GitHub checks to finish. Green ends the run.
Red hands the fixer the report -- every check, the annotations and the error
lines of the failed jobs' logs -- with the task of fixing it in this checkout,
committing and pushing to the same branch; the next cycle waits on the commit
that push produced. The fixer answers PUSHED <sha>, or CANNOT: <why> to stop.

This scenario reads CI and nothing else on its own: the fix, the commit and the
push are the fixer's, made with its own tools under its own permissions. A
fixer that may not push says so and the run ends there.

Parameters: `repo` (owner/repo; default the checkout's origin), `branch`
(default the checkout's branch), `fixer` (the agent), `cycles` (fixes at most),
`wait` (seconds to wait for one CI run), `task` (what the branch is for, so the
fixer does not "fix" the test instead of the code).
"""

PARAMS = {"repo": "", "branch": "", "fixer": "Raven", "cycles": "3", "wait": "1800", "task": ""}


async def main(sc):
    import asyncio
    import re

    from raven.stint import ci

    repo = sc.args["repo"].strip() or ci.repo_of(sc.workspace)
    branch = sc.args["branch"].strip() or ci.branch_of(sc.workspace)
    if not repo:
        raise RuntimeError(f"{sc.workspace} has no GitHub origin remote; pass repo=owner/repo")
    if not branch:
        raise RuntimeError("the checkout is on a detached head; pass branch=<name>")
    cycles, wait_s = int(sc.args["cycles"]), float(sc.args["wait"])
    fixes = 0
    while True:
        sc.log(f"waiting for CI on {repo}@{branch}")
        report = await ci.wait_async(repo, branch, timeout_s=wait_s)
        sha = report.sha
        sc.log(f"CI {report.state} at {sha[:12]} ({len(report.checks)} checks)")
        if report.state == "success":
            return f"CI is green on {repo}@{branch} ({sha[:12]}) after {fixes} fix(es)."
        if report.state == "none":
            raise RuntimeError(f"no CI checks appeared for {repo}@{branch} ({sha[:12]})")
        if report.state == "pending":
            raise RuntimeError(f"CI on {repo}@{branch} did not finish within {wait_s:g}s")
        report = await asyncio.to_thread(ci.explain, report)
        if fixes >= cycles:
            return f"Still red after {fixes} fix(es):\n\n{report.render()}"
        context = f"\nWhat the branch is for: {sc.args['task']}\n" if sc.args["task"].strip() else ""
        reply = await sc.run(
            sc.args["fixer"],
            f"CI is red on branch {branch} of {repo} at commit {sha}. Fix the code in the checkout "
            f"{sc.workspace} so the failing checks pass -- fix the cause, do not weaken or skip the tests. "
            "Run the failing checks locally where you can. Then commit the fix and push it to "
            f"{branch} on origin. When it is pushed, answer with a line 'PUSHED <commit sha>'; if you "
            "cannot fix it or cannot push, answer 'CANNOT: <why>' instead.\n"
            f"{context}\nThe CI report:\n{report.render()}",
            name="fixer",
        )
        if not reply.ok:
            raise RuntimeError(f"the fixer failed: {reply.error}")
        fixes += 1
        cannot = re.search(r"^\s*CANNOT:\s*(.+)$", reply.text, re.M | re.I)
        if cannot:
            return f"The fixer could not fix it: {cannot.group(1).strip()}\n\n{report.render()}"
        sc.log(f"fix {fixes} answered; waiting for the push to reach {branch}")

        def moved():
            now = ci.read(repo, branch)
            return now.sha if now.sha and now.sha != sha else None

        try:
            new = await sc.wait_for(lambda: asyncio.to_thread(moved), timeout=180, every=10, what="a new commit")
        except TimeoutError:
            return f"The fixer answered but {repo}@{branch} still points at {sha[:12]}: nothing was pushed.\n\n{reply.text}"
        sc.log(f"{branch} moved to {new[:12]}")
