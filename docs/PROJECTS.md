# Projects and feature history

Open **Projects → Start a project**, or use the same action in Chat. The kickoff interview asks about
users, capabilities, exclusions, constraints, repository, budget and the team's latitude. It proposes
3–6 milestones with observable done-when checks and cost/time estimates. Repository operations do not
start during this conversation.

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
explicitly confirm them before continuing. Screenshots come from the existing visual capture pipeline;
when none were captured the report says so. A correction keeps the current milestone open and goes
back to the lead. Approving the last report completes the project.

Projects run sequentially within their lane. Multiple projects can run on different repositories,
subject to `limits.max_parallel_queues`. Only one active project may own a repository. Queued single
tasks for that repository wait while the project is active; tasks for other repositories continue.
Direct CLI runs still use the ordinary repository coordination policy, so avoid starting independent
direct runs against a project-owned repository.

The second failure sends the problem back to the lead for replanning. A third failure pauses it for
you. Four rewrites without merged progress count as a task failure. A fixed-spec escalation must record
at least two distinct attempted approaches and a concrete proposal; weak escalations are returned to
the lead without sending a message to the human. Project budget exhaustion or a next-task reservation
that would exceed twice the milestone estimate also pauses only that project. Model spend is measured
using the existing provider-reported/estimated accounting; provider requests can cross a limit by the
cost of the final in-flight response.

The worker recovers terminal project runs after a restart instead of repeating their PR creation.
Pending plans and all approvals/checkpoints are in SQLite. If the worker is no longer running, use
the Work page's **Start queue** or `swarmforge queue work`. Repository/API/model errors appear on the
project card; fix the cause and resume. Pause stops new work; cancel also requests cancellation of
running work. Other queues are unaffected.

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
