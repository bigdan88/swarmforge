# Web interface handoff — November 22, 2026

## Start here

Continue the existing project **SwarmForge modern web interface**, local draft ID
`65bd72700145`, with a $100 total API cap. Read [the implementation guide](WEB_INTERFACE.md),
`AGENTS.md`, `PROJECTS.md` and `PROJECTS_DESIGN.md` first. This handoff is context, not launch
approval. The project remains drafting, with no queued tasks or paid calls initiated here.

The initial read-only Work slice is implemented locally. Do not rebuild it from scratch.
Inspect the actual working tree: substantial unrelated execution-policy, model-team and lead-action
changes were already present. Preserve them. The migration changes are not committed or published
yet, so a remote-only swarm must first receive the reviewed implementation through GitHub. Do not
assume its repository snapshot includes these local files. Do not auto-launch against stale code.

## Implemented

- Optional `swarmforge.webapp` module with a loopback-only FastAPI launcher.
- Bounded, read-only SQLite projections for queue items and unattached runs.
- React/TypeScript/Vite Work UI, search/status filters, pagination, task summaries,
  detail selection and URL state, four-second refresh, light/dark themes and phone layout.
- Existing artwork, API origin/host checks, public-field allowlists and explicit error states.
- Static asset packaging, optional Python dependencies, frontend lockfile and CI build/test wiring.
- Typed API response schemas and browser validation, including incompatible-response errors.
- Read-only evidence detail: shared persisted allowance, charged/reserved/available spending, run usage, acceptance checks, test results and recent events with reconnect cursors.
- Preview feature-history entry, user guide and meaningful API regressions.

Files specific to this work are `frontend/`, `src/swarmforge/webapp/`, `tests/test_webapp.py`, `tests/test_web_artifacts.py`,
`scripts/benchmark_web_queries.py`, this document and `docs/WEB_INTERFACE.md`.
There are small integration edits in `.gitignore`, `.github/workflows/checks.yml`, `README.md`,
`pyproject.toml` and `resources/feature-history.json`. The latter three also had unrelated edits
before this migration. Review and isolate only the intended changes when committing.

## Initial slice verification and limitations

- Seven new API regressions pass. The existing new-run form test also passes, eight total.
- Frontend TypeScript check and production build pass.
- Documentation checks, fatal-error Ruff checks and workflow-scoped mypy checks pass.
- Built a Python wheel and confirmed it includes frontend HTML, CSS and JS.
- Browser-checked search, status filtering and task detail; reviewed light/dark and 390px layouts.
  Phone document width equaled viewport width, without horizontal overflow.
- A 10000-item disposable history returned a 25-row page with a local median query time around
  16ms over 25 samples. Reproduce with `python scripts/benchmark_web_queries.py`.
  This measures the read projection only, not end-to-end UI speed or improvement over Streamlit.
- A broader dashboard test run was interrupted after a background-launch failure. Isolated
  `tests/test_dashboard.py::test_launcher_starts_background_run` expects FAILED at planning,
  but the current local execution-policy changes produce BUDGET_EXCEEDED / shared task spending
  allowance exhausted. This path does not import the new web interface. Resolve with the owner of
  those execution-policy changes; do not weaken allowance policy or silently change expectations.
  The complete dashboard suite and full CI have not been shown passing.
- FastAPI's test client emits a dependency deprecation warning about httpx; checks pass.
- Browser evidence, local fixture databases and build artifacts are under ignored
  `.swarmforge/ui-migration/`. Keep them and credentials/runtime data out of commits.

## Next work

Source publication, CI and browser performance parity still gate milestone one. The guide now has
checkable journeys and a reproducible legacy data-assembly comparison on queue-only and mixed histories.
Typed response contracts and mixed query-plan review are implemented. Responses remain bounded,
but global counts and sorting scan history. Work offset pagination is not an event cursor.

The first milestone-two evidence slice is implemented locally, without enabling controls. Retained report text and a bounded PNG gallery are now implemented too; rich report rendering
and older evidence browsing remain to implement. Before adding controls,
reuse existing application services for mutations, add
idempotency and authorization protections, and test duplicate requests before enabling controls.
Preserve project fixed-spec guards, merge barriers, launch approval and spending accounting.
No paid builds or real repository creation/deployment as tests. Keep Streamlit available.

Do not mark any milestone complete based solely on this handoff. The project spec's actual checks
and normal merge/evidence barriers still determine completion.


## Continuation review - November 22, 2026

The project has five interface milestones, separate from the earlier execution roadmap. Its saved
state was inspected read-only: drafting, no approval, $100 cap, zero queued project tasks and no
project planning calls. No launch, paid call, live project change or publication was performed here.

The attachment query now builds one non-NULL set rather than a correlated scan for every run.
Task detail uses the queue primary key. NULL legacy timestamps and skipped tasks are supported.
The new evidence API reads only explicit columns from SQLite. It never initializes execution policy,
creates an allowance, follows report paths or returns raw event payloads/model transcripts. Legacy
history without execution tables reports no shared allowance. Pending reservations reduce the shown
available allowance; reported, estimated and unavailable run costs remain distinct.

An event request starts with the latest 50 rows. Subsequent requests use event IDs, return at most
100 rows and expose a backlog flag. The browser deduplicates overlapping responses, retains at most
200 events, aborts requests on selection changes and preserves its last successful evidence on error.
Acceptance/test evidence is bounded, with visible truncation/remaining-history notices.

Local verification now includes 14 web API/query/benchmark regressions and four frontend contract/
reconnect-window tests, TypeScript and the production build, documentation and fatal-error Ruff checks.
Tests used disposable data and a temporary restricted-host async adapter; no paid models/repositories.
The system Python lacks the workflow's pytest async/parallel plugins, Streamlit and mypy. These local
checks are not a full CI pass. The initial background-launch regression above remains unresolved.

The browser bridge failed to attach to a new local tab, and local HTTP requests from this execution
host were denied socket access. New evidence-panel desktop/light/dark/390px screenshots could not be
verified here. Previous screenshots cover the initial Work slice only. This remains an explicit review
gate; do not claim the new panel has passed visual or end-to-end browser checks.

The legacy baseline measures actual Work assembly helper bodies without executing dashboard startup.
On the documented 10000-row fixtures (25 warm samples), medians were 140.41 ms legacy / 28.90 ms web
for queue-only and 220.92 ms / 114.75 ms for mixed. Legacy represents only the latest 300 detached
runs; the new projection represents all 15000 unique items. This is not a browser speedup claim.
See the guide for reproduction and remaining global scans.

Remote main does not contain the web module. Connected GitHub access is read-only. A UI-only review
patch and manifest are prepared under ignored `.swarmforge/ui-migration/review/`, based on local
commit `c63c06d42c11c177ea8b487ac9fefca55c5878ad`. They isolate UI files/integration hunks from the
execution, lead-action and model-team changes. Review/rebase this source, publish through an authorized
GitHub write path and obtain passing CI before starting any remote work. Do not auto-launch from the
old remote snapshot. No milestone was marked complete.


## Retained artifacts continuation

Task details now include a retained report viewer and screenshot gallery. Files are read beneath
an explicit runs root (default: `runs/` beside the selected database); database `run_dir` values and
report links are never followed. The launcher accepts `--runs-root` for existing moved archives.
Report responses stop at 256 KiB with a shortening notice; PNG reads stop at 8 MiB and reject
invalid signatures/headers or dimensions above 50 million pixels or 50000 pixels on either edge. Directory scans and galleries
are capped, and symlinks, Windows reparse points, hard links and special files are refused.
Missing and unavailable artifacts have distinct states. All artifact responses are uncached.

The browser binds artifact requests to the selected attempt, handles 409 changes, loads images only
when the gallery opens, and displays reports as inactive text. Refreshing retained files reloads
an open report. These are read-only controls; cancel/retry/approval remain disabled in the web UI.

Local Python verification covers 19 passing web/evidence/artifact/benchmark checks and one skipped
symlink integration check because this Windows host cannot create symlinks. Reparse-point refusal,
hard links and permission errors are tested separately. Frontend contract checks cover bounded
artifact payloads and unsafe image names. No live project/runtime settings were changed.

Browser verification was attempted through the app preview and native browser fallback. Computer
Use ended because it could not determine the browser URL confidently enough to enforce policy.
No further browser input was attempted. New panels remain visually unverified in desktop, both themes
and phone width; do not treat the initial slice screenshots as proof for these additions.

Broader checks from the preceding continuation: 45 affected fast checks passed, 16 were deselected
(slow tests and one Streamlit-dependent UI check). An earlier broader invocation exposed missing
Streamlit and a sandbox-blocked fixture Git clone; these do not establish a full CI pass. Workflow
mypy could not run because it is absent from this Python environment. The old background-launch
regression is still not resolved by this interface work. Source publication, CI and normal milestone
review/launch barriers remain required.


## React workspace continuation

The local implementation now has a React application shell and working navigation for Work, New task,
Projects, Chat, PRs, Failures, History, Knowledge, Outcomes, Teams, Settings and What's new. Common human
controls use a new shared `services/dashboard.py` dispatcher: cancel/retry/approval, queue start/pause,
project draft/save/launch/pause/cancel/checkpoint replies, draft lead conversation and general chat.
The launcher is still read-only by default; `--allow-actions` explicitly opens the writable Store and
its durable request journal. No worker/model/project starts on page load. Configuration and control
DB identity are checked. Signed expiring cookies, separate CSRF, exact Origin, JSON/size bounds and
persisted idempotency records protect writes; uncertain outcomes remain held instead of being replayed.

`python -m swarmforge.webapp --demo` starts an interactive disposable workspace with fake model/worker
adapters and a visible demo banner. It does not read live configuration, create real repositories or
make model calls. The demo and API tests assert no model network or subprocess launch occurs.
Normal configured use and its installation steps are in the implementation guide.

Small integration changes in shared project services add optional spec-version checks at atomic draft
save and launch. The interview can use the same guard before accepting a model's draft update. All
existing callers retain their defaults. The UI review artifact copies only these individual functions,
not unrelated execution/lead/model-team edits in the same files. Web retries require durable execution
support; an isolated UI-only installation without that implementation keeps retry controls disabled.

Latest affected local verification: 94 Python checks passed, one host-symlink check skipped, 16 slow/
Streamlit-dependent checks deselected. Nine frontend checks passed before the final route-rendering
expansion, plus TypeScript/production builds. Follow the current review manifest/logs for final counts.
Tests use fake models/repositories and disposable data; they are not a full remote CI pass. Rendering
React markup without a DOM is semantic component evidence, not theme/phone browser evidence. Browser
verification remains blocked as described above. The original dashboard launch regression, missing
workflow mypy/Streamlit dependencies and source-publication gates remain outstanding.

The guide now lists concrete remaining parity boundaries (advanced direct runs, attachments/streaming,
configuration editing, full benchmark suites, note editing/nested notes, archive/retry folding and PR
mutations). The React version is a usable opt-in local preview, not a claim of complete Streamlit parity,
completed migration milestones, a default-interface switch or launch approval for project 65bd72700145.
No live project state, credentials/runtime settings, Git index or remote repository was modified.


Final local checks for this continuation: 95 affected Python tests passed, one symlink integration
check skipped due to host permissions, and 16 slow/Streamlit-dependent tests deselected. Ten frontend
contract/component checks, TypeScript/production build, fatal-error Ruff, documentation and whitespace
checks passed. The UI-only source snapshot passed 30 web checks with two explicit skips (host symlinks
and independent durable-retry code not included in the UI patch). Its wheel includes the React bundle,
interactive demo, session protection and shared action dispatcher. See the current ignored review
manifest for hashes and final artifact verification. Local mypy is unavailable; full CI, browser/theme/
phone evidence and source publication remain unverified. No migration milestone was marked complete.


Startup dependency follow-up: the user's `(swarmforge)` Conda environment uses Python 3.12 and
had Uvicorn but lacked FastAPI/Pydantic. Install with `python -m pip install -e ".[interface]"`
in that same activated environment, then launch the checkout demo. The interface extra now
explicitly requires Pydantic 2. Startup checks identify missing libraries and the active Python;
unrelated application import errors are no longer mislabeled as missing optional dependencies.
These diagnostics are tested without reading live configuration or starting a worker/model.


## Browser, CI and regression continuation (Claude, November 22, 2026)

CI-equivalent verification on Linux with the workflow's own install line (Python 3.12, Node 22,
constraints file, Playwright Chromium), against an exact snapshot of this working tree, with
GitHub tokens and proxies unset like the hosted runner:
- Fast job: frontend tests (10), TypeScript and production build, docs check, fatal Ruff, workflow
  mypy (first run; passes) and `pytest -m 'not slow' -n 4`: **1334 passed, 0 failed**.
- Slow tests outside the Docker job: 28 passed. The Docker job (`test_e2e`, `test_tools_sandbox`)
  was not run.

Browser verification (closes the visual gate the earlier continuations could not run): the
`--demo` server driven by headless Chromium. All 12 pages x light/dark x 1440px/390px (48 views):
no console errors, no failed requests, zero horizontal overflow. Journeys passed: theme toggle and
persistence, search/filter with URL state, deep link plus back/forward, retained report, cancel a
queued task, retry with spending authorization, new task queued and visible, chat send/reply,
project detail. Double clicks on cancel and retry sent at most one action request.
Evidence and the reusable scripts: `.swarmforge/ui-migration/screenshots/browser-qa-2026-11-22/`
(`sweep.py`, `journeys.py`; run against `python scripts/react_dashboard.py --demo`).
Not verified: your real database (folder access was not granted), the Windows browser, and
performance of the full browser path.

Regressions found and fixed (patch: `.swarmforge/handoff-cc/handoff-fixes.patch`):
1. `llm.py` spending accounting (root cause of the old `test_launcher_starts_background_run`
   failure). Any transport error on a reserved call was charged as "uncertain", including connect
   failures where the request never left the machine; with unknown prices that is the entire
   remaining allowance. Now connect-phase errors (ConnectError/ConnectTimeout/PoolTimeout) retry as
   transient, and when no model produced a completion (never sent, busy, model unavailable, missing
   key) the reservation is released at $0 via `NothingBilled`. Read timeouts, dropped responses and
   cancellation still retain the reservation. New tests: `test_unsent_model_request_releases_its_reservation`,
   `test_sent_request_with_lost_response_still_retains_spend`. The launcher test now passes, and it
   fails again with the old llm.py under identical conditions.
2. `tests/test_speedups.py`: the repo-wait test now attaches and acquires the shared allowance the
   way `Orchestrator.run()` does (the wait loop now checks it by design).
3. Chat tests (`test_dashboard_chat.py`, `test_acceptance_chat_uploader.py`): broken on this branch
   since commit 2895b8d moved the sidebar logo to `st.image`; image assertions are now scoped to
   `at.main`. They still prove that attachment thumbnails render. (`main` itself is green.)
4. UI: the first visit follows the OS light/dark preference; an explicit toggle is saved and wins.
   The pager says "1 item".

Note: `tests/test_acceptance_chat_uploader.py` already had CRLF line endings in the working tree
(git HEAD has LF), so `git diff` shows a whole-file change. Normalize to LF when committing.
The UI-only review snapshot and manifest under `.swarmforge/ui-migration/review/` predate items 1-4
(App.tsx and ui.tsx hashes changed). Regenerate it, or commit from the working tree, before publishing.

Still open: publication (needs an owner decision and write access), Docker CI job, real-history check,
chat streaming/attachments in the web chat (milestone 3), parity items in milestone 4.
No milestone was marked complete, nothing was committed or pushed, and no paid model calls were made.
