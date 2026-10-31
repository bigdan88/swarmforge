# Troubleshooting

Start with `swarmforge check` (add `--live` to exercise each model). Then look at
`swarmforge logs <run>`, the per-agent transcripts in `<workdir>/runs/<run>/transcripts/`, and
test logs in `<workdir>/runs/<run>/artifacts/`.

| Symptom | Cause / fix |
|---|---|
| `config error: no config file found` | Run from the folder containing `swarmforge.yaml`, or pass `-c path/to/swarmforge.yaml`. |
| `environment variable DEEPINFRA_API_KEY is not set` | Create `.env` next to `swarmforge.yaml` (see `.env.example`) or set the variable in your shell. |
| `HTTP 401/403` from the provider | Wrong or revoked key, or no credit on the DeepInfra account. |
| `HTTP 404 … model` / `check` says model NOT in list | Model id typo or model retired. Pick an id from the live catalogue (docs/PROVIDERS.md). |
| `live <role>: model answered without a tool call` | That model doesn't do function calling reliably on this endpoint. Choose another model for that role. |
| Agent ends with `model stopped calling tools` | The model replied in prose three times instead of calling tools. Usually a weak/small model; switch models or lower temperature. |
| Agent ends with `did not finish within N steps` | Raise `roles.<role>.max_steps`, or make the issue smaller. The transcript shows where it looped. |
| `docker binary 'docker' not found` / daemon not reachable | Install/start Docker Desktop (Windows: enable the WSL 2 backend). Last resort: `sandbox.mode: local` (unsandboxed). |
| `sandbox image swarmforge-sandbox:latest missing` | `swarmforge sandbox build`. |
| `sandbox build` fails pulling the base image (429 Too Many Requests) | Docker Hub rate limit. Use a mirror: `sandbox.base_image: mirror.gcr.io/library/python:3.12-slim`, or `docker login`. |
| `sandbox build` fails with SSL errors in pip | You are behind a TLS-intercepting proxy. Build a derived image that copies your corporate CA and sets `PIP_CERT`. |
| Baseline tests fail / `no JUnit report` | The repo's tests need packages that aren't in the sandbox image. Build a custom image (FROM swarmforge-sandbox, `pip install -r requirements.txt` at build time) and set `sandbox.image`. Also check `project.test_command`. |
| Tests time out | Raise `sandbox.command_timeout_s`. |
| Docker bind mount errors on Windows | Make sure the `workdir` drive is shared with Docker Desktop (Settings → Resources → File sharing) — the default `%USERPROFILE%\.swarmforge` works out of the box. |
| `BUDGET_EXCEEDED` | Raise `limits.budget_usd` or `--budget`, or use cheaper models. Spend so far is in `swarmforge status`. |
| `FAILED: repair limit reached` | The gates still found blocking problems after `max_repair_cycles`. The report lists them; the result branch is kept in `<workdir>/runs/<id>/repo` for inspection. |
| Merge conflicts | Usually the planner put two parallel tasks on the same file. SwarmForge tries an agent-driven resolution up to `limits.max_conflict_resolutions` times. |
| `refusing to push … already exists` | A branch with that name is already on GitHub; SwarmForge never overwrites. Delete it yourself if it's stale. |
| `approved; PR not created (no push access)` | Set `GITHUB_TOKEN` (or `gh auth login`) with write access, then `swarmforge publish <run_id>`. |
| Push prompt answered automatically "no" | Non-interactive terminal. Use `--yes` to pre-approve (recorded as an intervention). |
| Dashboard shows nothing | It reads `workdir/swarmforge.db` from the config you launched it with; use the same `-c` as for `run`. |
| Ctrl+C in the middle of a run | The run is marked `CANCELLED` (one intervention), containers are killed, and the report is written. From another terminal, `swarmforge cancel <run>` does the same gracefully. |
| `BLOCKED: the repository … has no commits yet` | Brand-new empty repo. On GitHub click "Add a README" (or push an initial commit), then start the run again. |
| `BLOCKED: sandbox image '…' is not built` | The repo's `swarmforge.project.yml` (or the run settings) asks for an image you haven't built. For SwarmForge itself: `swarmforge sandbox build --dev`. |
| `could not deliver branch … already exists` | A branch with that name is already in your local repo; the result stays in `~/.swarmforge/runs/<id>/repo`. |
