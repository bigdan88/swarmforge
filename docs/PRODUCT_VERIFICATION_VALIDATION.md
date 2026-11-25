# Product verification implementation validation

Validated November 25, 2026 against an isolated snapshot of the NiBot checkout.
Existing uncommitted changes were preserved in the snapshot before this work;
the exported patch contains only the product-verification changes.

## Results

- 295 selected tests passed; 20 were deselected (15 slow tests and five Git
  transport tests blocked by the execution environment).
- New coverage includes known broken applications returning success with empty,
  incorrect or missing output; real ffmpeg output-format failure; stale artifacts;
  reused local criterion IDs; missing CUDA/runtime dependencies; frozen contracts;
  fresh workspaces; packaging failures; CLI execution; project evidence and the
  orchestrator's existing repair gate.
- `python scripts/check_docs.py` passed.
- Python compilation passed. Pyflakes passed on the new modules and tests.
- `git diff --check` passed.

The selected regression files were test_product_verification, test_product_gate,
test_units, test_plan, test_lead_actions, test_projects, test_project_autonomy,
test_queue, test_project_decisions, test_project_recovery,
test_project_kickoff_forms, test_repair_scope, test_transactions, test_summary,
test_prompts_planner and test_execution.

## Environment limits

The managed Windows execution environment blocks the TCP socketpair that Python's
asyncio loop normally uses for wakeups. Async tests used a session-only polling
IOCP launcher without TCP connections. That launcher is not shipped, does not
change SwarmForge's runtime, and does not grant network access. Normal CI should
run pytest directly.

Five existing Git clone/push tests failed before reaching assertions because
Git's MSYS subprocess could not create its Windows named object; those tests
were excluded from the final run. Their production implementation was unchanged.

Ruff and mypy are not installed in this execution environment. Full CI, Docker
execution, real browser playback and complete GPU model inference have not been
verified here. Model/repository adapters in SwarmForge's tests are fake; no paid
agent runs, real repository creation, model downloads or system installations
were used as validation. The real CLI benchmark and ffmpeg check used temporary
fixtures. A generated application's GPU/browser checks still require the
appropriate configured runtime and real input fixtures.

The live NiBot checkout is read-only to this session. These changes are prepared
and tested in a separate checkout and require applying the exported patch and
restarting SwarmForge before they affect running projects.

## Follow-up: cached baseline recovery and installer retries

The owner's normal Windows run exposed a missing artifact directory in resumed
and self-healing runs. Their baseline result was cached, so no test invocation
had created the new run's `artifacts/` directory before contract saving. The
contract writer now creates its parent directories itself before saving evidence
and the persisted contract.

New regression tests reproduce the original exception with both empty and real
application contracts, exercise the actual cached-baseline inspection followed
by contract saving, and preserve unrelated existing evidence on repeated calls.

The PowerShell installer now detects fresh, previously patched and already-fixed
checkouts. An incremental repair patch updates an existing installation; an
already-fixed installation only reruns requested validation. Conflicts stop
without overwriting edits. Installer tests use temporary local Git fixtures and
also cover a second invocation after validation failed. The script is compatible
with Windows PowerShell 5.1, which was used for these tests.

Follow-up validation: 54 focused tests passed, with one previously covered CLI
test deselected because of this execution environment's asyncio socket limit.
Documentation, Python compilation, new-module static checks and patch whitespace
checks passed. The two slow queue tests reported by the owner still need rerunning
in the normal Windows environment; this session's Git transport restriction
prevents executing those full cloned-repository workflows.
