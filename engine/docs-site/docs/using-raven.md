# Using Raven

Choose how to talk to Raven, give the conversation the right working directory,
and add other agents or services only when needed. Installed commands below can
be prefixed with `uv run` in a source checkout.

## Choose a surface

| Goal | Entry point | Next guide |
| --- | --- | --- |
| Terminal conversation | `raven tui` | [Command Reference](commands.md) |
| Browser conversation | `raven web` | [Launch WebUI](webui.md) |
| One task in a checkout | `raven agent -m "Summarize this project without editing files"` | [Permissions and security](permissions.md) |
| Chat from a messaging platform | A configured channel and resident gateway | [Channels and Messaging](channels.md) |
| Delegate to a specialist | A configured agent in the roster | [Agent Integrations](agent-integrations.md) |
| Follow up with a specialist | Its stateful instance | [Working with sub-agents](agent-collaboration.md) |
| Operate a shared browser | Browser tools and the WebUI panel | [Browser collaboration](browser-collaboration.md) |
| Retrieve document passages | Knowledge library or RPC client | [Knowledge bases](knowledge.md) |
| Run and watch a long job | Raven-Oncall with a declared campaign | [Long-running work (Oncall)](oncall.md) |
| Integrate an editor or remote peer | ACP or A2A | [Agent Protocols](agent-protocols.md) |

`raven agent` requires a message; it is not an interactive REPL. Use the TUI or
WebUI for a conversation. Start with [Quick Start](quick-start.md) if no model
provider is configured.

## Work in the right directory

The working directory holds the files the task acts on. Agent home holds
identity, memory, skills, and transcripts; it is not an interchangeable name for
the project checkout. For a one-shot task:

```bash
raven agent --workspace /absolute/path/to/project -m "Read the README and summarize the setup steps"
```

This executes a model turn and can incur provider charges. For changes, state
which files may be edited and how to verify the result. Parallel coding agents
do not automatically receive separate worktrees: divide file ownership or give
them separate working copies.

## Continue a conversation

Each one-shot CLI invocation starts a fresh session by default. Use one of
`--continue`, `--resume <id-or-prefix>`, or `--session <full-key>` when continuity
is intentional; these options are mutually exclusive.

```bash
raven sessions --help
raven agent --continue -m "Expand the previous summary; do not modify files"
```

Use the UI's conversation picker for interactive sessions. A sub-agent
`instance` continues that child's conversation; it is not the host session id
or a DAG node id. See [DAG Orchestration](orchestration.md).

## Add capability deliberately

Use a skill for reusable instructions, MCP for external tools, and a plugin for
runtime contributions. Use a specialized agent for a separate identity and
execution profile; use a DAG or playbook to coordinate dependent work. A2A is
for an independently operated peer, not another name for a local plugin.

[Skills, Memory and Extensions](skills-and-extensions.md) explains these choices.
For scheduled or proactive work, read [Proactive Reminders and Follow-ups](proactivity.md) and
keep the appropriate resident host running.

## Route requests to different models

Routing is off by default: every turn uses the default model, or the model a
conversation picked. To let the request decide, set `routing.enabled` to `true`
in `config.json` and choose a backend:

| `routing.backend` | Picks the model by | Needs |
| --- | --- | --- |
| `tiers` | How demanding the request reads (length, code, files named, several steps, design or refactor verbs, a request to think carefully) | A model per tier in `routing.tiers` |
| `ecoclaw` | The request's task category against benchmark scores, under `routing.profile` (`best`, `balanced`, `eco`) | An OpenRouter key |
| `knn` | Rewards recorded for similar past tasks | A prebuilt memory and `routing.models` |

```json
{
  "routing": {
    "enabled": true,
    "backend": "tiers",
    "tiers": {"simple": "ollama/llama3.2", "standard": "anthropic/claude-sonnet-4-5", "complex": "anthropic/claude-opus-4-5"}
  }
}
```

A tier left empty borrows the next stronger tier's model. When the chosen model
fails -- an error, a rate limit, an overload -- the turn escalates to the
stronger tiers and then the default model. `routing.simpleBelow` and
`routing.complexFrom` move the split points (0.3 and 0.65). Check a route
before turning it on:

```bash
raven routing explain "Refactor the auth module across three files"
raven routing status
```

A conversation's own model choice always outranks the router.

## Write workflows with loops and fan-outs

A playbook's graph is plain steps and `dependsOn`, and steps that wait on
nothing run at once. Three step types write the common shapes for you; they
are expanded into plain steps when the playbook loads, so every rule a graph
already follows applies to what they produce:

```yaml
nodes:
  - id: review
    subagent: Raven
    node_summary: Review a file
    prompt_template: "Review {{ item }}"
    forEach: [api.py, models.py, views.py]      # one copy per item, at once
    collect:                                     # one more step for the results
      node_summary: Merge the reviews
      prompt_template: "Merge these reviews:\n{{ results }}"
  - id: polish
    subagent: Raven
    node_summary: Polish the report
    dependsOn: [review]
    repeat: 3                                    # three passes, each on the last
    prompt_template: "Improve this:\n{{ previous }}\n\nThe reviews:\n{{ review.output }}"
```

`branches: [{...}, {...}]` runs different steps at once, each inheriting the
fields it leaves out. A step that depends on a typed step waits for its
`collect` step when it has one, otherwise for every copy (the last pass of a
`repeat`). Items are written in the file; a list known only at run time is a
batch (`raven playbook batch`).

**Settings → Workflows** edits a playbook's graph in the page: a card per step
(one step, for each item, repeat, with an optional collect step), what it
waits on, and beside it the waves it will run in -- every step of a wave at
once -- with every finding the library would refuse it for. Saving writes the
steps as written into your own library; a builtin is saved as your own copy.

## Split a large task into a plan

A goal too large for one agent can be split, recursively, into a tree of tasks:
a planner agent proposes each split, and Raven bounds and checks it -- depth,
subtasks per task, leaves in the whole plan, dependencies only between
siblings and never in a cycle -- asking once more when an answer is refused.
An optional reviewer may send the plan back. The plan is saved under
`.raven/plans/<plan>/` (`tree.json` and a readable `plan.md`).

```bash
raven playbook decompose "Add offline sync to the notes app" --depth 3 --max-leaves 20
raven playbook decompose "..." --reviewer Raven-Research --dag   # the leaves as run_subagent_dag nodes
```

To plan and carry it out in one go, the `decompose` scenario runs the leaves
level by level -- the leaves of one level at once, each handed the results it
waits on, a leaf whose prerequisite failed skipped -- and has a synthesizer put
the results together:

```bash
raven scenario run decompose --arg goal="Add offline sync to the notes app" --arg depth=2
```

## Give an agent an area to own

An agent can own a part of a project rather than take one task at a time. An
area names its owner, what it covers (path patterns and topic words) and a
charter -- the standing instructions for what the owner looks after. Areas live
in `<RAVEN_HOME>/areas.json` and outlive every conversation.

```bash
raven areas assign routing --agent Raven-Code -p raven/routing -t tiers --charter "Keep the tiers in step with the providers."
raven areas who raven/routing/tiers.py
raven areas run routing --task "Add a rung for the new model"
raven areas log routing
```

Every run in an area is journaled, and the owner is handed its charter and its
latest entries before the next one -- including a task the main agent spawns
to it that is about the area -- so it picks up where it left off in any
conversation. With `--every 6h` the gateway also runs the owner's standing duty
on its own; a line of the report that starts with `ATTENTION:` is raised on the
attention queue. Nothing runs until an area is assigned, and an area without
`--every` runs only when asked.

## Read CI and fix a red build

Raven reads GitHub CI -- check runs (GitHub Actions and other apps) and commit
statuses -- for a branch, a commit or a pull request, and explains each red
check with its annotations and the error lines of its job log. It only reads:
nothing is pushed or re-run. Set `GITHUB_TOKEN` for a private repository, for
job logs, and for waiting (an anonymous caller gets sixty requests an hour).

```bash
raven ci status                 # the checkout's origin and branch
raven ci status --pr 42 --wait 900
```

The exit code is 0 for green, 1 for red, 2 for still running or no checks.
To let agents read CI themselves, turn on the `ci_status` tool with
`tools.ciStatus: true`; it is off by default.

The `ci-fix` scenario closes the loop: it waits for CI, and while it is red
hands the report to an agent that fixes the code, commits and pushes to the
same branch, up to `cycles` times. The push is the agent's own, under its own
permissions; an agent that may not push says so and the run stops.

```bash
raven scenario run ci-fix --arg fixer=Raven --arg cycles=3 --arg task="what the branch is for"
```

## Check results and diagnose problems

Start with `raven status` and `raven doctor`. A configured provider, a listed
agent, or a successful handshake does not establish that a real task succeeded.
Check the final answer, generated files, and relevant tests or task evidence.

For a stalled task, check permission requests, child-agent readiness, provider
errors, and the run's progress before retrying. A retry can repeat external side
effects. Use `raven tracing` and `raven trajectory --help` for diagnostics, and
redact private material before sharing it. [Trajectory debugging and replay](trajectory-debugging.md)
walks through capturing evidence and reproducing harness behavior without
repeating recorded external actions.
