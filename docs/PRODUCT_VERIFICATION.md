# Verify the application, not only its unit tests

Product requirements need observed application behavior. Passing mocked unit tests
does not prove that audio was transcribed, objects were detected or a browser
played a recording. SwarmForge keeps those evidence levels separate.

| Evidence level | What it can establish |
| --- | --- |
| `unit` | Code behavior, including isolated tests with fakes |
| `integration` | A declared check executing real components and inspecting output |
| `product` | A declared journey through the application's normal entry point |

Project milestone checks default to `product`. The approved definition may declare
documentation or lower-level checks explicitly, for example a milestone's
`evidence_requirements: {"M1C1": "unit", "M1C2": "integration"}`. These choices
are rendered in the protected Fixed specification. Existing completed milestones
are not rewritten. When an existing project is evaluated again, old untyped
passing tests remain unit evidence and cannot establish product readiness.

## Stable requirements and frozen expectations

Each approved milestone check has an ID: `M1C1` means milestone 1, check 1. The
lead assigns `check_ids` on build tasks. The planner attaches `requirement_id`
and `evidence_level` to acceptance criteria. Application code restores omitted
assigned criteria and enforces their minimum evidence level. Display text may be
paraphrased; there is no need to create extra tests carrying exact check wording.

Before developers start, the planner proposes `verification_checks`. A check
contains a command and observable output expectations. Stronger criteria without
an adequate check are rejected during planning. Checks are frozen in the run's
`artifacts/verification-contract.json` and retained in the existing store.
Project contracts are scoped to the project and the approved Fixed spec hash.
Later tasks may append checks; they cannot replace an existing check's expected
outcome. Repair cycles rerun the accepted check against the repaired app.

A repository can also supply `swarmforge.verify.json` with a `checks` array. It
is read before coding and combined with already frozen checks, not reread as an
authority after developers or testers change it. This file contains expectations
and fixture paths, not permission or credential settings.

## Example: a short real recording

Adapt the command, local model paths and output schema to the application's actual
interface. A small known clip should be proven before attempting a multi-hour
recording. The fixture must contain the expected spoken phrase.

```json
{
  "checks": [{
    "id": "speech-smoke",
    "criteria": ["M3C1", "M3C2"],
    "level": "product",
    "timeout_s": 120,
    "preflight": {
      "executables": ["ffmpeg", "ffprobe"],
      "modules": ["faster_whisper"],
      "files": ["models/whisper/model.bin"],
      "cuda": true,
      "platform": "Windows"
    },
    "fixtures": ["samples/known-speech.mp4"],
    "steps": [
      {"kind": "command", "argv": ["python", "-m", "my_lifelog", "--input", "samples/known-speech.mp4", "--export", "output.json"]},
      {"kind": "json", "path": "output.json", "pointer": "/transcripts", "min_count": 1},
      {"kind": "file", "path": "output.json", "contains": "the red bicycle"}
    ]
  }]
}
```

This is an interface example, not a claim that MNEMOS currently exports this
schema. Its own contract must use its actual CLI and database tables. Visual
checks need a known fixture with the expected object and timestamp; a nonempty
list alone does not establish accuracy.

## Supported steps

Commands use `argv` arrays without a shell. They may set `background: true` to
start a local server. The runner captures command logs, bounds execution time
and stops the process trees it started, including on assertion failure.

| Kind | Assertion fields |
| --- | --- |
| `file` | `path`, optional `min_bytes` (default 1), optional `contains` |
| `json` | `path`, optional JSON `pointer`, `equals` and/or `min_count` |
| `sqlite` | `path`, read-only `query` starting with SELECT, `min_rows`, optional `contains` |
| `http` | local `url`, expected `status` (default 200), optional `contains` and `headers` |
| `browser` | local `url`, optional `actions`, expected `contains` and/or `media_time_s` |

Browser actions use `kind: click|fill|press`, a Playwright `selector`, and optional
`value`. A playback check waits for the requested media position and verifies
that playback advances. An attachment download does not pass. Browser checks
capture a screenshot and fail on uncaught page errors. Playwright/Chromium must
already be available in the configured sandbox.

Preflight can require binary names, importable Python modules, nonempty local
model files, CUDA and a target platform. The result records runtime details,
fixture hashes, the tested commit, contract hash, commands and observations.
Unit-test runner commands cannot be labelled as product checks.

## Fresh execution and honest outcomes

The runner copies application source to a fresh workspace, excluding original
`data/`, databases, credentials, Git state, virtual environments and caches.
Dependencies use the existing approved installation policy; Python packages are
installed locally into the verification workspace. Each check gets a fresh copy
so one check cannot pass by reading another check's stale output.
Declared file/JSON/SQLite output paths are removed from that disposable copy
before execution, so output left behind by unit tests or a manual run cannot
count as new processing. An output cannot also be a declared input fixture.

| Outcome | Gate behavior |
| --- | --- |
| `passed` | All commands and expected observations passed |
| `empty` | A positive expectation found too few results; blocks acceptance and feeds repair |
| `failed` | Execution or an assertion failed; feeds the existing repair loop |
| `blocked` | A required runtime/model/device/platform is unavailable; records a concrete environment blocker |

An intentionally empty result can be verified with an explicit assertion such
as JSON `equals: []`. Missing backends must never be represented as successful
empty processing. Generated applications are instructed to expose these states
with stage/reason details and keep blocked work retryable.

Passing unit tests cannot override a failed, empty or blocked application check.
`review.require_verified_criteria: false` does not waive an explicitly stronger
evidence requirement. Results from a different tested commit or an older repair
cycle cannot satisfy the current check. Publication still uses existing merge,
review, fixed-spec and approval rules.

## Run checks without agent calls

```sh
swarmforge verify --repo PATH --contract PATH/swarmforge.verify.json --artifacts PATH_TO_EVIDENCE --timeout 300
```

Use an artifacts directory outside the target repository or under its
`.swarmforge/` directory. The command uses the configured sandbox and dependency
installation policy and returns nonzero unless every check passes. It does not
call models, grant host/GPU access, download model weights, expose a server to
the network, or change the approved project scope.

Windows/CUDA requirements remain blocked in a Linux/CPU sandbox. Configure an
appropriate already-authorized runner; do not substitute a fake backend to get a
green report. Dependency installation is not a fresh operating-system image or a
guarantee that a fixture represents all production inputs. A frozen test can
still be weak: reviewers must inspect its expected outcomes and real component
usage. Known-broken variants under `bench/product-verification/` provide an
offline regression benchmark for empty, incorrect and missing output.
