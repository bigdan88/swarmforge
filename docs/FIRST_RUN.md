# Running SwarmForge on your first real GitHub issue

## 0. Prerequisites

* Python 3.11+, Git, Docker Desktop (running), a DeepInfra key.
* Optional for PRs: a fine-grained GitHub token for the target repo (see docs/SECURITY.md) or
  `gh auth login`.

## 1. Install and configure

Windows with Anaconda (recommended if you already use conda — keeps SwarmForge out of `base`):

```powershell
conda create -n swarmforge python=3.12 -y
conda activate swarmforge
pip install -e ".[dashboard]"
```

Windows with python.org Python (from the SwarmForge folder):

```powershell
py -3.12 -m venv .venv          # or -3.11
.\.venv\Scripts\Activate.ps1
pip install -e ".[dashboard]"
copy swarmforge.example.yaml swarmforge.yaml
copy .env.example .env      # then edit .env: DEEPINFRA_API_KEY=..., GITHUB_TOKEN=...
swarmforge sandbox build
swarmforge check --live
```

macOS/Linux: same, with `python3.12 -m venv .venv && source .venv/bin/activate` and `cp`.

## 2. Rehearse locally (no GitHub needed)

From the dashboard: `swarmforge dashboard` → **New run** → **Prepare sample** → **Start run**,
then watch it on **Runs**. Or from the command line:

```powershell
python samples\make_sample_repo.py $HOME\swarmforge-sample
swarmforge run --issue-file samples\issues\export-csv-json.md --repo $HOME\swarmforge-sample
```

In a second terminal: `swarmforge dashboard` (opens http://localhost:8501), or
`swarmforge logs -f`. The result lands on branch `swarmforge/task-<id>` inside
`~/.swarmforge/runs/<run_id>/repo`, with `report.md` beside it. Your sample repo itself is not
modified.

## 3. Put the sample on GitHub (to see the PR step)

```powershell
cd $HOME\swarmforge-sample
gh repo create swarmforge-sample --private --source . --push
gh issue create --title "Add CSV and JSON export functionality" --body-file <path-to>\samples\issues\export-csv-json.md
```

## 4. Run on the issue

```powershell
swarmforge run https://github.com/<you>/swarmforge-sample/issues/1
```

SwarmForge fetches the issue, clones the repo, plans, runs the agents, tests and reviews. When the
gate approves it asks:

```
>>> Push branch 'swarmforge/issue-1-…' to <you>/swarmforge-sample and open a DRAFT pull request against 'main'? [y/N]
```

Answer `y` to get a draft PR whose description includes the test evidence, acceptance-criteria
status, review summary, cost and intervention count. `--yes` pre-approves (still recorded as an
intervention). If the token was missing during the run, the run ends `COMPLETED` with
"PR not created" and you can publish later with `swarmforge publish <run_id>`.

## 5. Your own repositories

* Make sure the tests run in the sandbox: the default image has only Python + pytest and **no
  network**. For projects with dependencies, build a derived image:
  ```dockerfile
  FROM swarmforge-sandbox:latest
  USER root
  COPY requirements.txt /tmp/r.txt
  RUN pip install --no-cache-dir -r /tmp/r.txt
  USER agent
  ```
  `docker build -t myproj-sandbox .` and set `sandbox.image: myproj-sandbox`.
* Set `project.test_command` to what runs your tests (`python -m pytest -q tests/unit`, …).
* Start with small, well-specified issues with clear acceptance criteria.

## 6. Benchmark single agent vs swarm

```powershell
swarmforge bench samples\issues\export-csv-json.md --repo $HOME\swarmforge-sample --acceptance samples\acceptance\export
```

Both modes start from the same commit; afterwards the hidden acceptance tests (never shown to any
agent) are run against each result. Results go to the terminal, `~/.swarmforge/benchmarks/`, and
the dashboard's Benchmarks view. Run it several times — one run proves little.
