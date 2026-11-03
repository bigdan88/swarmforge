# Development checks

Use Python 3.12 and install the tested Linux dependency set:

```sh
pip install -c src/swarmforge/resources/constraints-linux.txt -e '.[dashboard,dev,tools]'
python -m playwright install --with-deps chromium
python scripts/check_docs.py
ruff check src/swarmforge scripts/check_docs.py --select E9,F63,F7,F82
mypy --follow-imports=silent --ignore-missing-imports src/swarmforge/agent/harness.py src/swarmforge/services
python -m pytest -q -m 'not slow' -n 4
```

The constraints capture the existing Linux dev image, including transitive packages. They are
not a cross-platform lock or an OS/browser image digest. Windows developers can use the normal
editable install; use the Linux sandbox for the authoritative platform checks. Upgrade constraints
deliberately, rebuild both images, run the checks and review the resulting version changes.

For container end-to-end validation:

```sh
swarmforge sandbox build --dev
python -m pytest -q tests/test_e2e.py
```

CI runs the fast checks and Docker end-to-end checks separately and uploads JUnit evidence.
Neither uses real model credentials. The end-to-end suite scripts model responses while using
real worktrees, orchestration and test execution. Live comparisons use `bench-suite` and require
a separately chosen API budget.

Keep lifecycle transitions in the orchestrator, pure assessment in `services/assessment.py`,
delivery operations in `services/delivery.py`, and page rendering in `dashboard_pages/`.
Pass page dependencies explicitly; avoid importing the Streamlit entrypoint from a page module.

`bench/smoke.yaml` is the small fixture for scripted variant-evolution tests; the live
`bench/suite.yaml` has separate behavioral tasks and hidden acceptance tests. Keep those tests
outside repository templates. Changes to a case's contract should update both its issue and hidden
tests; do not loosen hidden tests just to improve a model's score.
