"""Write, review, revise: a writer drafts and a reviewer reads it until the reviewer approves.

Each round the reviewer answers with APPROVED on its own line, or with what has
to change; the writer revises from that. The run ends at approval or after
`rounds` reviews, and returns the last draft with the reviewer's last word.
The two may be the same agent or different ones -- a second model as reviewer
is the point of the scenario.
"""

PARAMS = {"task": None, "writer": "Raven", "reviewer": "Raven", "rounds": "3"}


async def main(sc):
    task = sc.args["task"]
    draft = await sc.run(sc.args["writer"], task, name="writer")
    if not draft.ok:
        raise RuntimeError(f"the writer failed: {draft.error}")
    verdict = None
    for round_no in range(1, int(sc.args["rounds"]) + 1):
        verdict = await sc.run(
            sc.args["reviewer"],
            "Review this work against the task. Answer APPROVED on a line of its own if it is done; "
            f"otherwise list exactly what must change.\n\nTask:\n{task}\n\nWork:\n{draft.text}",
            name="reviewer",
        )
        if not verdict.ok:
            raise RuntimeError(f"the reviewer failed in round {round_no}: {verdict.error}")
        if any(line.strip().upper().strip(".!*") == "APPROVED" for line in verdict.text.splitlines()):
            sc.log(f"approved in round {round_no}")
            return f"{draft.text}\n\n---\nApproved by the reviewer in round {round_no}."
        sc.log(f"round {round_no}: changes asked")
        draft = await sc.run(
            sc.args["writer"],
            f"Revise your work for the task.\n\nTask:\n{task}\n\nYour work:\n{draft.text}\n\n"
            f"The reviewer asks for:\n{verdict.text}",
            name="writer",
        )
        if not draft.ok:
            raise RuntimeError(f"the writer failed revising in round {round_no}: {draft.error}")
    return f"{draft.text}\n\n---\nNot approved after {sc.args['rounds']} round(s). Last review:\n{verdict.text if verdict else ''}"
