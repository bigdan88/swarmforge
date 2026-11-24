# Validated project-lead actions (Stage 2 preview)

The project lead can select a bounded next action rather than always rewriting a plan. The model
proposes structured data; application code validates scope and executes it. Existing project launch
approval, the Fixed-spec byte guard, project/task allowances, queue lanes, tests, publication rules
and milestone merge barriers remain in force. No repository work starts during kickoff.

## Actions and outcomes

| Action | Application behavior |
| --- | --- |
| `inspect` | Read up to eight repository-relative files at the observed commit and up to four supplied project run IDs; retain bounded excerpts, hashes, test summaries and findings. No commands or writes. |
| `plan` | Create, split, revise, assign or reorder the unfinished horizon through the existing project queue. At most three ready tasks, or the configured smaller horizon. |
| `investigate` | Queue one ready spike with a hypothesis and observable result. Uses the existing single-agent workflow, has a small spending/time allowance and creates no PR. |
| `request_review` | Queue one task using the existing reviewed workflow: independent review, tests and any permitted in-scope corrections, with normal publication controls. This is not a read-only certification of old evidence. |
| `request_browser_verification` | Record which exact approved milestone checks need browser verification. Stage 2 has no journey runner, so the request is explicitly **blocked/unverified** and asks for a concrete human check or correction. Stage 3 supplies executable browser journeys. |
| `evaluate_milestone` | Evaluate retained merged-work evidence using the existing checkpoint path. Failed checks reject completion; unverifiable checks require human confirmation. |
| `pause` / `ask_user` | Pause only this project with a reason or concrete question in its existing conversation. Other repositories/lanes continue. |
| `escalate` | Ask for a specific scope correction after at least two distinct recorded ladder approaches. Cannot amend Fixed or approve itself. |

Every new action includes a reason and references from the supplied evidence catalog. The catalog
contains the current repository commit, recent project tasks/runs, approved milestone checks and
retained inspections. Repository files, reports and model responses are evidence rather than
authority; quoted instructions cannot grant approval, permissions, credentials or a bigger allowance.
Old `plan`, `checkpoint` and `escalate` responses remain supported; the adapter records their actual
repository reference and routes them through the same validation and journal.

`living` is optional for actions that update the plan or evaluate/escalate. When supplied, it must
preserve all required Living sections and previous decision/plan-change history. The application
preserves every Fixed byte. Arbitrary action fields, commands, repositories, approval flags and
permission updates are rejected.

## Task contracts and scheduling

Task proposals contain `key`, `title`, `description`, `acceptance_criteria`, `files`, `ready`, `kind`
(`build` or `spike`) and `budget_usd`. Optional `depends_on` contains earlier eligible keys in this
plan or completed work. Optional `replaces` contains eligible queue task IDs in this project and
milestone. Optional `max_runtime_s` can reduce the configured time allowance.

Paths must stay in the approved repository. Dependencies cannot point to unrelated, later, tentative
or cyclic work. Dispatch checks dependency evidence again; a completed build without a merged PR
does not satisfy a dependency. The lead cannot modify running tasks or completed evidence. Its
replacement horizon preserves Stage 1 allowance lineage even when it changes task keys; explicit
replacement targets inherit their original allowance too. Combining incompatible allowances pauses
for human review instead of pooling spending.

The lead and the queue share an atomic dispatch fence. One lead turn owns a project at a time;
tasks cannot start while it changes that project's horizon, and a running project task prevents a
new lead turn. Concurrent human queue/spec changes supersede an in-flight proposal. No global
lock waits on a model or repository request, so unrelated projects and queue lanes stay responsive.

Structural scope, ownership, dependencies, budgets and approvals are checked deterministically.
Whether a proposed implementation actually fulfills the goal remains subject to normal acceptance
checks, tests and independent review; a valid action is not proof of a correct outcome.

## Bounds, restart behavior and inspection

| Configuration | Default | Meaning |
| --- | --- | --- |
| `projects.max_lead_actions` | 3 | Maximum decision calls per persisted progress/reply epoch, including rejected proposals and inspections |
| `projects.lead_budget_usd` | $0.50 | Shared allowance for all decision calls in that epoch, also capped by remaining project/milestone spending |
| `projects.lead_timeout_s` | 300 | Timeout for each short decision call |
| `projects.investigation_timeout_s` | 900 | Upper bound for a spike, also limited by `limits.max_runtime_s` and its shared task deadline |

An epoch advances on actual project outcomes or human approval/replies, not on heartbeats, another
model message, a Living edit or a worker restart. The initial epoch policy and spending baseline
are persisted. A repeated successful inspection is rejected; saved inspection evidence can feed a
subsequent action without reading it again. Reaching the action/spending cap pauses with evidence.
Two consecutive invalid proposals stop the turn. Timeouts cancel the awaited model work. Unknown
request spending is retained conservatively rather than replenished. Provider-reported costs can
still cross a nominal limit by the final in-flight response, as with existing accounting.

The `project_decisions` journal retains the proposal, reason, evidence, timestamps, status and
result. Rejected, failed, blocked and superseded decisions are distinguishable from applied actions.
Queue publication and the applied decision commit together. Repository spec writes first record
execution intent. If a worker disappears during a decision or write, recovery pauses with the saved
record and does not replay an uncertain external action. Completed decisions/evidence are retained.

Find decision events in **Projects → Plan**, alongside the existing task history. For detailed
records use `swarmforge project status PROJECT_ID`; its JSON includes `lead_decisions`. History is
bounded when read; inspection excerpts and evidence catalogs are capped. No extra model calls run
on dashboard refreshes.

Opening an existing database adds the journal/job tables (lead turns are `project_lead` jobs; a claim row left by an older version is still honoured until its process exits) and nullable queue `depends_on` and
`project_action` fields. Existing tasks have no additional dependencies and retain their existing
workflow. No database migration launches work or changes an approved specification. Runtime
configuration, credentials and user databases are not part of this source change.

## Verification and remaining work

Regression tests use fake repository/model adapters and temporary databases. They cover authority
and path rejection, evidence scoping, inspection retention/reuse, investigation/review dispatch,
dependencies, immutable history, replacement allowances, merge barriers, concurrent leads/dispatch,
lost workers, failed writes, decision caps, spending, unknown billing and cancellation on timeout.
Fake-model results do not establish real-world agent reliability.

On November 22, 2026, the selected Stage 1–2 regression run passed **177 tests**, with one skip and
24 slow/environment-dependent tests excluded. This includes 33 new lead-action regressions, including
a fake-provider test of the actual lead JSON/usage contract. Async tests used the existing scratch-only
Windows IOCP/pytest adapter; normal CI remains necessary. Documentation, compilation, whitespace and
undefined-name checks passed. Ruff/mypy were attempted and remain unavailable in this environment.

Stage 1's normal CI and dashboard theme/phone review remain outstanding on this restricted host.
The same missing dependencies and network/Git restrictions limit Stage 2 verification. Browser
journeys, editable scoped memory, measured self-improvement and unified conversational notifications
remain Stages 3–6; the browser request above does not claim those journeys have been implemented.

Continuation: finish outstanding normal CI and UI verification for Stages 1–2, then implement
Stage 3's isolated browser journey runner and retained evidence using existing testing/visual
infrastructure. Preserve AGENTS.md, Fixed bytes, launch approval, merge barriers, evidence, ownership
and task/project allowances. Use fake/local fixtures and temporary state; no paid builds, real
repositories, production credentials, live project changes or deployments as validation.
