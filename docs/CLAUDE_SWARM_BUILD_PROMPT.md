# Build prompt: Claude-only multi-agent dev team

Paste everything below the line into Claude Code (CLI) at the root of a new, empty git repo on the work machine.

---

I want you to build **ClaudeForge**: an autonomous multi-agent software team where every agent is a Claude model running through Claude Code. You give it a GitHub issue (or a local markdown issue file) plus a target repo. A planner breaks the issue into a task graph. Two coding agents work in parallel in isolated git worktrees. Their work is merged. A test engineer writes acceptance tests. The orchestrator runs the tests itself. An independent reviewer reviews the diff. Blocking findings go back to the planner as corrective tasks, for up to 3 cycles. The output is a tested **draft** pull request and an execution report.

This is a port of a personal project that used several non-Anthropic models via an API. Here, use only Claude products: the Claude Code CLI (`claude`), its subagents, hooks and headless mode, and optionally the Claude Code GitHub Action. No other LLM providers and no third-party agent frameworks.

## Environment constraints (work laptop)

- Windows, PowerShell. Python 3.11+, Git and the `gh` CLI may or may not be installed. Check before assuming.
- Docker is probably **not** available. Don't depend on it. Use Claude Code's own permission rules and sandbox for isolation.
- Authentication is my company Claude account, not an API key. Don't hardcode or ask for `ANTHROPIC_API_KEY`.
- My admin may restrict which models are allowed. Detect this and fall back gracefully.

## Step 0: verify before you build

Before writing any code:
1. Run `claude --version` and `claude --help`. Confirm the exact names of these flags in this version: headless (`-p`), `--output-format json`, choosing an agent or subagent per run, `--model`, permission mode flags, and `--allowedTools`/`--disallowedTools`. If you're unsure of a flag, check https://code.claude.com/docs/en/headless.md, https://code.claude.com/docs/en/sub-agents.md and https://code.claude.com/docs/en/cli-reference.md. **Don't guess flag names.**
2. Run one tiny headless call with `--output-format json` and show me the actual JSON. Record which fields hold the result text, token usage, cost and session ID. The cost accounting is built on these real fields.
3. Check for `git`, `gh` (and `gh auth status`) and `python`. Report what's missing and stop for me to fix it.
4. Show me a short plan of the files you'll create and wait for my OK.

## Architecture

**Principle: agents propose, code decides.** State transitions, the approval gate, test verdicts, merges and GitHub publishing are deterministic Python. A model can never declare tests passed: the orchestrator runs them and parses JUnit XML.

### 1. Agent definitions (`.claude/agents/*.md`)

These are installed into the *target* repo at run time, or passed per run. Read the docs and use whichever is cleaner.

| Agent | Model | Effort | Tools | Output contract |
|---|---|---|---|---|
| `planner` | opus | high | Read, Grep, Glob only | JSON task graph: `{tasks:[{id, title, owner: backend\|frontend, files, depends_on, acceptance}]}` |
| `backend-dev` | sonnet | high | Read, Edit, Write, Grep, Glob, Bash (restricted) | Code changes in its worktree plus a final JSON `{summary, files_changed, notes}` |
| `frontend-dev` | sonnet | high | same as backend-dev | same |
| `test-engineer` | sonnet | medium | Read, Grep, Glob; Write/Edit **only under tests/** (enforce with a hook); Bash for pytest | Test files plus JSON `{tests_added, how_to_run}` |
| `reviewer` | opus | high | Read, Grep, Glob only | JSON `{verdict: approve\|changes_requested, findings:[{severity: blocking\|minor, file, line, issue}]}` |

- Make every model and effort level configurable in `claudeforge.yaml`.
- Each agent's system prompt must say exactly which JSON it returns. The orchestrator validates it with a schema and retries once on bad output.
- Coding agents need no network access, and Bash must not be able to run `git push`, `gh`, `curl` and similar. Enforce this with permission deny rules and/or a `PreToolUse` hook, not just with prompt text.

### 2. Orchestrator (Python package `claudeforge/`)

- Each agent call is a subprocess: `claude -p <prompt> --output-format json <agent/model/permission flags>`, with `cwd` set to the right worktree. Wrap it in one function `run_agent(role, prompt, cwd) -> AgentResult` (text, parsed JSON, tokens, cost, session id, duration, exit code).
- Pipeline, as a persisted state machine (SQLite or JSON in `.claudeforge/runs/<run_id>/`):
  `INTAKE → PLAN → APPROVE_PLAN (human y/n unless --yes) → IMPLEMENT (backend + frontend in parallel, separate git worktrees on claudeforge/<run>/<task> branches) → MERGE (into claudeforge/<run> integration branch; on conflict, send a resolve task to one dev) → TEST_WRITE → TEST_RUN (orchestrator runs pytest --junitxml) → REVIEW → (blocking findings or failed tests → corrective tasks to PLAN, max 3 cycles) → APPROVE_PUSH (human) → PUBLISH (push + gh pr create --draft) → REPORT`
- Respect `depends_on` in the task graph. Independent tasks run concurrently (default 2 at once).
- Budget guard: `--budget <usd>` stops gracefully when cumulative reported cost would exceed it. If cost isn't reported on my plan, track tokens and label any cost as an estimate.
- Never force-push, never merge to the default branch, never push without explicit approval.

### 3. Hooks (`.claude/settings.json` in the target repo during a run)

- `PreToolUse`: block writes outside the agent's allowed paths (test-engineer → tests/ only) and block dangerous Bash.
- `Stop`/`SubagentStop` (check the real hook names in the docs): log the event to the run log.
- Keep hook scripts in Python, Windows-friendly.

### 4. CLI (`claudeforge`, via Typer or argparse)

```
claudeforge check                       # verify claude, git, gh, models allowed, a live headless call
claudeforge run <issue-url|--issue-file F> --repo PATH [--yes] [--budget 5] [--max-cycles 3] [--single]
claudeforge runs | status [RUN] | logs -f [RUN] | cancel RUN
claudeforge report [RUN] -o report.md   # per-agent actions, tokens, cost, test results, review, every human intervention
claudeforge publish RUN                 # push + draft PR for an approved run
```

`--single` runs one Claude agent end to end as a baseline, so I can compare swarm vs. single agent on the same issue.

### 5. Sample and tests

- `samples/make_sample_repo.py` creates a small Python app (e.g. a todo CLI), and `samples/issues/` holds 2 issue markdown files, so I can rehearse with no GitHub access.
- Unit tests for the state machine, task-graph validation, JSON parsing and budget logic, with `run_agent` mocked. Don't spend real tokens in tests.

## Build order

1. Step 0 checks → show me the results.
2. Agent files + `run_agent` + `claudeforge check`. Demo one planner call on the sample repo.
3. State machine through IMPLEMENT + MERGE on the sample repo.
4. TEST_WRITE/TEST_RUN/REVIEW + the corrective loop.
5. Report, then GitHub publish (last, and only after I confirm gh auth works on this machine).

After each stage, run it on the sample repo, show me the output, and wait for me before the next stage. Keep a `README.md` and `docs/DECISIONS.md` updated with what you chose and why, especially wherever the real CLI behaviour differed from this spec.

## Optional later

- Try the experimental agent teams feature (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`, interactive only) as an alternative "swarm mode" and compare it against the orchestrator.
- A GitHub Action mode using the Claude Code GitHub Action, if my company allows it.
