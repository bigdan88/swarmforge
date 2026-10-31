# Example benchmark: single agent vs swarm (real run, 2026-10-30)

Issue: `samples/issues/export-csv-json.md`, run on the sample inventory app. Both runs cloned the
same commit. Afterwards the 13 hidden acceptance tests in `samples/acceptance/export/` (never
shown to any agent) were run against each result branch in the Docker sandbox.

Command:

```
swarmforge bench samples/issues/export-csv-json.md --repo ~/swarmforge-sample \
    --acceptance samples/acceptance/export --modes single --include-run 20261030-170218-a2b205
```

| Metric | single (DeepSeek-V4-Pro) | swarm (5 roles) |
|---|---|---|
| Final state | COMPLETED | COMPLETED |
| Elapsed | 75 s | 50 min 34 s |
| Cost (DeepInfra-reported) | $0.0485 | $0.7069 |
| Total tokens | 149,954 | 2,459,540 |
| LLM calls | 17 | 190 |
| Corrective iterations | 0 | 2 |
| Human interventions | 0 | 0 |
| Own test suite, last run | 34 passed | 98 passed |
| **Hidden acceptance tests** | **13/13** | **13/13** |

## Where the swarm's time and money went

| Role | Model | Calls | Model time | Cost |
|---|---|---|---|---|
| tester | MiniMax-M3 | 63 | 24.1 min | $0.094 |
| reviewer | GLM-5.3 | 26 | 15.8 min | $0.320 |
| planner | Qwen3.5-397B | 14 | 5.9 min | $0.081 |
| developer_backend | DeepSeek-V4-Pro | 49 | 3.1 min | $0.140 |
| developer_frontend | Kimi-K2.6 | 38 | 1.4 min | $0.072 |

## Reading the result

* On this small, well-specified issue, the specialized swarm brought **no gain on the hidden
  acceptance tests** and cost about 15× more money and 40× more time than one agent.
* About 80% of the swarm's time was quality control: the test engineer (24 min) and the
  reviewer (16 min). The coding itself ran in parallel and took a few minutes.
* The swarm did produce things the single agent didn't. GLM found a real defect: a raw `OSError`
  could escape the actual file write, which the issue explicitly forbids. Then, in cycle 2, it
  found a subtler one: `Path.exists()` raising `PermissionError`. DeepSeek fixed both and added a
  regression test. The hidden tests don't probe those paths, so the benchmark scores don't show
  that extra robustness. MiniMax also added a 567-line acceptance suite that the reviewer
  rightly flagged as largely duplicate.
* One run of each is an anecdote. Next experiments worth trying: harder multi-module issues where
  parallelism and review should matter more; a cheaper or faster reviewer/tester
  (`GLM-5.3-Flash`, `DeepSeek-V4-Flash`); `max_repair_cycles: 1`; running the tester only in the
  first cycle; and several repetitions per configuration.
