# Bounded task execution and recovery (preview)

## Architecture assessment

The existing orchestrator already owns planning, development, tests, review, repair and publication.
The queue owns dispatch and restart handling. Projects add approval, a byte-protected Fixed spec,
milestone merge barriers and project/milestone budgets. The built-in and Pi harnesses share LLMClient.
These remain the execution paths; this change does not add another worker or agent framework.

Previously every retry constructed a fresh time limit and budget, multiplied by self-healing factors.
The watchdog only checked elapsed time. A restarted ordinary queue task could repeat a completed run,
and cancellation could leave child processes behind. Project recovery spending could disappear when
the queue row was repointed to a continuation without a `heal_of` link.

Stage 1 adds `execution.py` as the shared durable policy boundary: lineage allowances, exclusive
attempt ownership, model request reservations, evidence counters and checkpoints. SQLite write
transactions serialize claims and reservations across processes. Run states stay compatible; a
separate `stop_kind` distinguishes intentional cancellation, exhausted allowance, lost worker,
stalled work, uncertain external action, concurrent attempt and ordinary failure.

## Time, spending and defaults

| Setting | Default | Meaning |
| --- | --- | --- |
| `limits.budget_usd` | $20 | Total task spending across all attempts, continuations and replacements |
| `limits.max_runtime_s` | 21600 | Six hours of **wall-clock** time from the first dispatched attempt |
| `limits.no_progress_s` | 1800 | Thirty minutes without new retained code or investigation/verification evidence |
| `limits.repeat_action_limit` | 6 | Repeated identical action/results or recurring failures before stopping |
| `limits.max_recoveries` | 1 | Total stall/lost-worker recoveries across the lineage |
| `self_heal.max_attempts` | 2 | Existing automatic retry cap, within the remaining shared allowance |
| `self_heal.max_total_budget_usd` | 0 | Optional **lower** task cap; zero uses the original task budget |

Waiting in the queue before initial dispatch consumes no task time. After dispatch, repository waits,
approval waits, queue/project pauses, retry intervals and restart downtime consume the shared
wall-clock allowance. Pausing stops new dispatch, as before. It does not replenish the allowance.
Pending human publication approval is excluded from the inactivity calculation, including when the
approval finishes, but still consumes wall-clock time. Role-session timeouts and command timeouts
are additional local limits. They cannot extend the task deadline.

The legacy `self_heal.time_factor` still adjusts role-session effort; `budget_factor` remains accepted
for configuration compatibility. Neither multiplies the total task allowance. Retries can get more
agent steps or repair cycles while spending only the remainder. Reloading a changed configuration
does not enlarge existing allowances. The Work detail settings form records explicit human allowance
changes. Project task increases must go through the existing project budget checkpoint.
The existing human Retry controls also record explicitly selected total limits. Starting code from
scratch uses `retry_of` independently of the code seed; it does not create a fresh allowance.
When the project lead splits or replaces a failed task, its replacement horizon inherits the failed
task's allowance even if task keys change. Replacing an unfinished recovery horizon preserves it too.
An exhausted allowance pauses the project before another planning call. Ambiguous combinations of
multiple task allowances pause for review instead of pooling or replenishing spending.

Project and milestone budgets remain separate. A task cannot replenish either by retrying. The
project spend includes all attempts in the task lineage, including recovery and uncertain requests.

## Reservations and cancellation

Only one attempt owns a task allowance. The claim contains a random fencing token and the process
ID. An old heartbeat alone never permits stealing a claim from a live process. A live but hung
process must be cancelled or stopped before recovery. A watchdog heartbeat is not useful progress.

Within an attempt, each model request reserves spending atomically before starting. Known token
prices use a conservative text-input estimate and configured output maximum. Missing prices or image
inputs reserve the entire remaining allowance, serializing those calls. A request that cannot fit
stops before it is sent. Actual provider-reported/estimated cost settles the reservation. If cost is
missing, the reservation is charged conservatively. An interrupted or unknown request keeps its
reservation; this uncertainty is also recorded in run/project cost evidence. A transport failure with
an uncertain outcome is not automatically sent again. Provider pricing or billing can differ from
estimates; the last in-flight response can still cross the nominal limit. No further work is admitted.

The watchdog cancels the pipeline on cancellation, exhausted allowance or detected stall. The
scheduler waits for child tasks to unwind. Local commands and Pi processes kill their process trees;
Docker commands/session containers are stopped. Agent context cleanup closes browser/process
resources. Remote providers and already-dispatched synchronous GitHub/git calls cannot be recalled;
their reserved spending or pending operation intent remains available for reconciliation.

## Progress and bounded recovery

Application-observed new repository reads, changed file contents, integrated commits and structured
test evidence count as progress. Evidence is fingerprinted across attempts; repeating the same read,
test outcome or failure does not reset the progress timestamp. Model answers, process heartbeats,
arbitrary shell output and a model claiming it finished do not count by themselves. Tool transcripts,
test logs, run reports and worktrees remain on disk when work stops. Progress telemetry retains
fingerprints rather than copying tool output into dashboard status.

Inactivity or repeated evidence stops the attempt with `stop_kind=stalled`. The existing diagnosis
and retry path directs the next attempt to inspect retained evidence and change approach or narrow
the task. It shares the original allowance and consumes the recovery cap. Recurring failure evidence
is retained; unsuccessful recovery escalates instead of starting another loop. Projects retain their
additional failure/replanning limits and human escalation path.

On worker restart, terminal queue runs are reconciled without rerunning them, including ordinary
non-project tasks. A dead nonterminal worker can be recovered once from its saved run/workspace,
within the original deadline and remaining spending. The prior attempt is explicitly marked
`lost_worker`. The existing continuation mechanism seeds integrated code; planning and verification
run again against that code. Uncommitted worktrees are retained as evidence and are not silently
treated as verified results.

Push, draft PR creation, merge, superseded-PR closure and local delivery record intent before the
external operation and its result afterward. Repeating a completed operation returns its recorded
result. A call that fails leaves the operation `uncertain` (it may have failed after GitHub acted);
one interrupted by a crash stays `pending`. Neither is ever replayed blindly: reconciliation asks GitHub
what really happened and keeps the evidence (see "Reconciliation" below). Automatic held-PR fixes use
the queue so the current attempt can release ownership first.
Explicit manual publication keeps its approval path; it can publish retained approved work after
execution time expires, but cannot replay an uncertain operation.

## Reconciliation

`swarmforge reconcile` (and automatically: the queue worker and the web interface on start, a task's
next attempt before it begins, and the same web request sent again) settles every open external
operation GitHub can answer for:

| Operation | What is checked | Done | Not done |
| --- | --- | --- | --- |
| Push | the branch on GitHub, against the commit the push meant to send | branch at that commit | no such branch |
| Draft PR | pull requests from the branch | the PR (its URL becomes the recorded result) | none |
| Merge (auto-merge, web Merge) | the PR's merged state and merged head commit | merged at the reviewed commit | still open |
| Close superseded PR | the PR's state | closed | still open |
| Update branch (web) | the PR's head commit | new head since the request | unchanged |

*Done* records the real result, so a retry continues from it instead of repeating it. *Not done*
frees the operation for one new attempt. A branch at another commit, or a merge at a commit other
than the reviewed one, is a *conflict* and stops for you. If GitHub cannot be reached, or the
operation is local delivery or another web action GitHub cannot describe, the record stays open
(*unknown*). Every verdict, whichever way it went, is kept with its evidence (HTTP status, remote and
expected commits, PR state) in the `reconciliations` table; `swarmforge reconcile --list` shows what
is still open without contacting GitHub. Model calls are never reconciled: a provider cannot be asked
whether it billed a request, so an uncertain call stays charged.

## Inspecting progress and migration

Open **Work → task details → Task allowance and progress**, view **Projects** activity, run
`swarmforge status RUN` / `swarmforge status RUN --json`, or read the execution report. These use the
same persisted snapshot: task elapsed time, attempt number, remaining time/spend, last meaningful
result and its timestamp, current action, next recorded action and stopping reason. Existing detailed
events, transcripts, changes and verification evidence remain available on the run.

Opening a database adds tables and nullable run/queue lineage columns automatically. Existing records
are preserved. A legacy task first continued or recovered is attached lazily, using its original
creation time, recorded budget and known spend. This may correctly reveal that an old task has no
time remaining. No task is launched by migration, and no live project needs conversion or a new spec.

## Remaining stages and continuation

Verification on November 22, 2026: the selected execution, queue, project, state, model-accounting,
CLI status, publication-rule, summary and chat-retry regressions passed **144 tests**, with one skip
and 24 slow/environment-dependent tests deselected. This included all tests in `test_execution.py`.
Documentation checks, Python compilation, `git diff --check`, and a Pyflakes undefined-name scan also
passed. Async tests used a scratch-only IOCP timer-wakeup/async-test adapter because this host blocks
the standard Windows event-loop socketpair and lacks pytest-asyncio. This is partial validation.

The broader attempted run exposed host restrictions on Git/MSYS local clones, loopback relay
connections and long Windows paths, plus missing Streamlit. Ruff and mypy could not run because
they are not installed; installing the CI dependencies failed because network access was unavailable.
Normal fast CI, Docker checks and browser review in both themes at desktop/phone widths remain
outstanding. No production projects, real repositories, paid model builds or deployments were used.

Stage 2 now adds [validated lead actions](LEAD_ACTIONS.md), including a durable decision journal and
bounded inspection/planning turns. Stages 3–6 remain: browser journeys, editable scoped memory,
measured self-improvement and unified conversational notifications. Stage 1's progress display does
not constitute a new conversational system or proof of real-world agent reliability.

Before shipping, finish normal CI verification and review the changed Work/Projects progress
display in light and dark themes at desktop and phone width. Use only temporary databases, fake
providers and fake repository adapters. Keep the user's runtime configuration and live projects out
of validation. See `.github/workflows/checks.yml` for the required checks.

Continuation prompt:

> Continue the persistent engineering-agent assignment. Read AGENTS.md, docs/PROJECTS.md,
> docs/PROJECTS_DESIGN.md, docs/EXECUTION.md and docs/LEAD_ACTIONS.md. Preserve all unrelated local changes. Finish Stages 1–2
> verification before starting Stage 2: run the execution, queue, project, publication, harness and
> status/report regressions, documentation checks, Ruff, mypy and the fast CI suite; review Work and
> Projects in both themes at phone width using a temporary database. The restricted Windows host
> lacked pytest-asyncio/xdist, Streamlit/Playwright, Ruff and mypy; installing them failed because
> network access was unavailable. Its default asyncio socketpair and Git/MSYS local clone operations
> were also blocked. A temporary scratch-only timer-polling IOCP/async-test adapter enabled core
> fake-model regressions; it is not a replacement for normal CI. Resolve genuine failures without
> weakening assertions or policy. Then implement Stage 3 browser journeys using the existing
> testing, visual, queue and project controls, followed by Stages 4–6 in the original order. Do not launch paid models,
> create real repositories, publish deployments or modify live projects for validation.
