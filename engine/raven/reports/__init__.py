"""Reports, retrospectives and the run archive, read off the journals Raven already keeps.

Nothing here records anything new while work runs. The conversations
(``sessions/*.jsonl``), every sub-agent call and graph run (``subagents/``),
every stint record and the usage ledger are already on disk; this package reads
them after the fact:

- :mod:`runs` -- one :class:`~raven.reports.runs.RunEntry` per graph run, spawn
  and stint, with its outcome, failures, retries, cost and receipt.
- :mod:`retro` -- a retrospective of one run or of a period: what went well,
  what went wrong, what is left open. Deterministic: every line is a fact read
  from a record, so two builds over the same journals say the same thing.
- :mod:`report` -- the period report (conversations, runs, failures, cost) as
  Markdown or JSON.
- :mod:`archive` -- a durable one-line-per-run index under ``<RAVEN_HOME>/reports``
  that outlives the records it summarises (retention may delete a session; its
  runs stay in the archive).
"""
