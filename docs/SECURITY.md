# Security model and limitations

SwarmForge runs code written by language models, driven by text (issues, repository files) that
anyone might have written. Treat it as running untrusted code. The safeguards below reduce risk;
they do not eliminate it.

## What is enforced in code

| Risk | Safeguard |
|---|---|
| Agent commands touching the host | Every `run_command` (and every orchestrator test run) executes in a fresh Docker container: only the task worktree is bind-mounted, `--network none`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--read-only` root filesystem with a size-limited `/tmp` tmpfs, `--pids-limit`, `--memory`, `--cpus`, non-root `--user`, no Docker socket, no host environment variables. Containers are killed on timeout and on run cancel/timeout (label `swarmforge.run=<id>`). |
| Agents editing files outside their workspace | File tools resolve paths and reject absolute paths, `..` escapes, symlink escapes and anything under `.git`. Each worktree is leased to one task. |
| Overwriting the user's work | The source repository is never modified: SwarmForge clones it into `<workdir>/runs/<id>/repo` (uncommitted local changes are not even copied, and a warning is logged). |
| Credential leakage | API keys live only in the orchestrator process (`.env` / environment). They are never written into workspaces, never passed to containers, and `LocalSandbox` strips every env var whose name contains KEY/TOKEN/SECRET/PASSWORD/CREDENTIAL/AUTH. The GitHub token is passed to `git push` as a one-off `http.extraheader` (never stored in `.git/config`) and is redacted from error messages. |
| Protected branches / destructive pushes | Branches must start with `github.branch_prefix` (`swarmforge/`); names like `main`, `master`, `develop`, `release` are refused; pushes are never forced and SwarmForge refuses to push if the remote branch already exists. |
| Unreviewed changes reaching the repo | Only draft PRs are created, only after the deterministic gate approves, and by default only after a human types `y` (recorded as an intervention). SwarmForge has no merge code path. |
| LLMs faking success | Test results come from the orchestrator executing the test command and parsing JUnit; a run with 0 executed tests is not a pass. State transitions are code-owned and allow-listed. Reviewer and implementer models must differ unless explicitly overridden. |
| Prompt injection via issue/repo text | System prompts label all issue/repo/tool content as untrusted data. More importantly, the enforcement above does not depend on the model obeying: an injected instruction cannot grant network access, widen file access, push, or change the gate. |
| Runaway cost or time | Shared run budget checked before every model call (provider-reported cost, else estimated); per-agent step and wall-clock limits; whole-run `max_runtime_s`; `max_repair_cycles`; `max_concurrent_agents`. |
| Tester weakening the implementation | Changes the tester makes outside test files are reverted before its work is merged. |

## Remaining limitations (be honest about these)

* **Docker is not a security boundary against a determined attacker.** Containers share the host
  kernel; a kernel or runtime escape would defeat isolation. For stronger isolation run Docker
  inside a VM (Docker Desktop on Windows/macOS already uses one), or use gVisor (`runsc`) / Kata.
* **The worktree mount is writable.** Agent code can write anything into its own worktree,
  including files that execute later on the host if *you* run them (e.g. a malicious `conftest.py`,
  git hooks are not used by SwarmForge but a script you run might be). Review the diff before
  running the result outside the sandbox.
* **`sandbox.mode: local` provides no isolation.** It exists only for machines without Docker.
* **Tests that need network or extra packages** fail under `--network none` with the default
  image. Prefer a custom sandbox image with dependencies preinstalled over enabling
  `network: bridge` (which lets generated code reach the internet).
* **Model providers see your code.** Issue text and repository content are sent to DeepInfra (or
  whichever provider you configure). Don't use SwarmForge on code you can't share with them.
* **The knowledge base is a persistence channel.** Notes written by the librarian are derived
  from issue text and repository content, and future runs read them. A malicious issue could
  try to plant a misleading note. Notes are labelled untrusted in every prompt, carry their
  source (`agent:<role>/<model>`, `user`, `orchestrator`), and are plain files you can review or
  delete in the dashboard or Obsidian. They grant no capabilities: the sandbox, the gates and
  the push approval don't depend on them. Disable with `knowledge.enabled: false`.
* **The dashboard has no authentication.** Streamlit listens on localhost by default. Don't
  expose port 8501 to a network, because anyone who can reach it can start runs (spending your
  API budget) and approve pushes.
* **Prompt injection can still degrade the result** (e.g. convince an agent to write bad code).
  The gates catch failing tests and reviewer-visible problems, not everything. The PR is a draft
  for a reason.
* **The GitHub token is visible to the `git` process** (command line argument) for the duration of
  the push. Use a fine-grained token scoped to the single repository with only Contents and Pull
  requests write access, and an expiry date.
* **Cost limits are best-effort for un-priced models.** If a provider returns no cost and no
  price is known, those calls are counted as "unavailable" and cannot be enforced against the
  budget; `swarmforge check` warns when a model is not in the price list.

## Recommended GitHub token

Fine-grained personal access token, *Only select repositories* → the target repo:
`Contents: Read and write`, `Pull requests: Read and write`, `Issues: Read-only`,
`Metadata: Read-only`. Enable branch protection on your default branch as a second line of
defence.
