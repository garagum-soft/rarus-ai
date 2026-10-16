"""Two agents argue a question in turns, and a judge decides from the transcript.

`pro` argues for, `con` against, for `rounds` exchanges; each sees the debate
so far. The judge then names the stronger case and why. Useful for a design
choice with two credible answers, where one agent alone tends to agree with
itself.
"""

PARAMS = {"question": None, "pro": "Raven", "con": "Raven", "judge": "Raven", "rounds": "2"}


async def main(sc):
    question = sc.args["question"]
    transcript: list[str] = []
    for round_no in range(1, int(sc.args["rounds"]) + 1):
        for side, agent in (("for", sc.args["pro"]), ("against", sc.args["con"])):
            so_far = "\n\n".join(transcript) or "(nothing yet)"
            reply = await sc.run(
                agent,
                f"You are arguing {side} in a debate. Question: {question}\n\nDebate so far:\n{so_far}\n\n"
                f"Give your strongest argument {side} in at most 150 words, answering the other side's last point.",
                name=f"{'pro' if side == 'for' else 'con'}",
            )
            if not reply.ok:
                raise RuntimeError(f"the {side} side failed in round {round_no}: {reply.error}")
            transcript.append(f"[{side}, round {round_no}] {reply.text}")
    verdict = await sc.run(
        sc.args["judge"],
        f"Judge this debate. Question: {question}\n\n"
        + "\n\n".join(transcript)
        + "\n\nSay which side made the stronger case and why, in at most 120 words.",
        name="judge",
    )
    if not verdict.ok:
        raise RuntimeError(f"the judge failed: {verdict.error}")
    return "\n\n".join(transcript) + f"\n\n[verdict] {verdict.text}"
