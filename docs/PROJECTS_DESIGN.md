# SwarmForge Projects: adaptive multi-milestone builds

Nov 2, 2026 · @Dan

## Summary and goals

SwarmForge should build a whole app from one approved spec by planning in rolling waves: fixed milestones, a short detailed horizon, and a replan after every merged task. Today a big request becomes \~20 sequential tasks planned up front, and tasks 4–20 are built on what the planner assumed tasks 1–3 would produce. This feature adds a **Project**: a kickoff chat that writes a spec, one human approval to launch, a "lead" step that replans from the real code after each merge, and a human checkpoint at the end of each milestone.

This doc is written for an implementing agent that has not seen the SwarmForge codebase. It describes behaviour and the integration points; the implementer should read the named modules before changing them.

**Goals**

- One short chat turns a goal into a written, approved spec with milestones and checkable "done when" criteria.
- After Launch, the swarm works unattended until a milestone is done, replanning freely inside the spec.
- The plan always comes from the repository as it actually is, never from earlier guesses.
- The human is interrupted rarely, and each interruption carries a concrete proposal.
- New and existing repositories are both supported.

**Non-goals**

- Replacing single tasks. A one-off task still goes through the normal queue.
- Fully parallel development of a project. Parallelism stays conservative (see the lead).
- A new agent runtime. Projects reuse runs, the queue, auto-merge and the existing roles.

## Context: what exists today

SwarmForge is a Python 3.12 package (`src/swarmforge`, repo `bigdan88/swarmforge`) with a Streamlit dashboard, a SQLite store and models served by DeepInfra. Projects should be built from these parts rather than beside them.

| Part | Where | What a Project uses it for |
| --- | --- | --- |
| Run | `orchestrator.py`, started by `launcher.start_run` as a detached `swarmforge run` process | Each project task is one ordinary run: plan, develop, test, review, repair, open a PR |
| Queue and lanes | `runqueue.py`, table `queue_items`, one background worker | Project tasks go into a lane named after the project, so they run in order and beside other lanes |
| Auto-merge | `automerge.py` (rules, judge model, fix-up runs) | A task counts as done only when its PR is merged into main |
| Chatbot | `chatbot.py`, conversations stored in the DB, Chat page in `dashboard.py` | Hosts the kickoff interview and the checkpoint replies |
| Roles and models | `swarmforge.yaml` (`roles:` planner, developers, reviewer, tester, librarian, designer) | The lead is a new role, configured like the others |
| Screenshots | `visual.py`, `designer` vision role | Milestone reports show the app in light and dark |
| Knowledge vault | Markdown notes written by the librarian | General lessons; the project spec is separate and per project |
| GitHub | `github.py`, `prs.py`, `repos.py` | Creating a repo, branches, PRs |
| Store | `db.py` (`Store`: runs, events, interventions, kv, queue\_items, conversations) | New tables for projects and milestones |

Two behaviours matter most here. First, a run already supports `--continue-from` (seed from an earlier run) and self-healing retries. Second, the repo lock (`same_repo` setting) lets several runs share a repo, so the lead must keep a project's own tasks sequential unless it marks them independent.

## User workflow

The human makes decisions at two points only: approving the spec at launch, and replying at each milestone checkpoint. Between them the lead loops on its own, and escalates only in the rare cases described below.

&#91;embedded content: project workflow · 2 human steps, 1 replanning loop\]

The highlighted boxes are the human's; the loop on the right runs unattended until the milestone's checks pass.

**The kickoff chat** is an interview, not a form. The chatbot should:

1. Ask who the app is for and the 3–5 things those users must be able to do; these become the milestones.
2. Ask what is explicitly out of scope.
3. Ask for hard constraints: stack, hosting, new or existing repository, budget, deadline.
4. Turn each milestone's goal into checkable done-when statements, pushing back on vague ones.
5. Ask where the swarm may choose freely and where it must ask.
6. Propose the milestone split itself and let the human edit it, then estimate cost and time per milestone.
7. Show the finished spec as a rendered page with one **Approve and launch** button. Nothing touches any repository before that press.

## The project spec

The spec is one Markdown file per project, committed to the project's repository at `docs/project-spec.md`, with a fixed part only the human changes and a living part the lead maintains. It is the single source of truth: every task the lead creates gets it as context, and git keeps its history.

**Fixed part** (written in the kickoff chat, approved at Launch)

- Goal: one paragraph.
- Users: who it is for.
- Milestones: 3–6, in order. Each has a name, a one-line goal and 2–5 "done when" checks phrased so they can be verified ("a new user can sign up, log in and see an empty list", not "auth works").
- Out of scope: an explicit list.
- Constraints: stack, hosting, the repository (new or existing, by name), budget, deadline.
- Latitude: where the swarm may choose freely ("any UI library") and where it must not ("ask before changing the data model").
- Estimates: rough cost and time per milestone, written by the kickoff chat.

**Living part** (maintained by the lead)

- Current state: what exists now, refreshed after each merge.
- Decisions made: dated, one line each, including how an ambiguous line of the spec was read.
- Plan: the short horizon (2–3 tasks) in detail, then one-line placeholders to the end of the milestone.
- Plan changes: dated log of what changed and why.
- Open questions for the next checkpoint.

The file uses fixed headings (`## Fixed` and `## Living`, with sub-headings per item) so code can parse and rewrite the living part without touching the fixed part. A write by the lead that changes any byte under `## Fixed` must be rejected by code, not just by prompt.

## The lead

The lead is a new model role that owns a project's plan between checkpoints: it plans only a short horizon, and it replans from the real repository after every merged task. It is a short model call (not a full run) with read access to the repo, the spec, recent run outcomes and the project's queue.

**When it runs**

- At Launch, to plan the first milestone.
- After each project task's PR is merged into main.
- When a project task fails for the second time.
- When a spike task finishes.

**What it sees**: the spec, a map of the repo at current main (tree, key files, how tests run), the diff and report of the task that just landed, the results of the milestone's done-when checks if run, and the remaining queue items of the project.

**What it may do** (as structured output that code applies, not free-form text):

1. Rewrite, split, add, drop or reorder **tentative** queue items in its project's lane.
2. Promote the next 2–3 items to **ready**, fully specified: title, description, acceptance criteria, files likely touched.
3. Mark items as independent of each other (allowed to run side by side) only when they touch disjoint files.
4. Update the living part of the spec.
5. Declare the milestone complete, which runs its done-when checks.
6. Escalate (next section).

**Planning rules**

- **Walking skeleton first.** The first task of the first milestone is a thin end-to-end version: every layer connected, runnable and tested, with stub data. Later tasks thicken it.
- **Spikes for uncertainty.** When the lead is unsure something works (a library, an API, a performance limit), it schedules a short spike: a single-agent run with a small budget whose output is a finding written into the spec, not merged code. Then it plans the real task.
- **Failure feeds the plan.** After a second failure of the same task, the lead decides: split it, rewrite it, defer it within the milestone, or drop it if it is no longer needed. It does not just retry.
- **Plan from main.** The lead never plans the next task until the previous one is merged. A PR held by auto-merge pauses the project's lane until it is merged or replaced by its fix-up.
- **Horizon only.** The lead never edits milestones, done-when checks or anything else in the fixed part.

## When the lead stops early

Between milestones the lead stops and asks the human in only three cases: it wants to change the fixed part of the spec, a milestone is about to exceed 2× its budget estimate, or the same problem has failed three times. Everything else it decides alone and records under "Decisions made".

**Changing the fixed spec is a last resort.** Before asking, the lead must work down this ladder and record what it tried at each step:

1. **Solve it within the spec**: another implementation, library or approach.
2. **Run a spike** to check whether the blocker is real.
3. **Reorder or defer** the troublesome piece within the milestone and build around it.
4. **Use the latitude** the spec grants.
5. **Escalate with a concrete proposal**: "Milestone 2 says X. I tried A, B and C (results). I propose Y, which costs Z. OK?"

Three guards keep escalations rare and useful:

- **Show the work.** An escalation is structured output with the ladder steps attempted. Code rejects it back to the lead when fewer than two of steps 1–4 were tried, without notifying the human.
- **Interpretation is not a change.** If a line of the spec is ambiguous, the lead picks the most reasonable reading and logs it; only changes that contradict what the human wrote count as spec changes.
- **Waiting is per project.** An escalation pauses only that project's lane; other lanes and single tasks keep running. The human answers in the project's chat.

## Milestone checkpoints

A milestone ends when the lead declares it complete and its done-when checks pass; the project then pauses and the human gets a short report in the project's chat. Checks the lead cannot verify automatically are marked "needs you" rather than passed.

The report contains:

- What was built, with screenshots in light and dark (existing `visual.py` and designer role).
- Each done-when check: passed, failed, or needs you, and how it was verified (test name, script, screenshot).
- What the lead changed from the plan, and why (from the plan-changes log).
- Cost and time against the milestone's estimate.
- The proposed plan for the next milestone.

The human replies in the same chat in plain words: "continue", or a correction ("group the dashboard by date first"). The chatbot turns a correction into either a living-spec note for the lead or, when it changes the fixed part, a spec edit the human confirms. The lead then replans and the lane resumes. After the last milestone, the report doubles as the project's final summary.

## Integration: repos, queue, data and UI

Nothing touches a repository before Launch; after Launch the swarm has full rein over the project's repository, new or existing.

**Repositories**

- **New**: the first launched step creates the GitHub repo named in the spec, then commits a README, CI that runs the tests, and `docs/project-spec.md`. The walking skeleton follows.
- **Existing**: the first step is a survey task that maps the codebase (structure, conventions, how to run the tests) into the spec's current-state section and commits the spec. Milestone 1 is planned from that survey.
- The repository is part of the fixed spec, so the lead cannot split work into a second repo without escalating.

**Queue**

- Each project gets its own lane, named after the project. Its items carry `project_id` and `milestone`.
- New queue status `tentative`: visible but never started by the worker. Only the lead promotes items to `queued`.
- The worker never starts a project item while an earlier item of the same project is unmerged, unless the lead marked both independent.
- Other lanes and single tasks run as today, within `max_parallel_queues`.

**Data model** (new tables in `db.py`)

| Table | Key fields |
| --- | --- |
| `projects` | id, name, repo, spec\_path, conversation\_id, status (drafting, running, at\_checkpoint, waiting\_for\_you, done, cancelled), current\_milestone, budget\_usd, created\_at |
| `milestones` | project\_id, index, name, status, estimate\_usd, estimate\_hours, spent\_usd, started\_at, finished\_at |
| `project_events` | project\_id, ts, kind (replan, spike, escalation, checkpoint, decision), detail JSON |

**Config** (`swarmforge.yaml`): a `lead` role (model, fallback, timeout) and a `projects` block with `horizon: 3`, `max_task_failures: 2`, `escalate_budget_factor: 2.0`, `escalate_failures: 3`.

**Dashboard and CLI**

- A **Projects** page: one card per project with status, a milestone strip (done / current / ahead), spend against estimate, and a link to its chat and spec.
- The Work page groups a project's tasks under its name, with tentative items shown greyed.
- The Chat page gets a "Start a project" action that opens the kickoff interview; the spec is shown as a rendered page with **Approve and launch**.
- CLI: `swarmforge project new|status|approve|pause|resume|cancel`.

## Risks, open questions and build plan

**Risks and guards**

| Risk | Guard |
| --- | --- |
| Thrashing: the plan keeps changing and nothing finishes | The lead edits only the horizon; every change is logged with a reason; more than 3 rewrites of the same item without progress counts as a failure |
| Drift from what the human asked for | Fixed spec is code-protected; done-when checks are written before work starts; the lead checks each plan against them |
| Planning on stale code | Next task is planned only after the previous PR is merged; held PRs pause the lane |
| Cost of replanning | One short lead call per merged task; counted against a project-wide budget |
| Merge conflicts from parallel tasks | Independence only for disjoint files, decided by the lead and enforced by the worker |
| Unverifiable done-when checks | The kickoff chat rewrites vague checks into checkable ones; anything still unverifiable is reported as "needs you" |

**Open questions**

- Can two projects run at once, and if so, can they share a repository?
- Should single tasks for a project's repository wait while that project is running, or run beside it?
- Which model serves the lead: the planner's model, or a stronger one given how much rides on it?
- How should a project resume after a SwarmForge restart mid-milestone?

**Build plan** (each step shippable and tested)

1. Data model, config and the spec file format with a parser that protects the fixed part.
2. Kickoff chat: interview prompt, spec writer, rendered spec with Approve and launch.
3. Project lane and the `tentative` status in the queue worker; launch creates or surveys the repo.
4. The lead role: replan after merge, structured output applied by code, plan-change log.
5. Escalation ladder and its validation; pausing a single lane.
6. Milestone checks and the checkpoint report in chat.
7. Projects page, Work page grouping, CLI commands.

**Tests to include**: the fixed spec cannot be changed by the lead; a tentative item is never started; no task starts before the previous one merges; a thin escalation is bounced without notifying the human; a held PR pauses only its project; a checkpoint pauses the lane and "continue" resumes it. Use the existing fake-team test helpers (`tests/fake_team.py`) so no test calls a real model.
