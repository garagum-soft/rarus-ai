"""The same task to several agents at once; a synthesizer merges what they found.

`agents` is a comma-separated list of roster names. They run in parallel,
each unaware of the others; a failed one is noted and the rest still count.
The synthesizer gets every answer and writes one result, saying where the
answers disagree.
"""

PARAMS = {"task": None, "agents": "Raven,Raven", "synthesizer": "Raven", "timeout": "900"}


async def main(sc):
    task = sc.args["task"]
    names = [a.strip() for a in sc.args["agents"].split(",") if a.strip()]
    if not names:
        raise RuntimeError("no agents given")
    runs = [
        sc.start(agent, task, name=f"worker-{i}", timeout=float(sc.args["timeout"])) for i, agent in enumerate(names, 1)
    ]
    replies = await sc.wait(*runs)
    good = [r for r in replies if r.ok]
    for r in replies:
        if not r.ok:
            sc.log(f"{r.name} ({r.agent}) failed: {r.error}")
    if not good:
        raise RuntimeError("every agent failed")
    answers = "\n\n".join(f"[{r.name}, {r.agent}]\n{r.text}" for r in good)
    merged = await sc.run(
        sc.args["synthesizer"],
        f"Several agents answered the same task independently.\n\nTask:\n{task}\n\nAnswers:\n{answers}\n\n"
        "Write one result from them. Keep what they agree on, resolve or flag where they disagree.",
        name="synthesizer",
    )
    if not merged.ok:
        raise RuntimeError(f"the synthesizer failed: {merged.error}")
    return merged.text
