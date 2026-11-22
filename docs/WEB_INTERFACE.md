# React web dashboard (preview)

SwarmForge has an optional React/TypeScript dashboard served by a loopback FastAPI application.
It includes Work, New task, Projects, Chat, Pull requests, Failures, History, Knowledge, Outcomes,
Model teams, Settings and What's new. This is local, unmerged preview work; it does not establish
completion of every migration milestone. Streamlit remains available and its default command is unchanged.

## Build and open

Use Node 22.12+ and the project's Python environment:

```sh
python -m pip install -e '.[interface]'
npm ci --prefix frontend
npm run build --prefix frontend
```

From this source checkout, try the interactive demo without opening your runtime database or calling any models:

```sh
python scripts/react_dashboard.py --demo
```

The checkout launcher adds `src/` to its own import path; no editable install is needed if interface
dependencies are already available. Installed wheels can use `python -m swarmforge.webapp --demo`.

Open `http://127.0.0.1:8600`. The demo creates temporary sample tasks, project drafts, notes and
conversations. Chat and worker adapters are simulated. A visible banner identifies sample data;
sample evidence is not proof of real task execution. Changes disappear when the demo stops.
Do not combine `--demo` with database, configuration or action-mode flags. `--port 8601` is supported.

Browse the configured workspace without writes or workers:

```sh
python -m swarmforge.webapp
python -m swarmforge.webapp --db PATH
```

Enable human controls explicitly:

```sh
python -m swarmforge.webapp --allow-actions
python -m swarmforge.webapp --config PATH --allow-actions
```

The checkout launcher accepts the same flags (for example `python scripts/react_dashboard.py --allow-actions`).

Controls open the configured database through the existing Store, perform its normal schema migrations
and add a durable action-request journal. Browsing without `--allow-actions` opens SQLite read-only,
never creates/migrates a database and starts no worker. Model/worker controls require configuration;
with `--db PATH --allow-actions` alone, database-only controls are available. A supplied configuration
and control database must refer to the same workspace. No task, project, model or worker starts on page load.

The existing `swarmforge dashboard` command remains available. Stop this web process to roll back.
For frontend development, run the API on 8600 and `npm run dev` in `frontend/`. Vite proxies `/api`
and `/brand`. The development proxy is intended for browsing; protected writes require the served
same-origin production interface. Ordinary use needs no Node server. Build frontend assets before
building a Python wheel; generated `resources/web` assets are ignored by Git and included in wheels.
A source-only installation without assets gives a helpful 503 rather than an empty page.

### Missing interface dependencies

React runs the interface in the browser. Python runs the SwarmForge API and serves the built
JavaScript/CSS; Node is needed for the frontend build or development server, not ordinary use.

If startup reports a missing interface dependency, install the extra using the Python interpreter
in the same activated environment (for example the `(swarmforge)` Conda environment):

```powershell
python -m pip install -e ".[interface]"
python scripts/react_dashboard.py --demo
```

The interface requires FastAPI, Uvicorn and Pydantic 2. Installing SwarmForge's core or Streamlit
extra alone does not install all of these. Startup diagnostics identify the dependency and active
Python executable. Application import errors are reported separately rather than hidden behind
the optional-dependency message.

## Workflows

| Page | React behavior | Current boundary |
| --- | --- | --- |
| Work | Search, status filters, 25-row pages, direct/attached runs, detail, four-second refresh, queue status | Retry folding, archive preferences and queue filters remain to migrate |
| Task detail | Shared allowance, charged/reserved/available spending, run costs, events, checks, tests, report and screenshots | Older evidence archive remains in the existing dashboard |
| Task controls | Cancel, bounded retry, approve/decline pending publication requests | Opt-in controls; project retries stay with the lead; durable execution support is required for retries |
| Queue | Start/resume worker, pause new starts, lane counts | Explicit spending authorization before starting; running work can finish when paused |
| New task | Preserved draft, repository, requirements, workflow and allowance; add to existing queue | Publication always asks for approval; direct-run advanced options remain in the previous dashboard |
| Projects | Lists, exact shared cost accounting, milestones, tasks, spec, conversation and events | Shows up to 100 connected tasks and 50 recent events |
| Project planning | Structured draft editor and lead conversation before launch | Saving/replying does not launch; forms remain in the browser across navigation |
| Project controls | Approve displayed spec, pause/cancel, checkpoint reply and manual-check confirmation | Existing fixed-spec, repository ownership, merge, launch and spending barriers remain in application services |
| Chat | Separate conversations, retained messages, preserved message drafts and configured model replies | Replies are not streamed; suggested actions never execute automatically; attachments remain in the previous dashboard |
| PRs, Failures, History | Bounded filtered run/queue history with the same detail/evidence views | PR merge/close actions and detailed failure editing remain in the previous dashboard |
| Knowledge | Search and read bounded top-level notes with opaque IDs | Up to 1024 entries; nested notes and note editing remain in the vault editor/previous dashboard |
| Outcomes | Recorded workflow outcome/cost totals | Not a controlled team benchmark; benchmark suite launches remain in the previous dashboard |
| Teams and Settings | Active policy, role/model assignments, named lineups, no credentials | Configuration/secret editing and experimental speed settings remain in the previous dashboard |
| What's new | Packaged user-visible history and preview labels | Read-marking and category filters remain to migrate |

In task details, a retry keeps the original persisted task budget and deadline. Retrying a project
item cannot detach it from project accounting or bypass a merge barrier. Queue start requires an
explicit authorization of already queued work and launched projects. The web server starts no
worker automatically after enqueue or project approval: use Work's queue controls when needed.

Project draft save and launch compare the spec the human reviewed against the current stored spec
inside a write transaction. Stale editors/approvals are rejected. A project lead reply cannot overwrite
a newer draft through this API. Post-launch fixed-spec edits are not exposed. At a checkpoint,
confirm any checks marked “needs you” before continuing; corrections retain the current milestone.

Draft text is stored in this browser. If another editor changes the project, reload/review the saved
spec before saving again. **Replace form with saved spec** discards the browser form in favor of the
current server version. No project source publication or paid work is authorized by installation,
opening a page, demo use or a development handoff.

## Authorization and recovery

The launcher binds only `127.0.0.1`. Host checks reject untrusted hosts; foreign Origin and cross-site
API requests are rejected. There is no remote-access mode. Keep this service on loopback.
API responses are uncached and carry a same-origin resource policy. Public projections exclude
provider headers, credentials, raw model transcripts, task bodies and arbitrary artifact paths.
Chat/spec/note/report text can still contain private user information.

Controls additionally require a signed, expiring HttpOnly/SameSite=Strict browser cookie and a
separate CSRF header. Every POST requires the exact Origin, JSON, bounded request size and an
Idempotency-Key. Only `/api/v1/actions` accepts writes. A process restart invalidates browser sessions;
a fresh page/session is required, while completed action results remain in SQLite.

The request journal records intent before executing a shared service. Duplicate requests replay a
completed result. Reusing a key for another payload is rejected. A running or uncertain action returns
409 and is never automatically repeated after interruption; inspect the resulting state first.
The journal records a fingerprint and public result, not credentials or the full submitted message.
Chat messages and project replies remain in their existing conversation stores.

## API and retained evidence

`GET /api/v1/session` reports capabilities and safe model/policy metadata. Work remains available at
`GET /api/v1/work?limit=25&offset=0&status=running&search=term&view=all`. Limit is 1–100,
offset 0–100000, literal case-insensitive search at most 200 characters. Views `prs`, `failures`
and `history` select bounded subsets. Counts remain global. Attached runs appear once; retries are
not folded and Streamlit archive/hidden preferences are not yet applied.

`/api/v1/work/{id}` accepts `task-N` or `run-ID`. `/controls` lists eligible controls and pending
approvals. `/evidence?after=0&limit=50` returns costs, persisted shared allowance, the latest 100
acceptance checks, 25 test results and recent events. Initial `after=0` starts at the latest events;
continue with `next_event_id`. `events_has_more` indicates a reconnect backlog. The browser deduplicates
events, retains at most 200, aborts requests on selection/attempt changes and preserves good snapshots
on refresh failure. Event/message/check text is shortened with notices. Offset pages are not event cursors.

`/artifacts` lists report availability and up to 24 retained PNGs. `/report` returns inactive text,
original byte size and a truncation flag. `/screenshots/{name}` serves validated PNG data. The browser
supplies `run_id=ID`; changed attempts return 409. Report text/links/embedded markup stay inactive.
Images load after expanding the gallery; **Refresh retained files** reloads an open report/listing.

Paths derive from `runs/` beside the database, or explicit `--runs-root PATH`, never database `run_dir`
or client/report links. Only `report.md` and generated visual PNG names are served. Symlinks,
Windows reparse points, hard links, special files and path traversal are refused. Reports stop at
256 KiB; PNGs at 8 MiB, 50 million pixels and 50000 pixels per edge. Directory inspection stops at
1024 entries. Missing, unavailable, shortened and partial evidence are distinguished. Originals are unchanged.

Project, conversation, note and control response schemas are in `/openapi.json`. These APIs use bounded
public projections. Model calls use existing chatbot/project interview services, not browser-owned business
rules. Project cost totals reuse existing accounting across retries and planning calls.

## Verification and release gates

```sh
python -m pytest -q tests/test_webapp.py tests/test_web_artifacts.py tests/test_web_controls.py
python scripts/check_docs.py
npm test --prefix frontend
npm run build --prefix frontend
```

Tests use disposable stores/files, fake model adapters and simulated worker starts. API tests cover
read-only behavior, public fields, pagination, origins/hosts, session/CSRF authorization, duplicates,
uncertain outcomes, stale specs, project retry/accounting barriers and demo journeys without network
or subprocesses. Frontend checks cover contracts, reconnect windows, accessible React navigation,
spending-disabled forms, draft-only project creation and rendering every route without network calls.
Server rendering is not browser evidence. New pages still need desktop, light/dark and 390px browser
review; the computer-use service previously ended because it could not confidently determine the URL.

The interface project has five milestones: Work/API; controls/evidence; Projects/Chat; secondary parity;
release/handoff. It remains drafting and unlaunched. No milestone is marked complete by these local
changes. Publication, passing remote CI, missing parity journeys, visual/performance review and normal
merge/evidence barriers remain required. A default-interface switch requires separate approval.

A UI-only review patch/manifest is retained under ignored `.swarmforge/ui-migration/review/`. It also
isolates the small shared-service spec-review guards from unrelated execution/model-team/lead work.
Do not launch a remote swarm against the old repository snapshot: the implementation must first be
reviewed and published through an authorized GitHub write path. Existing connected GitHub access is read-only.

## Data-assembly baseline

Run `python scripts/benchmark_web_queries.py --items 10000 --samples 25 --explain`.
The queue-only fixture has 10000 tasks; mixed has 10000 tasks plus 10000 runs, half attached. The script
compiles actual local legacy Work assembly helpers without executing dashboard startup or opening live data,
and records a source SHA-256. On Nov 22, 25 warm samples had these local medians:

| Fixture | Legacy data assembly | New 25-row projection | Items represented, legacy / new |
| --- | --- | --- | --- |
| 10000 queue items | 140.41 ms | 28.90 ms | 10000 / 10000 |
| 10000 queue items + 10000 runs | 220.92 ms | 114.75 ms | 10150 / 15000 |

This is not a browser or end-to-end speed claim. Legacy reads only the latest 300 runs; grouping/order
rules differ. The new query builds one attachment set and uses a primary-key detail lookup. Global counts,
search, costs and sorting still scan history. Complete performance parity has not been established.

See [the continuation handoff](WEB_INTERFACE_HANDOFF.md) for verification limitations and publication context.
