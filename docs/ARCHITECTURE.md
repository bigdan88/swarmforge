# SwarmForge architecture

```
issue URL ─▶ CLI ─▶ Orchestrator (state machine, gates) ──▶ SQLite store ◀── dashboard / status / logs
                     │
                     ├─ GitHub client ........ fetch issue, push branch, draft PR
                     ├─ GitWorkspace ......... disposable clone, integration branch, task worktrees
                     ├─ Planner agent ....... read-only tools → submit_plan (schema-validated)
                     ├─ Scheduler ............ dependency DAG, max N concurrent coding agents
                     │    ├─ Backend developer  ┐ each in its own worktree,
                     │    └─ Frontend developer ┘ built-in or Pi runtime in Docker sandbox
                     ├─ Tester agent ........ acceptance tests → criteria↔test mapping, defects
                     ├─ Test runner .......... runs the real test command, parses JUnit (deterministic)
                     ├─ Reviewer agent ...... read-only → submit_review (structured findings)
                     └─ Report ............... Markdown report + PR body from recorded telemetry
```

## Components

| Module | Responsibility |
|---|---|
| `orchestrator.py` | Owns the run lifecycle and every state transition. Applies the approval gate, budget, runtime limit, cancellation, repair-cycle limit, and the push approval. |
| `state.py` | `RunState`/`TaskState` enums and the allow-list of transitions. Illegal transitions raise. |
| `plan.py` | JSON schemas for plans, reviews and test reports; deterministic validators (unique ids, known dependencies, no cycles, allowed roles, safe paths); topological "waves". |
| `scheduler.py` | Async DAG executor: dispatches ready tasks up to `max_concurrent_agents`, marks dependents of failed tasks `BLOCKED`. |
| `agent/runtime.py` | The coding-agent runtime: OpenAI-style tool-calling loop with step/time limits, transcript capture, context compaction, cancellation and budget checks. |
| `agent/harness.py` | Shared async execution contract and cancellation before launch. |
| `services/assessment.py` | Pure evidence assessment; lifecycle transitions stay in the orchestrator. |
| `services/delivery.py` / `services/publication.py` / `services/runs.py` | Local delivery, GitHub publication/auto-fix and shared cancellation; lifecycle transitions remain in the orchestrator. |
| `dashboard_pages/` | Page renderers with explicit UI/application dependencies. |
| `effective_config.py` / `team_profile.py` | Allow-listed operational export and validated team profiles. |
| `evaluation.py` | Repeated pinned comparisons, persisted progress and total spend stop. |
| `agent/pi_runtime.py` | Optional Pi developer harness inside the sandbox, using a host model relay for metering, fallbacks and role limits; `sf` exposes SwarmForge tools. |
| `agent/tools.py` | Agent tools. File tools are confined to the worktree (no absolute paths, no `..`, no `.git`). `run_command` goes through the sandbox. Each role gets a subset. |
| `sandbox.py` | `DockerSandbox` (default) and `LocalSandbox` (explicit opt-in). |
| `workspace.py` | Git: clone, integration branch, per-task worktrees + leases, commits, serialized merges, conflict detection and the conflict-resolution worktree flow. |
| `testing.py` | Runs the configured test command, adds `--junitxml`, parses per-test outcomes, evaluates acceptance criteria from executed tests only. |
| `llm.py` | OpenAI-compatible HTTP client (DeepInfra by default), retries/backoff, per-call usage and cost with source labels, shared run budget. |
| `github.py` | Issue fetch (REST), push-permission check, safe branch push (prefix-only, no force, refuses existing remote branch), draft PR creation. |
| `automerge.py` / `prs.py` | Optional rule/judge-based auto-merge and bounded fix-up tasks; dashboard PR status and manual merge operations. |
| `db.py` | SQLite (WAL) store: runs, tasks, events, llm_calls, test_runs, findings, acceptance, interventions, benchmarks. |
| `report.py` | Execution report and PR body, generated only from recorded data. |
| `benchmark.py` | Single-agent vs swarm on identical start states with hidden acceptance tests. |
| `dashboard.py` | Streamlit control center: start runs, approvals, delegation graph, transcripts, knowledge base, benchmarks. |
| `launcher.py` | Starts `swarmforge run` as a detached background process for the dashboard; cross-platform liveness check. |
| `approvals.py` | Approval channels for the push gate: terminal prompt, dashboard button (DB request polled until decided or timed out), or pre-approved. |
| `knowledge.py` | Zettelkasten vault: note format, search, wikilinks/backlinks, hub notes, librarian note validation and writing. |

## Workflow

```
ISSUE_RECEIVED → REPOSITORY_INSPECTION → PLANNING → TASK_VALIDATION → TASK_EXECUTION
  → INTEGRATION → TESTING → CODE_REVIEW → (FIX_REQUIRED → TASK_VALIDATION → …) | APPROVED
  → PULL_REQUEST_CREATION → COMPLETED
any non-terminal state → FAILED | BLOCKED | CANCELLED | BUDGET_EXCEEDED
```

* **REPOSITORY_INSPECTION** clones the repo into `<workdir>/runs/<id>/repo`, creates the integration
  branch `swarmforge/issue-<n>-<id>`, builds a deterministic repo summary and runs the **baseline**
  tests, so pre-existing failures are visible.
* **PLANNING**: the planner explores with read-only tools and must call `submit_plan`. Its
  arguments are validated in code; rejection messages go back to the model, which retries.
* **TASK_EXECUTION**: the scheduler starts every task whose dependencies are integrated. Each task
  gets `git worktree add` from the *current* integration HEAD, so dependents see their
  predecessors' merged code. The developer agent edits files and runs tests in the sandbox, then
  calls `finish`. The orchestrator (not the agent) commits and merges into the integration branch
  under a lock (`--no-ff`, never forced). A merge conflict aborts the merge and starts a
  conflict-resolution agent in a separate worktree; the orchestrator refuses to conclude it while
  conflict markers remain.
* **INTEGRATION** verifies the integration branch actually differs from base.
* **TESTING**: the tester agent writes acceptance tests in its own worktree (a deterministic
  guard reverts any change it makes outside test files), maps criteria → test ids and files defect
  reports. Then the **orchestrator itself** runs the full test command in the sandbox and parses
  JUnit. A criterion is `satisfied` only when its mapped tests were executed and passed.
* **CODE_REVIEW**: the reviewer (different model; enforced by config validation) reads the diff
  and the integrated tree and submits structured findings.
* **Gate** (code, not LLM): approve only if the configured test command passed, no
  acceptance criterion failed, no review finding / defect at a blocking severity, and no task
  failed. Otherwise `FIX_REQUIRED`: the planner turns the problems into corrective tasks and the
  cycle repeats, up to `max_repair_cycles` (default 3), after which the run stops as `FAILED` with a
  report listing the unresolved problems. When JUnit is present, passing requires at least one
  passed test and no failures/errors. `review.require_junit` holds delivery without JUnit;
  `review.require_verified_criteria` also holds unverified criteria. Both are enabled in the example
  and local configuration; compatibility defaults in the Python config classes are false. Missing
  or skipped test mappings remain unverified even if other mapped tests pass. Criteria only a person
  can check (browser, device, live site, judgement) are recorded as `needs_you` and listed for the
  person instead; they never block (`criteria.py`). If only unproved criteria remain, the run stops
  without repair cycles (they cannot help) so the task is re-planned.
  Review blocking severities are configurable, and reviewer findings are filtered to repair scope.
* **PULL_REQUEST_CREATION**: only after approval, only for GitHub issues, only with push access,
  and (by default) only after a human answers "yes". Delivery creates a draft PR. Optional
  `auto_merge` policy can then merge eligible PRs or start a bounded fix-up run; the dashboard
  also supports manual merges. The example disables auto-merge; this checkout's local
  configuration enables it for SwarmForge itself. Updating the running checkout is separate.

## Agent runtime: why not OpenCode

OpenCode was evaluated as the runtime. SwarmForge uses its own small tool-calling loop because:

1. **Sandboxing.** SwarmForge routes commands through its sandbox abstraction. Docker is the
   default; local execution is explicit opt-in. Built-in sessions and orchestrator tests use
   `sandbox.network` (default `none`); Pi sessions use `pi.network` (normally `bridge`).
2. **Metering.** Every model call must be counted against the run budget *before* it is made and
   recorded with provider-reported cost. With an external CLI agent, per-call usage is opaque.
3. **Role-scoped tools and structured outputs.** Planner/reviewer are read-only; the tester can
   only change test files; completion is a schema-validated tool call (`submit_plan`,
   `submit_review`, `submit_test_report`, `finish`) whose rejection is fed back to the model.
4. **Model portability.** Any OpenAI-compatible endpoint with tool calling works; `swarmforge check
   --live` verifies each configured model actually emits tool calls.

The built-in loop lives in `agent/runtime.py`. Developers can instead select `runtime: pi`;
`agent/pi_runtime.py` launches Pi inside a session container and forwards model calls through a
host relay into the same LLM client. Pi receives a per-agent relay token, not the provider key.
Its own shell and the `sf` tools share that session's network. Bridge networking permits relay
access but does not enforce relay-only egress. See [SECURITY.md](SECURITY.md).

The README records the local team as of 2026-11-02. The committed example configuration has a
different team and uses the built-in runtime; model names are configuration, not fixed architecture.

## Background jobs and transactions

Background work is recorded in the `jobs` table (`src/swarmforge/jobs.py`). Each job has a kind, a
subject, one owner (a per-start process identity, its PID and host) and a lease the owner renews while it
works. The database allows one active job per kind and subject across all connections and processes.

- **States:** pending → running → succeeded / failed / interrupted / uncertain / cancelled. Terminal
  states never change; a retry is a new job that names its predecessor and counts the attempt.
- **Owner-only finish:** only the current owner can end a running job, so a process that lost its lease
  can never overwrite what recovery decided.
- **Recovery needs proof:** any process may recover a running job whose lease expired, or whose owner
  process on the same host has exited. A live owner elsewhere is never disturbed. Recovery is an explicit
  transition, recorded with its reason in `job_transitions`. Work that may already have reached the
  outside world recovers as `uncertain`; nothing is replayed automatically.
- **Hooks:** a kind registers a hook that updates its own rows inside the transition's transaction (the
  chat request the browser shows, a project's lead decisions), so the two can never disagree.

External effects (push, draft PR, merge, close) record their intent before they run and become
`uncertain` if the call fails. `reconcile.py` later asks GitHub what really happened and settles each
record as done (the real result is replayed), not done (safe to repeat once), conflict (a human
decides) or unknown (left open), keeping the evidence. Details: docs/EXECUTION.md, Reconciliation.

On jobs today: chat replies (`chat_reply`) and project-lead turns (`project_lead`, recovers as
uncertain and stops the project for a human). Runs and the queue worker still use their allowance claims
(docs/EXECUTION.md) and move next.

`Store.transaction()` is the boundary for one business operation: it takes the write lock up front
(`BEGIN IMMEDIATE`), store helpers called inside join it rather than committing, and nested calls join
the outer one. An exception rolls the whole operation back.

Network and model calls never happen inside a transaction: record the intent, commit, make the call,
then record the outcome in a new transaction (the web action journal and chat reply jobs work this
way). This is enforced: model calls (`LLMClient.chat`), GitHub API requests and `git push` raise
`TransactionViolation` if the calling thread holds a write transaction.

Operations that are one transaction each: project create, draft save, approve, pause, cancel (queued
items, running-run cancellations and the status change together), spec amendment (its checks run inside
the transaction), checkpoint reply (including reopening the lane), lead replans and fix-ups; queue item
claim, worker claim, retry (`requeue`: the duplicate check and the insert together) and environment
pause; conversation append, delete and clear; run deletion, cancellation and queue reordering. Opening a
write transaction by hand anywhere else fails `tests/test_transactions.py`.

## Data & observability

Everything lives under `workdir` (default `~/.swarmforge`):

```
swarmforge.db                       SQLite store (dashboard/CLI read this)
runs/<run_id>/repo                  disposable clone; integration branch checked out
runs/<run_id>/worktrees/<task>      per-task worktrees (removed after successful merge)
runs/<run_id>/transcripts/*.jsonl   full agent transcripts: prompts, tool calls, outputs, usage
runs/<run_id>/artifacts/            test logs and JUnit XML
runs/<run_id>/report.md             execution report
runs/<run_id>/effective-config.json operational policy/model snapshot (no credentials)
evaluations/<label>-<id>/results.json pinned comparison inputs, progress and summaries
benchmarks/*.md                     benchmark tables
```

Cost labels: `provider_reported` (DeepInfra's `usage.estimated_cost`), `estimated` (tokens ×
published price), `unavailable`. Reports keep them separate.

## Knowledge base

`knowledge.py` stores one Markdown file per note (`<YYYYMMDDHHMMnn> <title>.md`, YAML front
matter: title, type, tags, repo, run, source, created). Links are Obsidian `[[stem|title]]`
wikilinks.

* Retrieval (`Vault.search`): BM25-style keyword scoring over title (×3), tags (×2) and body, a
  boost for notes about the same repository, and down-weighting of run logs. No embeddings and
  no extra services. The selected notes go into the planner prompt inside a
  `<knowledge_base>` block marked as untrusted. Developers and the reviewer get the subsets
  relevant to them.
* Capture (`Orchestrator._capture_knowledge`) runs after the terminal state is set, so it can
  never change a run's outcome. The librarian gets a deterministic digest of the run (tasks,
  summaries, test evidence, findings, warnings, cost per role) plus the titles of related notes,
  and must call `submit_notes`. Its output is schema-validated, and links are resolved to existing
  or sibling notes. The orchestrator then writes a run-log note and regenerates the repo hub
  note.

## Dashboard-driven runs

The dashboard writes a local issue to `<workdir>/issues/<run_id>.md` and launches
`python -m swarmforge.cli run --run-id … --approval dashboard`, which runs detached from
the dashboard, with output in `<workdir>/logs/<run_id>.log`. When the gate approves and a push
is possible, the run inserts a pending row into `approvals` and waits. The dashboard shows
Approve / Decline, and the decision is recorded as a human intervention.
