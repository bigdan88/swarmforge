# Security model and limitations

SwarmForge runs code written by language models, driven by text (issues, repository files) that
anyone might have written. Treat it as running untrusted code. The safeguards below reduce risk;
they do not eliminate it.

## What is enforced in code

| Risk | Safeguard |
|---|---|
| Agent commands touching the host | In Docker mode, commands execute in disposable containers, with long-lived per-agent sessions when enabled. Containers use `--cap-drop ALL`, `--security-opt no-new-privileges`, a read-only root filesystem, a size-limited `/tmp`, process/memory/CPU limits and a non-root user; no Docker socket or host environment is passed through. Worktrees and explicit artifact/cache mounts provide writable storage. Built-in sessions and orchestrator tests use `sandbox.network` (default `none`); Pi sessions use `pi.network` (default `bridge`). Cleanup handles session completion, timeout and cancellation. Local mode has no container isolation. |
| Agents editing files outside their workspace | File tools resolve paths and reject absolute paths, `..` escapes, symlink escapes and anything under `.git`. Each worktree is leased to one task. |
| Overwriting the user's work | The source repository is never modified: SwarmForge clones it into `<workdir>/runs/<id>/repo` (uncommitted local changes are not even copied, and a warning is logged). |
| Credential leakage | API keys live only in the orchestrator process (`.env` / environment). They are never written into workspaces, never passed to containers, and `LocalSandbox` strips every env var whose name contains KEY/TOKEN/SECRET/PASSWORD/CREDENTIAL/AUTH. The GitHub token is passed to `git push` as a one-off `http.extraheader` (never stored in `.git/config`) and is redacted from error messages. |
| Protected branches / destructive pushes | Branches must start with `github.branch_prefix` (`swarmforge/`); names like `main`, `master`, `develop`, `release` are refused; pushes are never forced and SwarmForge refuses to push if the remote branch already exists. |
| Unreviewed changes reaching the repo | Delivery creates draft PRs after the configured gate passes. Push approval is enabled in the example but can be disabled or pre-approved. Auto-merge is off by default; when enabled for a repository, `automerge.py` can merge eligible PRs after rule checks and, where required, model judgment. The dashboard also supports manual merges. Protected files and major findings can be configured to hold or go to the judge; they are not unconditional vetoes. |
| LLMs faking success | Test results come from the orchestrator executing the test command and parsing JUnit; a run with 0 executed tests is not a pass. State transitions are code-owned and allow-listed. Reviewer and implementer models must differ unless explicitly overridden. |
| Prompt injection via issue/repo text | Prompts label issue/repo/tool content as untrusted. Container restrictions and code-owned gates apply independently of model instructions. However, Pi bridge sessions already have network connectivity, and model judgments can affect review and auto-merge decisions. File-tool path checks apply to SwarmForge's file tools; shell execution is bounded by the container and its mounts. |
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
* **Network access depends on the execution path.** Built-in sessions and orchestrator test
  containers use `sandbox.network`, default `none`. Pi uses `pi.network`, default `bridge`, to
  reach the host model relay; shell commands and `sf` tools in that session share its network.
  Bridge mode is not restricted to the relay and can permit internet and reachable host/LAN
  access. `PI_OFFLINE=1` is a harness setting, not a network firewall. For offline agent execution,
  select `runtime: builtin` and `sandbox.network: none`. Restricting Pi to only the relay requires
  additional network controls outside the current implementation.
* **The dependency install step also has network access.** `pip install` runs packages' build code,
  and agents choose what goes into requirements.txt, so a prompt-injected agent could pull a
  malicious package. That code runs in the same locked-down container (no credentials,
  non-root, read-only root fs, only the worktree mounted) but can reach the internet during the
  install. Set `sandbox.dependency_install: false` (and use a custom image with dependencies
  preinstalled) to disable this path. This does not disable Pi session networking.
* **The Pi relay is a host service.** Pi receives a per-agent relay token, not the provider key.
  If `pi.relay_host: 0.0.0.0` is needed for Docker Desktop, the relay listens on all interfaces;
  use host firewall rules to limit who can reach it. A relay token is still a credential and should
  not be shared or published in artifacts.
* **Local policy can allow autonomous delivery.** As of 2026-11-02, this checkout's git-ignored
  `swarmforge.yaml` disables push approval, blocks only critical review findings, and enables
  auto-merge for `bigdan88/swarmforge`, with protected files and major findings sent to a judge.
  This differs from the example configuration. Review the effective configuration before relying
  on human approval or a particular finding severity as a gate.
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
