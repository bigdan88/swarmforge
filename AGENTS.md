# Working on SwarmForge

Keep user-visible feature history current. Every new feature should add or update an entry in
`src/swarmforge/resources/feature-history.json`: explain the behavior, why it helps, and where the
user finds it. Use a stable ID and an accurate date/source link. Label unmerged work `preview`;
do not describe successful task runs as shipped features. Remove the preview label when shipping.
Bug fixes that materially change user-visible behavior can also have a highlight. Avoid commit-title
jargon. The What's new page is intended for people who do not follow development chats.

Projects reuse the existing run queue and publication path. Read `docs/PROJECTS.md` and
`docs/PROJECTS_DESIGN.md` before changing project behavior. Preserve the fixed-spec byte guard,
merge barriers, evidence retention, per-project budget accounting and explicit human launch approval.
Tests must use fake models/repository adapters; do not launch paid builds or create real repositories
as validation. Keep user runtime configuration, databases, credentials and backup files out of commits.

Run affected tests, `python scripts/check_docs.py`, and the checks in `.github/workflows/checks.yml`
appropriate to the change. Project invariants are in `tests/test_projects.py`; ordinary queue tests
also matter when changing scheduling. Review new dashboard pages in both themes and at phone width.
