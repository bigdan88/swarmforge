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
| Backend developer | `deepseek-ai/DeepSeek-V4-Pro` | read/write files, sandboxed commands, `finish` |
| Frontend developer | `moonshotai/Kimi-K2.6` | same, runs concurrently |
| Reviewer | `zai-org/GLM-5.3` | read-only, `submit_review` |
| Test engineer | `MiniMaxAI/MiniMax-M3` | test files only, sandboxed commands, `submit_test_report` |
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

* **New run**: describe the work (or paste a GitHub issue URL), point it at a repository,
  pick swarm or single agent, set the budget and repair cycles, then **Start run**. Runs are
  background processes and keep going if you close the browser. Use **Prepare sample** for a
  one-click demo.
* **Runs**: every run, live. Each run has these tabs:
  * **Delegation**: planner → tasks → agents → tester → reviewer → repair cycles, with each
    agent's brief and what it reported back.
  * **Agents & timeline**: what is running now and which tasks overlapped.
  * **Transcripts**: every agent session, showing the prompt it received, its reasoning, each tool
    call and result, and what it submitted.
  * **Tests, Review, Knowledge, Tokens & cost, Console, Events**.

  Approve or decline the GitHub push with a button. Cancel runs.
* **Queue**: line up tasks (or import a backlog file such as
  `docs/SELF_IMPROVEMENT_BACKLOG.md`) and press **Start queue**. Tasks run one after another in
  a background worker. With chaining, each task starts from the previous task's result on the same
  repository, so a series of improvements builds up without merging in between. The queue pauses
  if a task fails. CLI: `swarmforge queue import FILE --repo R`, `queue list`, `queue resume`,
  `queue pause`.
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
