# SwarmForge

An autonomous multi-agent software team built from Chinese open models (Qwen, DeepSeek, Kimi,
GLM, MiniMax) served by DeepInfra.

**You give it a GitHub issue URL → a planner breaks it into a task graph → two coding agents
work in parallel in isolated git worktrees → their work is merged → a test engineer writes
acceptance tests → the orchestrator runs the tests itself → an independent reviewer reviews the
diff → blocking problems go back to the planner as corrective tasks (up to 3 cycles) → you get a
tested draft pull request and an execution report** that records what each agent did, the tokens
and cost, and every human intervention.

```
swarmforge run https://github.com/you/repo/issues/42
```

| Role | Default model (DeepInfra) | Tools |
|---|---|---|
| Planner / Eng. manager | `Qwen/Qwen3.5-397B-A17B` | read-only, `submit_plan` |
| Backend developer | `deepseek-ai/DeepSeek-V4-Pro` | read/write files, sandboxed commands, dev tools (processes, browser, `check_code`), `finish` |
| Frontend developer | `moonshotai/Kimi-K2.6` | same, runs concurrently |
| Reviewer | `zai-org/GLM-5.3` | read-only, `submit_review` |
| Test engineer | `MiniMaxAI/MiniMax-M3` | test files only, sandboxed commands, dev tools, `submit_test_report` |
| Librarian | `deepseek-ai/DeepSeek-V4-Flash` | read-only, `submit_notes` (Zettelkasten) |

All of these are set in `swarmforge.yaml`; any role can use any model.

## Principles

* **Agents propose, code decides.** State transitions, the approval gate, test verdicts, merges
  and GitHub publishing are deterministic code. A model can't declare tests passed: the
  orchestrator runs them and reads JUnit.
* **Sandboxed by default.** Each agent command runs in a throwaway Docker container with no
  network, no capabilities and a read-only root filesystem; only that task's worktree is mounted.
  Keys never enter a container. See [docs/SECURITY.md](docs/SECURITY.md) for what this doesn't
  protect against.
* **Nothing is merged automatically.** It produces draft PRs only, on `swarmforge/*` branches,
  never force-pushed, after you approve the push.
* **Measured, not guessed.** Each model call records tokens and DeepInfra's reported cost.
  Estimates and missing data are labelled as such.

## Install

Requires Python 3.11+, Git, and Docker Desktop (or Docker Engine).

```bash
python -m venv .venv
# Windows: .\.venv\Scripts\Activate.ps1     macOS/Linux: source .venv/bin/activate
pip install -e ".[dashboard]"          # add ,dev for the test suite
cp swarmforge.example.yaml swarmforge.yaml
cp .env.example .env                   # put DEEPINFRA_API_KEY (and optionally GITHUB_TOKEN) here
swarmforge sandbox build               # builds swarmforge-sandbox:latest (python + pytest)
swarmforge check --live                # config, docker, models, prices, live tool-calling test
```

## Use

The dashboard is the control center:

```bash
swarmforge dashboard          # opens http://localhost:8501
```

* **Work**: one list of everything: running runs, waiting queue tasks, finished and failed work,
  with filters (All / Active / Waiting / Finished / Failed) and search. **Click any task** to open
  it right underneath. **Tick the boxes** to act on several tasks at once: delete (with their run
  history and, optionally, their files), cancel, run again, or move to the front of the queue.
  Opening a task shows:
  * **Waiting task**: change anything (title, description, repository, team, budget, repair cycles,
    time limit, chaining, GitHub approval), move it up/down or to the front, or remove it.
  * **Running run**: change its budget, time limit and repair cycles while it runs (applied within a
    few seconds, no restart), or cancel it. Approve or decline GitHub pushes.
  * **Failed/stopped run**: start it again from scratch, or continue from where it stopped, with new
    limits.
  * Every run shows these tabs: **Delegation** (planner → tasks → agents → tester → reviewer →
    repair cycles, with each agent's brief and hand-back), **Agents & timeline**, **Transcripts**
    (each agent's prompt, reasoning, tool calls and results), **Tests, Review, Knowledge, Tokens &
    cost, Console, Events**.

  The queue bar at the top starts, pauses and resumes the queue worker. Queued tasks run one after
  another; with chaining, each task starts from the previous task's result on the same repository.
  CLI: `swarmforge queue import FILE --repo R`, `queue list`, `queue resume`, `queue pause`.
* **New task**: the repository field remembers the repositories you used before (newest first)
  and still accepts a new folder, owner/repo or URL. *Start now* (describe the work or paste a GitHub issue URL, point it at a
  repository, set budget, repair cycles and time limit, **Start run**; **Prepare sample** for a
  one-click demo) or *Add to the queue* (one task, or import a backlog file such as
  `docs/SELF_IMPROVEMENT_BACKLOG.md`). Runs are background processes and keep going if you close
  the browser.
* Every page has its own address (`?page=work`, `?page=failures`, …), handy for bookmarks on an iPad.
* **Knowledge**: the Zettelkasten. Browse, search, edit and link notes, view the note graph,
  and write your own notes for the agents to use.
* **Benchmarks**: single agent vs swarm comparisons.

Everything is also available from the CLI:

```bash
swarmforge run https://github.com/OWNER/REPO/issues/N        # or --issue-file F --repo PATH
swarmforge runs | status [RUN] | logs -f [RUN] | cancel RUN | report [RUN]
swarmforge publish RUN                                        # push + draft PR later
swarmforge bench ISSUE --repo PATH --acceptance DIR           # single agent vs swarm
```

Machine-readable output (added by SwarmForge itself in its first self-improvement run):
`swarmforge runs --json` prints a JSON array of runs (id, mode, state, cost_usd, total_tokens, …);
`swarmforge status [RUN] --json` prints one object with the same fields plus
`integration_branch`, `pr_url`, `repair_cycle`, `interventions` and a `tasks` list. An unknown
run prints `{"error": "run not found: <id>"}` and exits with status 2.

## Agent developer tools

Developers, the conflict resolver and the test engineer get, besides files and `run_command`:

| Tool | What it does |
|---|---|
| `start_process` / `read_process_output` / `stop_process` | Run a server or app in the background (e.g. `streamlit run app.py --server.headless true --server.port 8501`), wait until a port or log line says it's ready, read its output, stop it. |
| `browse` | Open `http://localhost:<port>` in headless Chromium: visible text, errors shown on the page (Streamlit exceptions, tracebacks, `role=alert`), uncaught JS errors, console errors, failed requests and the clickable elements. Optional actions (click, fill, press, select, wait) run first; optional screenshot to the run's artifacts. Local URLs only. |
| `check_code` | `ruff` + `mypy` on the changed files (or given paths), returned as `file:line:col code message`. Respects the project's own ruff/mypy config. |

They run where `run_command` runs, with the same isolation: in Docker, one long-lived session
container per agent (no network, read-only root, dropped capabilities), which is killed with every
process in it when the agent finishes. The tools need the rebuilt sandbox image
(`swarmforge sandbox build`, plus `--dev` for SwarmForge's own runs); the browser adds roughly
500 MB to the image. In `sandbox.mode: local`, install them with `pip install -e ".[tools]"` and
`python -m playwright install chromium`. Turn them off with `sandbox.dev_tools: false`.

## Speed

Runs are slow mainly because of repeated full test-suite runs, many large-model calls and hand-offs.
These settings (all on by default) keep that down:

* **Targeted tests.** Agents are told to run only the tests relevant to their change; the orchestrator
  still runs the full `test_command` as the evidence. A repository can name a quicker suite for agents
  with `agent_test_command` in `swarmforge.project.yml` (SwarmForge's own skips the `slow`-marked
  end-to-end tests: `pytest -m "not slow"`).
* **One active run per repository** (`limits.one_run_per_repo`). A run started while another run on
  the same repository is active waits for it ("waiting: run(s) ... are already working on this
  repository") instead of competing for the model rate limit and editing the same files. Its time
  limit starts when it actually starts.
* **One container per agent** (`sandbox.reuse_container`). `run_command` executes inside the agent's
  long-lived sandbox container instead of starting a new one per command; the timeout is enforced
  inside the container.
* **Fewer, larger tasks.** The planner aims for 2-4 tasks (`limits.max_tasks: 5`).
* **Faster developer models.** `swarmforge.yaml` uses Flash models for the developers, with the
  previous models as fallbacks. Compare with `swarmforge bench` and switch back if quality drops.

## Failures and self-healing

Every run that fails gets a **post-mortem**. SwarmForge sorts the failure into a cause (time limit,
budget, tests failing, review rejected, environment, ...) and asks the planner model *why it
failed and what the next attempt should do differently*. The post-mortem is kept in three places:

* the dashboard's **Failures** page, with counts by cause and what happened next;
* the run's `postmortem.md`;
* a `failure` note in the knowledge vault, linked from the **Failures** hub note. Later runs on
  the same repository are given these notes, so the agents learn from them.

CLI: `swarmforge failures`, `swarmforge diagnose RUN`.

If a retry can fix the failure, SwarmForge **heals itself**:

* It starts a new attempt that continues from the failed run's code.
* The diagnosis is added to the task.
* The new attempt gets more effort: 1.5x the time, budget and agent steps, compounding with each
  attempt, plus one more repair cycle.
* It never retries problems a human must fix first, such as Docker being down, a missing key or
  a bad repository.
* Retries are capped by *Auto-fix attempts* (default 2, settable per task) and by a total budget
  of 3x the task's budget. See `self_heal:` in `swarmforge.example.yaml`.

On the Work page, a task that healed itself shows as "Done (fixed itself, attempt N)". Its earlier
attempts are listed under Failures.

## Repositories and dependencies

The Repository field and `--repo` accept a local git folder, `owner/repo`, or any GitHub URL of the
repo: `https://github.com/owner/repo`, `.../tree/<branch>` (selects that branch), `.../blob/...`,
`.../issues/N`, `git@github.com:owner/repo.git`. Before a run starts, SwarmForge checks that Docker
answers, the sandbox image exists, the API key is set, and the repository is reachable and has at
least one commit. Each problem is reported with its fix, and nothing is spent. Private GitHub
repositories are cloned with your `GITHUB_TOKEN` (or `gh auth login`); the token is never stored in
the clone.

Project dependencies are installed automatically. Packages from `requirements*.txt` or
`pyproject.toml` (or an `install_command` in `swarmforge.project.yml`) go into the worktree's
git-ignored `.swarmforge/pydeps`. This is the only step that gets network access, and it uses a
shared pip cache. Agents can call `install_dependencies` after adding a package. Tests and agent
commands stay offline.

## Per-project settings

A repository can include `swarmforge.project.yml` with `test_command`, `sandbox_image` and
`install_command`. These are the only settings a repository can set. The dashboard's **Advanced** section and the CLI
flags `--test-command` / `--sandbox-image` override them for a single run. SwarmForge's own
repository uses this to test itself in `swarmforge-dev-sandbox` (`swarmforge sandbox build --dev`).

For a local repository, an approved result is pushed back into it as a new branch
`swarmforge/...`; your checkout and existing branches are never touched.

## Knowledge base (Zettelkasten)

`knowledge/` (next to `swarmforge.yaml`) is an Obsidian-compatible folder of atomic Markdown
notes with `[[wikilinks]]`:

* **Before planning**, the most relevant notes (same repository first, then keyword relevance)
  are given to the planner. Developers get the pitfalls, patterns and facts among them, and the
  reviewer gets the review lessons. Which notes were used is logged with each run.
* **After every run**, including failed runs, a librarian agent (DeepSeek-V4-Flash by
  default, costing about a cent) distills up to 6 atomic notes and links them to existing ones.
  Note types: decision, pitfall, pattern, repo-fact, review-lesson, model-behavior. The
  orchestrator also writes a run-log note and refreshes a hub note per repository.
* Your own notes (type `user-note`, or any type) are read by the agents the same way, so you
  can write down conventions and design decisions once.

Benchmarks turn the knowledge base off so every mode starts with the same information.

## Documentation

* [docs/FIRST_RUN.md](docs/FIRST_RUN.md): step-by-step first real GitHub issue
* [docs/SELF_IMPROVEMENT.md](docs/SELF_IMPROVEMENT.md): point SwarmForge at its own code
* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, state machine, why it has its own agent runtime instead of OpenCode
* [docs/PROVIDERS.md](docs/PROVIDERS.md): DeepInfra setup, choosing models, other providers
* [docs/SECURITY.md](docs/SECURITY.md): sandbox strategy and remaining limitations
* [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)
* [docs/example-report.md](docs/example-report.md): report from a real run on the sample issue
* [docs/example-benchmark.md](docs/example-benchmark.md): real single-vs-swarm benchmark result

## Tests

```bash
pip install -e ".[dev]"
python -m pytest -q
```

The suite covers config validation, plan/DAG validation, the scheduler's concurrency, tool path
confinement, cost accounting, retries, budget enforcement, JUnit evidence, GitHub safety rules,
and full end-to-end runs. The end-to-end runs use the real orchestrator, git worktrees, the
sandbox and pytest, with the model API replaced by scripted responses: a complete cycle including
a review-driven repair, repair-limit failure, budget exhaustion, cancellation, single-agent mode,
and the same flow inside Docker plus checks on the sandbox's isolation. Mocks are used only in
these tests, never in real runs.

## Layout

```
src/swarmforge/      orchestrator, state, plan, scheduler, agent/, sandbox, workspace,
                     testing, llm, github, db, report, benchmark, checks, publish, dashboard, cli
samples/inventory-app/   demo application with existing tests
samples/issues/          GitHub-ready issue text
samples/acceptance/      hidden acceptance tests for benchmarking
tests/               unit + end-to-end tests
docs/                documentation
```
