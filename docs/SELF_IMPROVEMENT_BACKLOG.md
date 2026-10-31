# Self-improvement backlog (paste-ready runs)

How to use (queue, recommended): dashboard -> **Queue** -> "Import a backlog file" (this file is
pre-filled) -> Repository `bigdan88/swarmforge` (or `C:\Users\danie\Projects\NiBot`) -> budget $5 per
task, 2 repair cycles, Chain ON -> Import into queue -> Start queue. The tasks run one after another,
each starting from the previous task's result, so you don't merge in between. When the queue is
done, review the last result branch (it contains every step) and merge it, or review each step's
branch/PR on its own.

How to use (one at a time): dashboard -> New run -> "Describe it here" -> paste one item's Title and
Description. Merge the result before starting the next one: most of these touch
`src/swarmforge/dashboard.py`.

Recommended order: 1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7.

---

## 1. Dashboard never crashes: isolate every section

**Title:** Make every dashboard section fail-safe

**Description:**
A single bad file (truncated transcript line, missing log, corrupt JSON in an event, a deleted
run folder, a locked SQLite database) can currently raise an exception that replaces the whole
dashboard page with a stack trace. Make the dashboard resilient:

- In `src/swarmforge/dashboard.py`, wrap the rendering of each run tab (Delegation, Agents &
  timeline, Transcripts, Tests, Review, Knowledge, Tokens & cost, Console, Events) and each page
  (Runs, New run, Knowledge, Benchmarks) in a helper `safe_section(name, fn)` that catches any
  exception and shows `st.warning("<name> could not be displayed: <short error>")` with the
  traceback inside a collapsed `st.expander("Details")`, while the rest of the page keeps working.
- `render_transcript` must skip unparseable lines and show a caption with how many were skipped.
- Reads of files referenced by the database (logs, transcripts, JUnit, reports) must handle missing
  files with a friendly caption instead of an error.
- `Store` reads must retry up to 3 times with a short delay on `sqlite3.OperationalError:
  database is locked`.

Acceptance criteria:
1. A transcript file containing a corrupt line renders the valid steps plus a "skipped 1 line" note.
2. Deleting a run's folder from disk does not break the Runs page; affected tabs show a friendly message.
3. An exception raised inside one tab (simulate with monkeypatch in a test) shows a warning in that
   tab only; the other tabs and metrics still render.
4. Locked-database reads are retried (unit test with a mocked execute that fails twice then succeeds).
5. New tests in `tests/test_dashboard_resilience.py` using Streamlit `AppTest`; the existing test suite passes.

---

## 2. Plain-language run status with the next action

**Title:** Show what happened and what to do next on every run

**Description:**
Run state reasons are technical (`BLOCKED - cannot access repository ...`, `FAILED: repair limit
reached ...`). Add a status card at the top of the run detail page in `src/swarmforge/dashboard.py`
that explains the outcome in plain language and offers the next action.

- New module `src/swarmforge/explain.py` with `explain_run(run: dict, events: list[dict]) ->
  Explanation` (dataclass: `headline`, `detail`, `severity` one of success/info/warning/error,
  `actions: list[str]`). Pure function, no Streamlit import.
- Cover at least: running (show current phase in words, e.g. "Developers are coding 2 tasks"),
  COMPLETED with PR, COMPLETED delivered to local branch (show the exact `git diff` / `git merge`
  commands), COMPLETED without PR access, BLOCKED for each preflight reason (Docker, missing key,
  empty repo, unreachable repo, not a git repo, image not built), BUDGET_EXCEEDED,
  FAILED repair limit (summarize the unresolved problems), CANCELLED, unexpected error.
- The dashboard renders the card with the matching colour, the detail text, and the actions as a
  bulleted list; commands are shown in `st.code` blocks so they can be copied.
- Add a "Run again with the same settings" button for FAILED/BLOCKED/CANCELLED runs that pre-fills
  the New run form (title, description, repository, budget, cycles) and switches to the New run page.

Acceptance criteria:
1. `explain_run` returns a specific headline and at least one action for every case listed above
   (parametrized unit tests in `tests/test_explain.py`).
2. The run page shows the card above the metrics for completed, blocked and failed runs (AppTest).
3. "Run again" pre-fills the New run form with the original values.
4. The existing test suite passes.

---

## 3. A clearer New run form with live checks

**Title:** Guide the user through the New run form with live validation

**Description:**
Improve `page_new_run` in `src/swarmforge/dashboard.py` so mistakes are caught while typing, before
anything is started:

- A "Check repository" button next to the Repository field that runs `normalize_repo` + `preflight`
  and shows a green "OK: <resolved source> (branch X)" or the error with its fix. Remember the
  last successful check in session state; Start run skips re-checking if the value is unchanged.
- An "Issue quality" panel that warns (does not block) when: the description is shorter than
  200 characters, there is no "Acceptance criteria" section or numbered list, or no file or module
  is mentioned. Show one-line tips for each warning.
- Templates: a selectbox "Start from a template" with Bug fix, New feature, Refactor, and
  Improve SwarmForge itself; choosing one fills the description with a skeleton (Context,
  Requirements, Acceptance criteria) without overwriting text the user already typed (ask first).
- An estimate line under the budget field: median cost and duration of the user's previous
  completed runs in the same mode (from the SQLite store), e.g. "Similar runs: ~$0.45, ~25 min".
  Hide it when there are fewer than 2 previous runs.

Acceptance criteria:
1. Check repository shows success for a valid local git folder and the specific error for a missing
   folder and for an empty repository (AppTest).
2. The quality panel shows the "no acceptance criteria" warning for a description without them and
   no warnings for a well-formed one.
3. Choosing a template fills an empty description; it does not overwrite existing text silently.
4. The estimate uses only completed runs of the same mode and is hidden with fewer than 2 runs.
5. New tests in `tests/test_new_run_form.py`; the existing test suite passes.

---

## 4. Settings & health page

**Title:** Add a Settings & health page to the dashboard

**Description:**
Users currently edit `swarmforge.yaml` by hand and diagnose problems with `swarmforge check`.
Add a "Settings" page to the dashboard sidebar:

- Health section: one row per check from `preflight.environment_problems` plus GitHub access
  (token present and valid, via `github.get_token` and a GET /user call), each with a green/red
  icon and the fix. Buttons: "Re-check", "Build sandbox image" (runs `DockerSandbox.build_image`
  and streams its output into an expander), "Test models" (sends the same tiny tool-calling request
  as `checks.run_checks(live=True)` for each role and shows OK/fail, tokens and cost per role).
- Models section: an editable table of roles -> model id (selectbox populated from the provider's
  model list when reachable, free text otherwise), temperature and max_steps.
- Limits section: budget, max runtime, concurrent agents, repair cycles.
- Save validates by calling `config.parse_config` on the edited data and only writes
  `swarmforge.yaml` if it is valid; it first writes a backup `swarmforge.yaml.bak`. Show validation
  errors inline. Never display or write API keys; show only whether each key variable is set.
- After saving, clear the cached config (`st.cache_resource.clear()`) so the change applies without
  restarting.

Acceptance criteria:
1. Health rows show the right status for a missing API key and an unreachable Docker (tests
   monkeypatch the checks).
2. Saving an invalid config (e.g. reviewer model equal to a developer model) shows the validation
   error and leaves `swarmforge.yaml` unchanged; a valid save writes the file and a `.bak` copy.
3. API key values never appear in the rendered page (test asserts the fake key string is absent).
4. New tests in `tests/test_settings_page.py`; the existing test suite passes.

---

## 5. Live progress during long model calls (streaming)

**Title:** Show live progress during long model calls

**Description:**
Planner and reviewer calls can take 4+ minutes with no visible activity, so users think SwarmForge
is stuck. Add streaming to `LLMClient.chat` in `src/swarmforge/llm.py` (OpenAI-compatible
`stream: true` with `stream_options: {"include_usage": true}`), assembling content,
reasoning_content, tool calls (merging incremental `tool_calls[i].function.arguments` deltas by
index) and the final usage chunk into the same `ChatResult` as today. Report progress through an
optional `on_progress(tokens_so_far, seconds)` callback; the orchestrator logs a "progress" event
about every 15 seconds ("planner: 1,840 tokens so far, 45 s"), and the dashboard's "Working right
now" cards show the latest progress for each running agent. Keep cost accounting and retries
unchanged; if the provider rejects streaming (HTTP 400 mentioning stream) fall back to the current
non-streaming request and remember that for the rest of the run.

Acceptance criteria:
1. A streamed response (mocked SSE via httpx.MockTransport) produces the same ChatResult as the
   equivalent non-streamed response, including tool calls split across chunks and usage/cost.
2. Progress callbacks fire during a slow mocked stream; progress events are logged and rendered.
3. A provider that rejects streaming still works via fallback.
4. Unit tests in `tests/test_streaming.py`; the existing test suite passes.

---

## 6. Run history management

**Title:** Filter, archive and clean up runs from the dashboard

**Description:**
The Runs table grows forever and clones under the workdir consume disk space. In
`src/swarmforge/dashboard.py` and `src/swarmforge/db.py`:

- Filters above the runs table: state (multi-select), repository, mode, and a text search over
  the issue title.
- An `archived` flag on runs (schema migration that adds the column if missing, default 0).
  Archived runs are hidden unless "Show archived" is ticked. Buttons on a run: Archive / Unarchive.
- "Free disk space" for finished runs: deletes the run's `repo` and `worktrees` folders but keeps
  the database rows, report, transcripts and logs; refuses for active runs; shows how many MB were
  freed. Requires a confirmation checkbox.
- Show the total disk usage of the workdir on the Runs page.

Acceptance criteria:
1. Filters narrow the table correctly (AppTest with several fake runs in different states).
2. Archiving hides a run by default and the migration works on an existing database without the column.
3. Free disk space removes only the clone/worktrees of a finished run, keeps the report and
   transcripts, and is refused for a running run.
4. New tests in `tests/test_run_history.py`; the existing test suite passes.

---

## 7. Friendlier first-time experience

**Title:** First-run welcome and guided setup in the dashboard

**Description:**
When the dashboard opens with no runs, users see an empty table. Replace that with a welcome panel
in `src/swarmforge/dashboard.py`:

- A checklist that reuses the Settings & health checks (item 4) if present, otherwise
  `preflight.environment_problems`: Docker running, sandbox image built, DeepInfra key set,
  GitHub login (optional). Each unchecked item shows the exact command or button to fix it.
- A "Try the sample (about $0.40, 15 minutes)" button that prepares the sample repository and
  starts a run with sensible defaults (max repair cycles 1, budget $3), then opens it.
- A short "How SwarmForge works" section (planner -> developers in parallel -> tester -> reviewer
  -> repair cycles -> PR) with a small static diagram drawn with `st.graphviz_chart`.
- The panel stays available later via a "Getting started" link in the sidebar.

Acceptance criteria:
1. With an empty database the welcome panel renders instead of the empty runs table (AppTest).
2. The checklist reflects a missing API key and a missing image (monkeypatched checks).
3. The sample button creates the sample repo (in a temp HOME in tests) and starts a run via
   `launcher.start_run` (monkeypatched to avoid a real process).
4. The existing test suite passes.
