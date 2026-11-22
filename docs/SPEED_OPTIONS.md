# Speed, routing and escalation options

Added 2026-11-11. Everything here is opt-in: the Python defaults are off, so existing configurations
behave as before. None of it has been measured on real models yet. Run the suite before and after
and keep an option only if success rate holds while time or cost drops:

```sh
swarmforge bench-suite bench/suite.yaml --runs 3 --modes single reviewed swarm --budget 20
```

| Setting | What it does | Risk to watch |
|---|---|---|
| `--mode auto` / Workflow "Auto" | One planner-model call picks `reviewed` (one developer + tester + reviewer) or `swarm`. No usable answer means `reviewed`. | Under-routing a large task costs a repair cycle. |
| `limits.direct_repairs` | A repair cycle with at most `direct_repair_max_problems` non-review problems becomes one developer task (`D<cycle>`); reviewer findings keep their mandatory `R..` tasks. No fix-planner call. A task that never completed still goes to the planner. | One developer gets every problem, even cross-module ones. |
| `limits.parallel_review` | After the acceptance tests are merged, the reviewer runs while the full suite runs. The gate still needs both. | The reviewer no longer sees the test result in its prompt. |
| `limits.tester_with_developers` | Experimental. In swarm mode the tester starts with the developers and writes tests from the acceptance criteria alone (`tester-c0-early`). If it produces no report, the normal tester runs after integration. | Tests written blind may not match the interfaces the developers choose. |
| `roles.<dev>.escalation_model` | A retried task, and every task in a repair cycle, uses this model first; the role's `model` and `fallback_models` remain as fallbacks. | Must not be the reviewer's model if you want independent review. |

## Test runners

`project.test_command` gets a JUnit flag automatically for pytest, Vitest, `node --test` and gotestsum.
For any other runner put `{junit}` where the report path belongs. `review.require_junit: true` holds
delivery when no report is produced.

## Pi harness on Windows

The Docker Pi session used to pass the system prompt and the task brief as `docker exec` arguments.
Windows rejects command lines over 32,767 characters with `WinError 206`. The prompts are now written
to `<worktree>/.swarmforge/pi/<token>.system` and `.prompt` and read inside the container.
