# Proposed SwarmForge tasks after the React expansion

Reviewed November 22, 2026. These are task drafts, not queued work or authorization to spend.
The format is accepted by `swarmforge.runqueue.parse_backlog`.

Review baseline: local branch `codex/fix-project-kickoff`, commit
`f4f829f6b21b496f3ec293c3fe61dd0a138bc94e`. The working tree was clean when reviewed.
GitHub `main` was `1c3b240ab514f87d382f59785b167e0942476400`: the branches had diverged,
with 13 commits unique to the React branch and 8 unique to main. Reconcile that branch with main
through a reviewed integration before scheduling this backlog against the default branch.
Retain main's accessibility, breakpoint, retry-chain and project fixes alongside the React work.
The first task can be performed on the integration branch to restore its checks.

New capabilities already present include background chat progress and attachments, reviewed-head PR
merging, failure diagnosis, nested editable notes, settings, Speed, Benchmarks, archive and attempt
folding. They should be extended, not rebuilt. Main also contains `a11y.py`, breakpoint checks and
visual audit integration; functional browser journeys should reuse that infrastructure.

Observed verification:

- [Latest remote checks](https://github.com/bigdan88/swarmforge/actions/runs/45085944938): fast job
  passed; Docker job failed two tests, with 35 passing and one skipped.
- Local frontend: 11 tests, TypeScript and production build passed. Documentation checks passed.
- Local affected Python checks: 26 passed, one failed because CRLF left a carriage return in a note title.
  These ran with the temporary restricted-host event-loop adapter; they are not a full CI run.
- Disposable reproductions confirmed that independent same-title tasks collapse into one Work row,
  and a second action-service instance interrupts the recorded status of a live fake chat reply.
- The service-change issue below is a source-review finding: actions refresh their session independently
  of the displayed workspace. It still needs the browser regression described in its task.

Start with tasks 1-5, one reviewed change at a time. Tasks 6-8 improve everyday use; tasks 9-10
extend performance and autonomous verification. Set execution budgets when scheduling; no new
budget or launch approval is implied by this file. A live project that owns this repository should
receive in-scope tasks through its lead rather than detached runs.

---

## 1. Restore the Docker verification job

**Title:** Restore Docker CI with the shared task allowance policy intact

**Description:**
The fast job at commit f4f829f passed, but the Docker job failed
`tests/test_e2e.py::test_live_settings_and_runtime_watchdog` and
`tests/test_e2e.py::test_failed_run_gets_postmortem_and_heals_itself`.
Inspect that run's retained logs and reproduce with fake providers. The watchdog test constructs
an orchestrator without the execution setup used by a normal run; establish the supported lifecycle
before changing it. The healing test expects a planner diagnosis but receives a rule-based diagnosis.
Determine whether its fixture, finalization budget or implementation needs correction.

Likely files: `tests/test_e2e.py`, `src/swarmforge/orchestrator.py`,
`src/swarmforge/execution.py`, `src/swarmforge/postmortem.py`.

Acceptance criteria:
1. Both failures are explained and covered by deterministic tests of the intended behavior.
2. The watchdog cancels overdue work with the correct recorded reason; model diagnosis uses only
   the remaining shared allowance and falls back explicitly when it cannot run.
3. Task retries, finalization and diagnosis never allocate a replacement spending allowance or extend
   the original deadline. Missing lifecycle initialization does not masquerade as a genuine timeout.
4. The affected fake-provider tests and normal fast/Docker verification pass. Do not weaken assertions,
   skip failing tests, or increase allowances merely to turn the checks green.

Preserve project accounting, Fixed-spec protection, launch approval and merge barriers. Use fake
models and disposable repositories; no paid calls or live project changes as validation. Run the
affected checks from `.github/workflows/checks.yml` and `python scripts/check_docs.py`.

---

## 2. Make Knowledge work with Windows notes

**Title:** Parse Knowledge notes consistently with Windows and Unix line endings

**Description:**
On Windows, `tests/test_web_parity.py::test_knowledge_lists_nested_notes_with_facets_and_skips_hidden`
fails because `Top note` is returned as `Top note\r`. The reader decodes raw bytes and its heading
regex captures the carriage return. Review `split_front_matter` at the same time so metadata and
body boundaries work consistently for LF, CRLF and mixed legacy notes.

Likely files: `src/swarmforge/webapp/notes.py`, `tests/test_web_parity.py` and focused parser tests.

Acceptance criteria:
1. Explicit byte fixtures for LF and CRLF produce identical titles, tags, type and repository facets
   on every test platform. Notes with and without YAML front matter are covered.
2. Display parsing keeps heading and front-matter control characters out of metadata.
3. Conflict-detection SHA values still reflect the original bytes, and reading never rewrites vault files.
4. Saving a legitimate body does not strip Markdown horizontal rules or meaningful leading text.

Use disposable vaults and fake services. Preserve safe-file checks and stale-edit protection, run the
affected tests and documentation checks, and add a preview feature-history fix entry for Knowledge.

---

## 3. Recover only genuinely interrupted chat replies

**Title:** Keep live chat replies running when another web instance starts

**Description:**
`DashboardActions.__init__` calls `release_interrupted`, which marks every thinking request in
the database interrupted. A disposable reproduction starts a gated fake reply, constructs another
action-service instance, and observes thinking -> interrupted while the original reply still runs.
This permits another send before the original has finished and can interleave conversation messages.

Likely files: `src/swarmforge/services/chat_requests.py`, `src/swarmforge/services/dashboard.py`,
`src/swarmforge/webapp/__main__.py`, `tests/test_web_chat.py`.

Acceptance criteria:
1. Persist and verify request ownership using a process-instance identity and a bounded recovery
   mechanism; another live instance cannot release a reply it does not own.
2. A genuinely dead owner becomes visibly interrupted after verified recovery, without an automatic
   duplicate model call. A late completion cannot overwrite the recovered request's terminal state.
3. One pending reply per conversation is enforced transactionally across separate Store connections.
4. Tests with two service instances and gated fake replies cover live ownership, dead-owner recovery,
   duplicate sends and late completion. No real model or server process is required for these tests.

Preserve existing idempotency, spending consent and conversation retention. Run affected web/chat
checks and docs checks; update the preview feature history and recovery guide.

---

## 4. Fold actual retries rather than matching titles

**Title:** Group Work attempts by persisted task lineage

**Description:**
`WorkReader.list(fold=True)` groups by normalized title and repository. Two independent queued
requests with the same title but different bodies currently become one row with two attempts.
Use persisted execution/retry lineage instead. Existing `execution_id`, `retry_of` and project-task
relationships should be inspected before introducing another identity system.

Likely files: `src/swarmforge/webapp/queries.py`, `src/swarmforge/webapp/contracts.py`,
`frontend/src/Work.tsx`, `frontend/src/contracts.ts`, `tests/test_webapp.py`.

Acceptance criteria:
1. Independent same-title tasks remain separate, including when their bodies also happen to match.
2. Genuine retries share one group even if a later attempt has a changed title; unrelated projects
   never share a group. Legacy work without proven lineage remains individually visible.
3. Attempt counts, archive/filter behavior, pagination and deep links identify the actual records;
   the UI explains any distinction between global attempt counts and displayed task counts.
4. Read-only browsing remains read-only. Tests cover independent tasks, genuine retry chains,
   legacy rows, project boundaries and selecting an older attempt.

Preserve original task allowances and project accounting. Use disposable databases, run web contract
and frontend checks plus docs checks, and update the preview feature history.

---

## 5. Keep browser actions bound to the workspace being viewed

**Title:** Detect service and workspace changes before executing browser actions

**Description:**
The React shell loads its session once, while `performAction` and attachment upload fetch a new
session before every write. If a demo is replaced by a configured server on the same origin, an
old page can retain its demo banner and drafts while obtaining credentials for the replacement.
Introduce an opaque workspace identity and service-instance identity, and bind actions to the
workspace the page actually displayed. Do not expose configuration or absolute database paths.

Likely files: `src/swarmforge/webapp/app.py`, `src/swarmforge/webapp/auth.py`,
`frontend/src/App.tsx`, `frontend/src/api.ts`, draft consumers and their tests.

Acceptance criteria:
1. Replacing demo A with workspace B on the same origin prevents actions and uploads from the old
   page until the user reloads and reviews the new workspace. Spending confirmations are reset.
2. Drafts, staged attachments and pending idempotency keys are namespaced by workspace. Same-workspace
   restarts retain recoverable drafts but visibly refresh server/session state.
3. Stale requests fail on the server as well as in the UI. Completed action replay remains safe
   within its original workspace and existing Origin/CSRF checks remain in force.
4. Browser/API regressions cover demo-to-real replacement, two workspaces, server restart and an
   interrupted action, using disposable stores and fake workers only.

Run affected API/frontend checks and docs checks, retain Streamlit and existing approval/budget
barriers, and add a preview feature-history entry describing the workspace-change behavior.

---

## 6. Retain and automate the browser journeys

**Title:** Run the React demo's core browser journeys in CI

**Description:**
Current browser evidence and scripts are under ignored `.swarmforge/ui-migration/screenshots/`.
The committed frontend tests validate contracts and server-rendered markup, not interactive browser
behavior. Move reusable test logic into tracked tests and run it against a disposable demo in CI.
Reuse Playwright and main's accessibility/breakpoint infrastructure instead of creating another stack.

Likely files: tracked browser tests, `frontend/tests/`, `.github/workflows/checks.yml`,
`src/swarmforge/webapp/demo.py` and `docs/WEB_INTERFACE.md`.

Acceptance criteria:
1. Search/filter/deep links, duplicate-action protection, authorized fake chat with attachment,
   queued task creation, project draft/launch/checkpoint, note editing and stale PR-head rejection
   have browser tests using fake adapters. Keyboard-only navigation is included.
2. All 13 current routes are smoke-checked in both themes at desktop and 390 px, with assertions
   for horizontal overflow, visible errors, uncaught exceptions and failed local requests.
3. Failure screenshots, console/network details and test results are retained as CI artifacts.
   Tests have bounded waits and always stop the demo process.
4. The harness cannot select a live database, provider, worker or repository. Document one command
   to reproduce the same journeys locally and refresh stale parity claims in the guide.

Keep all existing spending/launch/merge checks. Run affected checks, documentation verification and
the new browser job. Runtime screenshots remain artifacts rather than committed generated files.

---

## 7. Plan and review a queue in React

**Title:** Import and edit a reviewed backlog from the React queue

**Description:**
React can enqueue one new task, but the existing Markdown backlog importer and queued-task editing
are still outside this interface. Add a backlog preview with per-task descriptions, workflow/team,
allowance and time limit, ordering and a displayed sum of the configured task allowances. Reuse
`runqueue.parse_backlog` and existing application services rather than reimplementing scheduling.

Likely files: `frontend/src/Forms.tsx`, `frontend/src/Controls.tsx`,
`src/swarmforge/services/dashboard.py`, API contracts and queue/web tests.

Acceptance criteria:
1. Pasting a Markdown backlog produces an editable preview without queue writes or worker starts.
   Parse errors identify the affected task and preserve all browser draft text.
2. A separate reviewed, authorized enqueue action persists the selected tasks atomically. Duplicate
   submissions replay the same result; changing the payload requires a new review.
3. The UI states whether an already-running worker may begin the newly queued work. Saving a preview
   never starts it, and spending approval stays explicit. Existing project-owned tasks stay with the lead.
4. Ordinary queued tasks can be edited/reordered only while eligible. Running work, acquired allowances
   and project merge barriers cannot be bypassed. Concurrent edits produce a visible conflict.

Use fake models/workers and disposable stores. Run queue/requeue/project invariants, affected web and
frontend tests, docs checks and theme/phone journeys. Update the preview feature history.

---

## 8. Make React startup predictable

**Title:** Add a supported React launch command and verify packaged assets

**Description:**
React is still launched through a checkout script or Python module; `swarmforge dashboard` opens
Streamlit. Add an explicit `swarmforge web` command using the existing loopback launcher, with useful
dependency, missing-assets and occupied-port diagnostics. Preserve the existing dashboard command.
Verify the installable distribution includes a bundle produced from the matching frontend source.

Likely files: `src/swarmforge/cli.py`, `src/swarmforge/webapp/__main__.py`, `pyproject.toml`,
release/build wiring, `tests/test_web_launcher.py`, README and interface guide.

Acceptance criteria:
1. `swarmforge web --demo --port PORT` and the corresponding configured/read-only/control modes reuse
   the existing launch semantics, bind to loopback and show the actual URL after readiness.
2. Dependency, missing-build and port conflicts provide actionable diagnostics. No existing process
   is terminated and no paid model/worker/project work starts as a consequence of opening the UI.
3. CI builds an isolated wheel after the frontend build, installs it in a clean environment and
   verifies its HTML/CSS/JS and demo API with fake services. Ordinary wheel use needs no Node server.
4. A single documented checkout build/launch path works on Windows and Linux; default-interface
   selection and any later default switch remain a separate user decision.

Use disposable fixtures, local builds and fake adapters. Run launcher/CLI checks, docs checks and
appropriate packaging/frontend checks. Update the preview feature history.

---

## 9. Keep large histories responsive

**Title:** Measure and reduce repeated history scans in the React Work page

**Description:**
Work returns bounded pages but repeats union, filtering, grouping, count and sorting work on each
refresh. The current benchmark predates archive/attempt folding. Extend
`scripts/benchmark_web_queries.py` to representative mixed histories with retries, archived runs,
project tasks and costs, then use query plans and measurements to reduce the dominant scans.

Likely files: `scripts/benchmark_web_queries.py`, `src/swarmforge/webapp/queries.py`, related
indexes/migrations only if justified, Work query tests and the interface performance guide.

Acceptance criteria:
1. Disposable 10,000- and 100,000-item mixed fixtures produce before/after median and p95 query
   measurements with fixture details, source revision and query plans. No live history is benchmarked.
2. The implementation addresses the measured bottleneck, with correctness comparisons for filters,
   lineage groups, counts and archive behavior. Any cache has explicit freshness and invalidation rules.
3. Opening one task still uses its primary key, response sizes remain bounded, and browsing does not
   create/migrate a database. Existing offset pages are not described as event cursors.
4. Preserve raw evidence and original allowance accounting. Document measured tradeoffs without
   presenting query-only timings as end-to-end browser improvements.

Run affected query/contract tests, queue/project tests if persistence changes, frontend checks where
needed and docs checks. Update feature history only for a demonstrated user-visible improvement.

---

## 10. Give the project lead functional browser evidence

**Title:** Execute bounded browser journeys requested by the project lead

**Description:**
`request_browser_verification` still records a blocked/unverified result because no functional
journey runner is connected to the project lead. Existing screenshots, accessibility audits and
breakpoint checks are useful infrastructure but do not prove a user can complete a workflow.
Implement a narrow first version of typed journeys tied to approved milestone checks and the exact
tested revision. Reuse existing sandbox browser/process tools, evidence retention and repair findings.

Likely files: `src/swarmforge/projects/actions.py`, `src/swarmforge/projects/decisions.py`,
`src/swarmforge/projects/service.py`, `src/swarmforge/visual.py`, a focused journey service and tests.

Acceptance criteria:
1. A validated journey can open the sandbox preview, fill/click named controls and assert observable
   results. It has bounded steps, timeout and artifacts, and respects cancellation and the shared
   task/project allowance. It cannot invoke arbitrary external URLs or host commands.
2. Persist passed/failed/unverified results with check IDs, tested commit, step evidence and relevant
   screenshots/errors. Missing/stale artifacts or an unexecuted journey never count as passing.
3. A failed journey can feed the existing bounded repair path; verification neither launches a new
   repository nor amends Fixed, grants approval, replenishes a budget or bypasses the merge checkpoint.
4. Local fixture apps demonstrate a successful flow, a broken flow, timeout, restart and stale-evidence
   rejection. Project invariant tests cover the resulting transitions. No real model calls or live
   project launches are validation, and existing manual checks remain available when a journey is unsupported.

Run affected project/queue/execution and browser tests, docs and workflow checks. Retain Streamlit,
normal publication barriers and evidence; update the preview feature history and lead-action guide.
