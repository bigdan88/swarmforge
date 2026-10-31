# Letting SwarmForge improve itself

SwarmForge can take an issue about its own code, run the whole team on it, and hand the result
back to your NiBot folder as a git branch. You review it and merge it yourself; nothing changes in
your working copy until you do.

```
dashboard "New run" (repo = NiBot) → disposable clone of NiBot → planner / developers / tester / reviewer
   → SwarmForge's own test suite runs in the dev sandbox → gate approves
   → branch swarmforge/task-<id> appears in NiBot → you review + `git merge`
```

## One-time setup (Windows, PowerShell, in `C:\Users\danie\Projects\NiBot`)

```powershell
conda activate swarmforge

# 1. Make NiBot a git repository (the swarm clones committed code only)
git init -b main
git add -A
git commit -m "SwarmForge baseline"

# 2. Build the dev sandbox (Python + pytest + git + httpx + streamlit), ~2-4 minutes
swarmforge sandbox build --dev
```

`.gitignore` already keeps `.env`, `swarmforge.yaml`, `knowledge/`, `_to_delete/` and build files
out of git, so your key and notes are never copied into a workspace.

`swarmforge.project.yml` (committed in the repo root) tells SwarmForge how to test itself:

```yaml
test_command: PYTHONPATH=/workspace/src python -m pytest -q -p no:cacheprovider tests
sandbox_image: swarmforge-dev-sandbox:latest
```

Only those two keys are ever read from a repository. Network, budget, limits and every other
safety setting come only from your own `swarmforge.yaml`, and a repository can't make
SwarmForge pull an image it hasn't been told to build.

## Each improvement

1. **Commit first.** Uncommitted changes in NiBot are not seen by the swarm.
2. `swarmforge dashboard` → **New run** → *Describe it here*:
   * **Title / description**: the change you want, with acceptance criteria (see example below).
   * **Repository**: `C:\Users\danie\Projects\NiBot`
   * Start with **Max repair cycles 2** and a modest budget ($3–5); SwarmForge's test suite takes
     about 2 minutes per run, so a run takes 30–60 minutes.
3. Watch it under **Runs**. When it completes, the reason line says
   `delivered to branch swarmforge/task-xxxxxx in C:\Users\danie\Projects\NiBot`.
4. Review and merge (stop the dashboard first if the change touches the dashboard):
   ```powershell
   git diff main...swarmforge/task-xxxxxx        # or open the branch in VS Code / GitHub Desktop
   git merge swarmforge/task-xxxxxx
   ```
   Because SwarmForge is installed in editable mode (`pip install -e`), the merged code is live
   the next time you start a command or the dashboard. If the change adds dependencies, run
   `pip install -e ".[dashboard]"` again; if it changes what the sandbox needs, rebuild with
   `swarmforge sandbox build --dev`.
5. Don't like it? `git branch -D swarmforge/task-xxxxxx`. Nothing else was touched.

## Writing good self-improvement issues

The team does best with small, testable changes that name files and interfaces. Example (this
exact issue was completed end to end during development):

```
Title: Add --json output to `runs` and `status` commands

`swarmforge runs --json` prints a JSON array with one object per run: id, mode, state, ...
`swarmforge status [RUN] --json` prints one object with the same fields plus tasks [...]
Unknown run id: print {"error": "run not found: <id>"} and exit with status 2.
Without --json the existing text output must stay exactly the same.
Unit tests in tests/test_cli_json.py ...
Acceptance criteria:
1. ...
```

Good next candidates:
* Stream model responses so the dashboard shows tokens arriving during long planner calls.
* Let the tester skip re-writing acceptance tests in repair cycles when nothing relevant changed.
* A dashboard page that compares runs side by side (cost/time per role).
* A `swarmforge knowledge prune` command that merges near-duplicate notes.

Avoid giant rewrites ("refactor the orchestrator"): they exceed what one plan of ≤8 tasks can
deliver and the reviewer will keep finding problems.

## Safety notes specific to self-modification

* The running SwarmForge never edits itself: agents work on a disposable clone, and the result
  only reaches your code when you merge a branch.
* Review diffs to `orchestrator.py`, `sandbox.py`, `github.py`, `workspace.py` and
  `agent/tools.py` with particular care: those files enforce the safety gates. A green test
  suite is evidence, not proof.
* Merge only when no run is active.
* Optional: push NiBot to a private GitHub repo and run self-improvement from GitHub issues
  instead. You then get a draft PR (with test evidence and agent activity) to review in the
  browser.
