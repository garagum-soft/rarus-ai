"""Split a large goal into a tree of tasks, run the leaves level by level, and put the results together.

A planner splits the goal recursively (raven.playbook.decompose: depth, children
and leaves bounded, every answer checked); an optional reviewer may send the
plan back. The leaves then run on `worker` (or the agent the planner named for
one) a level at a time, the leaves of one level at once up to `parallel`, each
handed the results of the leaves it waits on. A leaf whose prerequisite failed
is skipped rather than run on a hole. The tree, with every leaf's status, is
saved under <workspace>/.raven/plans/<plan>/ as it goes; `synthesizer` writes
the answer from the leaves' results.
"""

PARAMS = {
    "goal": None,
    "planner": "Raven",
    "reviewer": "",
    "worker": "Raven",
    "synthesizer": "Raven",
    "depth": "2",
    "max_children": "5",
    "max_leaves": "12",
    "parallel": "3",
}


async def main(sc):
    import asyncio

    from raven.playbook import decompose as dc

    goal = sc.args["goal"]

    async def ask(role, prompt):
        agent = sc.args["reviewer"] if role == "reviewer" else sc.args["planner"]
        reply = await sc.run(agent, prompt, name=role)
        if not reply.ok:
            raise RuntimeError(f"the {role} failed: {reply.error}")
        return reply.text

    tree = await dc.decompose(
        goal,
        ask,
        max_depth=int(sc.args["depth"]),
        max_children=int(sc.args["max_children"]),
        max_leaves=int(sc.args["max_leaves"]),
        agents=sc.agents(),
        review_rounds=1 if sc.args["reviewer"].strip() else 0,
        # Named after the run's own tag, so the plan and the run's record are found together.
        plan_id=f"plan-{sc.run_id.rsplit('-', 1)[-1]}",
    )
    where = dc.plan_dir(sc.workspace, tree.id)
    dc.save(tree, where)
    waves = dc.levels(tree)
    sc.log(f"plan {tree.id}: {len(tree.leaves())} leaves in {len(waves)} level(s), saved to {where}")
    deps = dc.leaf_dependencies(tree)
    outputs = {}
    gate = asyncio.Semaphore(max(1, int(sc.args["parallel"])))

    async def work(leaf):
        if any(tree.find(d).status != "done" for d in deps[leaf.id]):
            leaf.status, leaf.error = "skipped", "a part it waits on did not finish"
            return
        async with gate:
            leaf.status = "running"
            dc.save(tree, where)
            reply = await sc.run(leaf.agent or sc.args["worker"], dc.leaf_prompt(tree, leaf, outputs), name=leaf.id)
        if reply.ok:
            leaf.status, outputs[leaf.id] = "done", reply.text
            leaf.output = reply.text[:4000]
            (where / f"{leaf.id}.md").write_text(reply.text, encoding="utf-8")
        else:
            leaf.status, leaf.error = "failed", reply.error

    for wave in waves:
        await asyncio.gather(*(work(tree.find(leaf_id)) for leaf_id in wave))
        dc.save(tree, where)
    done = [leaf for leaf in tree.leaves() if leaf.status == "done"]
    missing = [leaf for leaf in tree.leaves() if leaf.status != "done"]
    sc.log(f"{len(done)} of {len(tree.leaves())} leaves done")
    if not done:
        raise RuntimeError(f"no part of the plan finished; see {where / 'plan.md'}")
    parts = "\n\n".join(f"## {leaf.title} ({leaf.id})\n{outputs[leaf.id]}" for leaf in done)
    gaps = "".join(f"- {leaf.title}: {leaf.status} ({leaf.error})\n" for leaf in missing)
    final = await sc.run(
        sc.args["synthesizer"],
        f"Several agents each did one part of this goal:\n{goal}\n\nPut their results together into one answer "
        "to the goal. Say plainly what is missing."
        + (f"\n\nParts that did not finish:\n{gaps}" if gaps else "")
        + f"\n\nThe parts:\n\n{parts}",
        name="synthesizer",
    )
    if not final.ok:
        raise RuntimeError(f"the synthesizer failed: {final.error}")
    return f"{final.text}\n\n---\nPlan {tree.id}: {len(done)} of {len(tree.leaves())} parts done; {where / 'plan.md'}"
