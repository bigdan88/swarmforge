# Projects and feature history

Open **Projects → New project**, or **Start a project** in Chat. Describe your idea in the kickoff form;
users, capabilities and constraints can be left for the lead to propose. It can ask one short round
of product questions, shown as fields or choices, before drafting 3-6 milestones with observable
done-when checks and cost/time estimates. The proposal opens for review when it is ready.

Optional specification details get editable defaults. `projects.default_repo_owner` selects the GitHub
account or organization for new repositories; the lead proposes the repository name. The proposed
allowance defaults to `projects.default_budget_usd` ($30). Hosting defaults to a local preview, with
approval required for scope changes, paid services, credentials or public deployment. You do not need
to answer setup trivia or type `None`. A malformed model draft gets one internal repair attempt;
if the lead still cannot finish it, a labelled starter proposal is prepared from the brief for review.
An existing proposal is retained if a revision cannot be completed.

These are proposal defaults, not permission to spend. Repository operations do not start during the
conversation; review the repository, scope and allowance before approving launch.

Review the rendered proposal in **Approved spec**. Continue the conversation to refine it, or expand
**Edit the proposal before approval**. **Approve and launch** authorizes repository work, task runs,
publication and automatic merging under the existing merge-review rules, within the approved budget.
New repositories are private GitHub repositories. Existing repositories must be accessible with the
configured GitHub credentials. Branch protection still applies; a protected spec commit can require
administrative setup before the project can run.

The lead uses `roles.lead` when configured and otherwise uses `planner`. It reads the current default
branch, representative source files, project spec, recent reports, landed diff and remaining plan.
It makes a short structured planning call rather than starting a new agent runtime. Each build task
is an ordinary swarm run in the project's own queue lane. A spike is a single-agent run with no PR
publication; its report feeds the next plan.

The lead can also request bounded repository/evidence inspection, investigations, independent review,
milestone evaluation, a pause or a concrete question. Application code validates each typed action and
retains its evidence and outcome in **Projects → Plan** and `swarmforge project status`. Decision
calls share a persisted cap; they cannot repeat inspections or rewrite the plan indefinitely. Browser
verification requests remain unverified until a journey runner is available. Unattended mode can carry
those observations to the final preview review while building the remaining software.
See [validated lead actions](LEAD_ACTIONS.md) for contracts, defaults and recovery behavior.

Only the short horizon is ready to run. Tentative tasks are visible in Projects and greyed on Work,
but the worker never starts them. The lead replaces the pending horizon after a merge. Completed and
failed attempts remain in the project history. A completed run does not unlock later work until
GitHub confirms its PR merged into the default branch. A held PR can get a seeded replacement in the
same project lane, rather than a detached run that escapes project accounting.

The approved spec lives at `docs/project-spec.md` in the target repository. Code compares every byte
before `## Living` before accepting a lead update. It also refuses to publish a task that changes or
removes the approved Fixed section. The lead must preserve decision and plan-change history. Humans
can approve a revised fixed spec at a checkpoint; completed milestones and the repository identity
are preserved. A repository migration or a different milestone count requires a separate project.

A milestone report shows its checks, available light/dark screenshots, plan changes, elapsed time,
spend against estimate and the next milestone. Automatic check evidence comes from recorded acceptance
and passing test results of the latest merged task. Unverifiable checks say **needs you**, not passed;
in manual mode, explicitly confirm them before continuing. Screenshots come from the existing visual capture pipeline;
when none were captured the report says so. A correction keeps the current milestone open and goes
back to the lead. Approving the last report completes the project.

Projects run sequentially within their lane. Multiple projects can run on different repositories,
subject to `limits.max_parallel_queues`. Only one active project may own a repository. Queued single
tasks for that repository wait while the project is active; tasks for other repositories continue.
Direct CLI runs still use the ordinary repository coordination policy, so avoid starting independent
direct runs against a project-owned repository.

The second failure sends the problem back to the lead for replanning. In manual mode a third failure pauses it for
you; unattended mode first tries its bounded additional recovery rounds. Four rewrites without merged progress count as a task failure. A fixed-spec escalation must record
at least two distinct attempted approaches and a concrete proposal; weak escalations are returned to
the lead without sending a message to the human. Project budget exhaustion or a next-task reservation
that would exceed twice the milestone estimate also pauses only that project. Model spend is measured
using the existing provider-reported/estimated accounting; provider requests can cross a limit by the
cost of the final in-flight response.

The worker recovers terminal project runs after a restart instead of repeating their PR creation.
Tasks and their retries/replacements now share a persisted spending allowance and wall-clock deadline.
See [bounded execution and recovery](EXECUTION.md) for progress detection, recovery caps, waiting-time
semantics and the handling of uncertain publication. Project and milestone limits remain separate.
Pending plans and all approvals/checkpoints are in SQLite. If the worker is no longer running, use
the Work page's **Start queue** or `swarmforge queue work`. Repository/API/model errors appear on the
project card; fix the cause and resume. Pause stops new work; cancel also requests cancellation of
running work. Other queues are unaffected.

Internal lead response errors are repaired automatically within the existing bounded decision
allowance, with precise feedback and valid examples. The lead can continue an already accepted
queued task without replacing the rest of the plan. **Planning and recovery** on the project page
explains rejected proposals and offers **Retry planning automatically**, with an optional clarification,
when a planning round exhausts its attempts. This retains the approved scope, remaining allowance
and rejection history. Uncertain external actions and genuine milestone confirmations still need
their specific recovery/decision path. **Edit approved scope** exposes the human amendment editor
at a pause; it preserves the repository and completed milestones and does not resume by itself.

The decision panel names the stopped task, shows its actual stopping reason and last tested
acceptance results, and explains what each action permits. Removed pending plans are history,
not failed builds. **Verify & fix automatically** keeps a milestone open for evidence gathering
and repairs. **I verified these checks — accept milestone** explicitly confirms the listed
unverified checks and advances the milestone; it does not require extra checkboxes.
When every check has passing recorded evidence and the approved spec has `ask_before: None`,
the milestone advances automatically. Missing evidence and unavailable physical checks never pass
on an agent's assertion. Build criteria should be machine-testable; human observations and report
instructions belong in descriptions and open questions. The lead is instructed to use exact approved
milestone check text when a build can prove it, so evidence can be linked without fuzzy matching.

An exhausted task offers a bounded recovery extension: approve a specific additional USD amount
and recovery time window, then the lead revises the plan. The extension retains the same execution
lineage, previous spending, tests and event history. It cannot exceed remaining project/milestone
allowances or restart cancelled, active or uncertain work. Old decision tokens are rejected.
Launch, amendments, task launches, retries and queue start use explicit action buttons with nearby
allowance explanations instead of a second authorization checkbox.

`projects.min_build_budget_usd` sets a minimum for newly planned build tasks: $2 by default (one
full team attempt costs about $0.70-1.10, so less cannot finish even once); 0 turns it off. Research
spikes may remain cheaper. Application code applies this configured
floor even if the lead proposes less, and records the adjustment alongside the original proposal.
A task's acceptance criteria are sorted when it is planned (`criteria.py`): what a test can prove
stays a gate; what only a person can check (a browser, a phone, the live site, "looks good", what the
report says) is listed under "For a person to check after merge" and never fails the task. When a
run's only remaining problems are criteria no test proves (tests pass, review is clean), it stops
without spending repair cycles, and it is re-planned instead of retried unchanged.
In manual mode, existing shared task allowances require an explicit recovery extension. Unattended
mode may reallocate approved project funds under the bounded policy below. The total project cap
continues to apply.

## Unattended preview delivery

Set `projects.autonomous: true` to work toward a usable preview without routine milestone replies.
The sample configuration enables it; older configurations retain manual checkpoints until the owner
selects this mode. Launch approval, the fixed specification, merge barriers and the total project cap
remain enforced. `auto_recovery_rounds` defaults to 2 and `auto_retry_delay_s` to 15 seconds.

- Valid future dependencies between tentative tasks are accepted; ready tasks still need completed or
  earlier ready prerequisites. Acyclic tasks supplied in the wrong order are sorted by application code.
- Invalid planning responses get bounded extra rounds with precise error feedback, without fabricated
  owner replies. Early clarification requests first get an opportunity to use reversible assumptions,
  in-scope alternatives or fixtures. Persistent problems eventually produce a concrete question.
- Exhausted task allowances can grow from remaining approved project funds, retaining the same lineage,
  previous spending and evidence. Extensions are limited per lineage and applied once per failed attempt.
  Milestone estimates can be reallocated within the unchanged total cap, with an audit event.
- Retriable provider timeouts and safe planner interruptions are retried with conservative cost retention.
  Environment-paused work gets bounded rechecks; explicit owner pauses are preserved. Graceful worker
  restarts finish an active lead turn. An idle worker waits for its recovery callback before exiting.
- Failed work can seed the next attempt from its retained integrated code. A replanned task with the same
  problem key can reuse a matching retained attempt; unrelated split tasks do not inherit that code.
- Missing milestone evidence gets one automatic verification pass. If observations remain unavailable,
  the milestone is labelled `verification_pending`; the swarm can build later milestones with fixtures.
  These checks remain unverified and are gathered in the final preview review. Failed checks are never
  accepted as passing. A project with deferred observations stays at a final checkpoint until owner review.
- Resolved planning rejections are labelled **Corrected automatically** in **Recovery history**, separately
  from the current project state and the current planning round.

Cancellation, pending/uncertain publication, active task owners and unresolved model reservations are
not replayed by this policy. A model reply whose outcome is unknown but whose conservative cost was
already recorded may be retried within the remaining cap; it is separate from uncertain publication.
Changing scope, obtaining credentials, increasing the total project cap or accepting final unverified
observations still requires the owner. Disabling autonomous mode returns to ordinary checkpoints.

```sh
swarmforge project new --name "Inventory"
swarmforge project chat PROJECT_ID --reply "An inventory app for small shop owners…"
swarmforge project status PROJECT_ID
swarmforge project approve PROJECT_ID
swarmforge project pause PROJECT_ID
swarmforge project resume PROJECT_ID --reply continue --confirm-manual
swarmforge project cancel PROJECT_ID
```

For scripted setup, `project new --definition definition.json` accepts the same structured definition
the kickoff produces: `name`, `goal`, `users`, `repo` (owner/name), `repository_kind` (new/existing),
`out_of_scope`, `stack`, `hosting`, `deadline`, `latitude`, `ask_before`, `budget_usd`, and `milestones`.
Each milestone has `name`, `goal`, `checks` (2–5 strings), `estimate_usd` and `estimate_hours`.
This creates a draft only. `approve` is still required. Creating test/demo projects does not authorize
spending money on a real build.

## What's new

**What's new** is a feature history for the installed build. It explains what changed, why it helps,
where to find it and links to the source change. Search and category filters work offline; **Mark all
as read** clears the new-highlight count for this dashboard database. Preview entries are labelled.
It is separate from Work's run history: an attempted task is not a released feature.

Every feature PR should update `src/swarmforge/resources/feature-history.json` with a stable unique
`id`, ISO `date`, `category`, `title`, plain-language `summary`, `benefit`, `where`, optional dashboard
`page`, source `url`, and `preview` when not merged yet. Keep the newest entries first. Remove the
preview flag when the change ships. The file is packaged in wheels, so the page works without a git
checkout or access to GitHub. Keep descriptions about user-visible behavior rather than commit titles.

Project state transitions live in `projects/service.py`, format protection in `projects/spec.py`,
persistence in `projects/store.py`, read/write GitHub operations in `projects/repository.py`, and model
prompts/calls in `projects/lead.py`. Tests use fake adapters without calling real models or changing
repositories. Local repository projects, independent tasks running in parallel within a project,
and automatic repository migration are intentionally excluded from this conservative first version.
