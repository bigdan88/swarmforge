# Swarm quality and maintainability improvement plan

Proposals based on the repository and local configuration inspected on 2026-11-02.
These are experiments and planned changes, not implemented features or measured improvements.

## Improve the swarm

### 1. Establish a useful evaluation set before changing the team

`bench/suite.yaml` currently contains one sample case. The historical export benchmark passed
13/13 hidden tests in both modes, while the swarm took about 50 minutes and $0.71 versus
75 seconds and $0.05 for one agent. That establishes overhead on one small task, not a general
quality ranking. Keep the original report as historical evidence.

Add 10-20 cases spanning isolated bug fixes, multi-module features, UI work, dependency changes,
and integration conflicts. Keep acceptance tests hidden, pin each starting commit, and repeat
each configuration at least three times. Compare the current team against a single developer
using the same developer model and a reduced developer-plus-reviewer workflow. The reduced
workflow would need implementation if not representable by an existing mode.

Track hidden-test success, regressions, delivery rate, median and tail elapsed time, cost per
successful task, repair cycles, and human interventions. Distinguish test success from externally
reviewed correctness. Change one setting at a time; retain a candidate only when its quality and
cost tradeoff is supported across several cases.

### 2. Spend coordination effort where it helps

Prototype task routing: small, independent changes get one developer and a short independent
review; cross-module work gets a dependency plan and concurrent developers. Preserve the final
orchestrator-run test evidence in both paths. Start by comparing the existing single and swarm
modes manually before building an automatic router.

The planner should split along interfaces and ownership, with explicit inputs, outputs, files,
and acceptance criteria. Avoid two agents editing the same module simultaneously. The local
configuration uses `same_repo: parallel`; evaluate `coordinate` against it, measuring merge
conflicts, blocked time and successful deliveries rather than maximizing agent count.

### 3. Reduce redundant testing and review

Use the existing targeted agent tests and tester-once option, while retaining the full final
suite as evidence. Give the tester existing coverage and ask for tests of uncovered acceptance
criteria, rather than a parallel duplicate suite. Give the reviewer the diff, test evidence and
specific risk areas; in repairs, focus on unresolved findings and the corrective diff.

Measure phase time and the rate at which reviewer findings produce useful fixes. Do not shorten
review solely because it is expensive: compare escaped defects as well as minutes saved.

### 4. Make delivery policy match confidence

For a conservative trial, block both critical and major findings, hold protected paths, and
disable auto-merge for core changes. The current local policy instead blocks only critical
findings and allows a judge to accept protected changes and major findings. Compare policies
on the evaluation set before expanding autonomous delivery.

Add explicit checks for unmapped/unexecuted acceptance criteria and missing test evidence.
`TestEvidence.ok` currently accepts exit zero when no JUnit report is available; `_gate` blocks
criteria marked failed. Decide and document whether missing evidence should hold delivery,
with an explicit adapter policy for projects that do not use pytest.

### 5. Turn failures into targeted repairs

Use existing post-mortems and metrics to distinguish environment failures, tool errors, model
timeouts, incorrect implementations and integration failures. Retry transient infrastructure
problems; add a focused reproduction test and corrective brief for code failures. Evaluate
whether escalating time and budget actually increases successful deliveries before broadening
the existing self-heal limits.

Curate the knowledge vault: deduplicate repeated lessons, retain source run links, and evaluate
retrieval with knowledge enabled versus disabled. A growing note count is not proof of learning.

## Improve maintainability

### 1. Extract dashboard pages and application services first

`dashboard.py` is roughly 3,000 lines. Move pages into a `dashboard/pages/` package and shared
rendering into small components. Put start/retry/cancel/approve/merge operations in application
services that the dashboard and CLI both call. Keep Streamlit session state and widgets in the
UI layer. Extract one page at a time and preserve page URLs and user behavior.

Validate each extraction with the existing page/action tests and relevant UI preview. Avoid
rewriting the dashboard while reorganizing it.

### 2. Reduce the orchestrator's responsibilities without distributing state ownership

`orchestrator.py` is roughly 1,760 lines. Extract test/review assessment, delivery and auto-fix
coordination into services with explicit inputs and results. Keep lifecycle transitions in one
owner so it remains possible to follow a run from start to finish.

Use typed result objects for phase evidence, findings, delivery outcomes and retry decisions.
Prioritize boundaries currently passing loosely structured dictionaries. Preserve cancellation,
budget and failure semantics; validate with existing scripted end-to-end runs, including repair,
budget exhaustion and cancellation.

### 3. Define one contract for built-in and Pi agents

Specify a small runtime interface for starting a role, recording events, receiving results and
cancelling execution. Keep provider metering, policy and tool permissions outside harness-specific
code. Document capability differences explicitly, including Pi session networking and file access.
Use shared contract tests for both runtimes where their behavior is meant to agree.

### 4. Make configuration and documentation easier to keep consistent

The local configuration is intentionally git-ignored and differs from `swarmforge.example.yaml`.
Add a redacted effective-config export that includes selected models, runtime, networking,
approval, review severities and merge policy. Show this summary before a run and record it with
the run. Never include API keys or relay tokens.

Consider a committed, validated team profile for the current model/runtime choices, keeping
machine paths, credentials and operational approval settings local. Generate the README team
table from that profile, or add a small consistency check. Keep historical benchmarks dated.

### 5. Establish repeatable checks and dependency versions

Add CI checks for the fast suite and documentation links, with Docker end-to-end checks in a
separate job. Introduce lint/type checks incrementally at changed boundaries; avoid a large
formatting-only diff. Lock or constrain tested dependency versions for reproducible development
and sandbox builds, with deliberate upgrades. Name maintainers' required checks in the contributor
documentation so local and CI validation agree.

## Suggested order

1. Expand the benchmark set and capture the current team's baseline.
2. Compare coordinated repository scheduling and stricter delivery policy in controlled runs.
3. Extract one dashboard page plus its application service.
4. Extract gate assessment and delivery from the orchestrator, preserving behavior.
5. Introduce an effective-config summary and a reproducible CI environment.
6. Use the measured results to decide whether automatic task routing is worth implementing.
