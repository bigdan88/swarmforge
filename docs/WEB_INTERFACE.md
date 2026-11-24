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

| Page | React behavior | Deliberate differences (deferred or kept elsewhere) |
| --- | --- | --- |
| Work | Search, status filters, 25-row pages, direct/attached runs, detail, four-second refresh, queue status; one row per task with an attempt count (Every attempt lists them); Show archived; archive/unarchive finished runs | Streamlit's extra state/repository/mode filters, project progress banner and storage readout are not migrated; search and status cover the same rows |
| Task detail | Shared allowance, charged/reserved/available spending, run costs, events, checks, tests, report and screenshots | Older evidence archive and free-disk tools remain in the existing dashboard |
| Task controls | Cancel, bounded retry, approve/decline pending publication requests, archive | Opt-in controls; project retries stay with the lead; durable execution support is required for retries |
| Queue | Start/resume worker, pause new starts, lane counts | Explicit spending authorization before starting; running work can finish when paused |
| New task | Describe it or a GitHub issue URL (optional repository override); allowance, workflow, model team, repair cycles, time limit, auto-fix attempts; preserved draft | Always queued with publication approval ("Pre-approve" is not offered). Test command/sandbox image overrides, the sample-repository helper and immediate direct runs remain in the existing dashboard |
| Projects | Lists, exact shared cost accounting; a project opens on a summary (plain-language status, last error with a suggested fix, milestone/tasks/spending, pending decision) with Overview, Milestones, Tasks, Conversation and Spec & history tabs (the last tab used is remembered per project; drafts open on Conversation) | Shows up to 100 connected tasks and 50 recent events |
| Project planning | New project opens a brief idea form; follow-up questions use fields or choices. Optional details, GitHub owner and proposed budget use editable defaults. A short exchange opens a drafted proposal for review, with an optional editor | Saving/replying does not launch; answers remain in the browser across navigation; stale question forms are refused |
| Project controls | Approve displayed spec and start the queue worker, pause/cancel, checkpoint reply and manual-check confirmation, with page-level confirmations | Existing fixed-spec, repository ownership, merge, launch and spending barriers remain in application services. Paused projects offer a reviewed scope amendment; repository identity and completed milestones are protected |
| Chat | Right-aligned user bubbles and left-aligned assistant replies; live model text, copy/download buttons on code blocks, copy reply and save Markdown, tables and inline images, generated-file download cards, retained task suggestions, attachments and separate conversations | Reply text uses provider streaming and reconnectable SSE; tool status is also polled. Suggestions still require reviewed launch. Uploaded SVG is refused; generated HTML/SVG is an inert download |
| Pull requests | Open PRs per known repository (60-second cache, Refresh from GitHub), status, size, head commit, link to the run; merge (squash/merge/rebase, delete branch), Merge all ready, Update branch, all behind "I've reviewed these changes" | Each merge names the reviewed head commit and is refused if new commits arrived. Auto-merge settings and "update and restart SwarmForge" after merging its own repository remain in the existing dashboard |
| Failures | Post-mortems with counts (fixed themselves / need you / not diagnosed), cause filter, why/fix/lesson/evidence, links to the run and its retry, authorized re-diagnosis | Bulk "diagnose all older failures" remains in the existing dashboard (one model call per run); the cause chart is shown as counts |
| History | Finished work with the same detail/evidence views | None |
| Knowledge | Nested vault notes (hidden and linked folders skipped), type/repository filters, read, create, edit and delete with conflict protection against edits made in Obsidian | Generated note types (hub, run log, failure) are read-only. Backlinks and the note graph remain in the existing dashboard and Obsidian. Up to 2048 vault entries are scanned |
| Speed | Median run/working time/repair cycles/cost, time per phase per run, phase medians, by-settings groups, agents by role and model, self-healing outcomes | CSV export stays on the command line (`swarmforge metrics --csv`) |
| Benchmarks | Suite comparisons per team/mode (delivery %, time, cost per success) and recorded everyday outcomes | Suites run real paid tasks, so they are started from the command line (`swarmforge bench-suite`) |
| Settings | Health checks on demand, which API key variables are set (never values), per-role model/temperature/max steps with optional provider model list, limits; validated save with comment-preserving backup and stale-file protection; authorized model test | Saved values apply after restarting the web interface (the page says so). Sandbox image build (long Docker build) stays on the command line or existing dashboard. Secrets are never editable here |
| Model teams | Named lineups and current roles | Team files are edited in `teams/` |
| What's new | Packaged history grouped by day, search, category filter, New-to-you marks, Mark all as read (shared with the existing dashboard), links to pages; nav badges for unread highlights and failures needing you | None |

## Task workspace on Work

Expand a task on **Work** or **History** to open its task workspace. A compact header keeps the
status, current run stage, repair cycle and pull-request link visible above seven tabs:

- **Overview** shows finished bot assignments, bots, the current acceptance cycle and recorded
  spend, followed by the current action, last result and next recorded action. Long text expands
  on demand. Clicking a summary card opens its relevant tab.
- **Sequence** groups implementation assignments by repair cycle and dependency level, including
  parallel branches. Each card shows its bot/model, state and prerequisites; results, errors and
  file hints expand separately. Filter by working, waiting, finished or needs attention.
- **Bots** lists assigned and actually used role/model pairs, including fallbacks and escalations,
  with recorded calls, known cost and assignment states. Current settings are not substituted for
  the models that ran. A stopped run does not show assignments as actively working.
- **Checks** keeps the latest acceptance results in the current cycle separate from earlier
  results, and shows recorded test runs. Expand a check to inspect its evidence.
- **Files** opens the existing retained-report and screenshot browser on demand.
- **Activity** shows a compact newest-first event list with search, warnings/errors and stage
  filters. Only 25 matches appear at a time; messages expand individually.
- **Controls** keeps approvals, cancellation, authorized retries and archiving together with the
  full allowance and model-usage breakdown. Pending approvals are highlighted above every tab.

The selected tab is remembered per task and workspace. Tabs support arrow keys, Home and End;
on phones they scroll horizontally. Evidence continues refreshing while a task is open.
The stage strip marks observed stages and the current stage; it never invents a completion
percentage from elapsed time or stage position. Finished assignments and passed acceptance
checks are different measures, and a completed run can still await a pull-request merge.

Execution details are a bounded read-only projection of task assignments, dependencies, state
transitions and aggregated model calls. It exposes no configuration, raw prompts, worktree paths
or provider credentials. Up to 200 recent assignments, 40 role/model/provider usage groups and
100 stage transitions are previewed, with explicit truncation notices and whole-run counts.
Missing or cyclic prerequisites are shown without an assumed order. Validation assignments
remain separate from implementation dependency levels. Undispatched and older runs show honest
empty states; browsing never starts a model, worker or task and does not migrate the database.

The interactive demo includes a fabricated branching task plan, several bot roles and recorded
sample usage so these views can be checked without spending AI credits.

## Rich replies and streaming

General and project conversations use the same message renderer. Code fences show a language header,
**Copy code** and **Download**. Hover or focus a message to copy it or save it as Markdown; these controls
stay visible on phones. Markdown tables and HTTPS images render inline, with no image referrer sent.
Image attachments open at full size when clicked. User messages stay on the right in both themes.

When asked for a file, chat can use `create_file` to attach text/code, valid raster images, PDF, ZIP or
Office document bytes (maximum 10 MB each, eight files per reply). It cannot execute code or read an
arbitrary local path. A configured chat-tool allowlist still applies. Without tools, a `file:filename.ext`
text fence provides a copyable and downloadable text file. Completed text file fences are also retained
as attachments. HTML/SVG/code downloads are served as inert attachments, never executed in the app.
Artifacts are scoped to their conversation and message and survive a web-server restart.

The provider's answer deltas are saved as a bounded partial reply and delivered through reconnectable
server-sent events. Reading or reconnecting starts no model call. Navigation leaves the background
reply running. A disconnected model stream is not retried after partial output; its text/files are
retained with an interruption notice, and known or conservatively estimated costs remain accounted for.
Project kickoff streams the human explanation while keeping the structured proposal behind its existing
validation and human approval. Launch is disabled while a project reply is in progress.

The demo uses simulated generation events and includes code, a downloadable CSV and a PNG preview;
it never calls models or creates repositories. The previous Streamlit general chat also accepts live
text callbacks; the shared rich message controls described above belong to the React dashboard.

Chat replies run in the background as durable jobs (see docs/ARCHITECTURE.md, Background jobs) owned
by the web process that started them. The question is stored in the same transaction that starts the
job, so a crash never loses it. Starting another web instance never interrupts a live reply; a reply
whose owner process has exited, or which stopped renewing its lease for a minute, is marked interrupted
when the next instance starts or the conversation is used, and is never re-sent automatically. Pressing
Send is the consent for one chat or project-lead reply; queue starts, task creation, retries, launches
and paid model tests still ask for explicit confirmation.

When a conversation is longer than the configured window, older messages are summarized once
(a stored rolling summary, extended only with newly dropped messages) and sent with the recent ones;
the project lead does the same past its last 30 messages. Agent sessions whose transcript passes about
240k characters summarize their earlier tool turns and keep the latest six intact. Summaries and chat
titles are small model calls charged to the same chat or project allowance; if one fails, the reply
still goes out (older turns are dropped with a note, or the title comes from the first message).

Work folds attempts by recorded lineage: the shared task allowance, or the retry/self-heal/continue
chain. Tasks are never grouped by title, so independent tasks with the same title stay separate.

Each server reports an opaque workspace identity (derived from its database path, which is not shown)
and a per-start instance identity. The page binds browser drafts, staged attachments, pending request
keys and every write to the workspace it loaded; writes naming another workspace are refused with 409.
A tab left open across a workspace change shows an out-of-date banner until reloaded; a restart of the
same workspace keeps drafts and shows a short notice.

In task details, a retry keeps the original persisted task budget and deadline. Retrying a project
item cannot detach it from project accounting or bypass a merge barrier. Queue start requires an
explicit authorization of already queued work and launched projects. The web server starts no
worker automatically after enqueue or project approval: use Work's queue controls when needed.

Project draft save and launch compare the spec the human reviewed against the current stored spec
inside a write transaction. Stale editors/approvals are rejected. A project lead reply cannot overwrite
a newer draft through this API. At a pause, Edit approved scope uses the existing human amendment service; approving a revision keeps the project paused until continued. At a checkpoint,
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
python -m pytest -q tests/test_webapp.py tests/test_web_artifacts.py tests/test_web_controls.py tests/test_web_chat.py tests/test_web_parity.py
python scripts/check_docs.py
npm test --prefix frontend
npm run build --prefix frontend
```

Tests use disposable stores/files, fake model adapters and simulated worker starts. API tests cover
read-only behavior, public fields, pagination, origins/hosts, session/CSRF authorization, duplicates,
uncertain outcomes, stale specs, project retry/accounting barriers and demo journeys without network
or subprocesses. Frontend checks cover contracts, reconnect windows, accessible React navigation,
explicit allowance approvals, draft-only project creation and rendering every route without network calls.
Server rendering is not browser evidence. Browser evidence for every page (desktop and 390px, light and
dark) and the main journeys was captured with Playwright against `--demo` on November 22, 2026; the
scripts and screenshots are under ignored `.swarmforge/ui-migration/screenshots/`. Rerun them after UI changes.

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
