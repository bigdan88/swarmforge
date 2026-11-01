"""Plain-language explanation of a run's outcome.

Pure module: no Streamlit, no database, no network. The dashboard renders the
returned :class:`Explanation` as a status card at the top of the run page.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SEVERITIES = ("success", "info", "warning", "error")

# Task states (state.TaskState) that mean the task is actively being worked on.
_ACTIVE_TASK_STATES = ("RUNNING", "PENDING")

# Phases shown while the run is going, keyed by RunState value.
_RUNNING_PHASES: dict[str, str] = {
    "ISSUE_RECEIVED": "The task has been received and is being prepared.",
    "REPOSITORY_INSPECTION": "Inspecting the repository to understand the codebase.",
    "PLANNING": "The planner is breaking the task into small tasks.",
    "TASK_VALIDATION": "Checking that the planned tasks are valid.",
    "TASK_EXECUTION": "Developers are coding.",
    "INTEGRATION": "Merging the developers' work.",
    "TESTING": "Running the test suite.",
    "CODE_REVIEW": "An independent reviewer is checking the diff.",
    "FIX_REQUIRED": "Writing corrective tasks for the problems found.",
    "APPROVED": "Waiting for your approval to push.",
    "PULL_REQUEST_CREATION": "Opening the draft pull request.",
}


@dataclass
class Explanation:
    headline: str
    detail: str
    severity: str = "info"
    actions: list[str] = field(default_factory=list)


def _running_tasks(events: list[dict]) -> int:
    """Count tasks whose LAST task event shows them as still running.

    Tracks the last state per task_key: a task that went RUNNING and then
    SUCCEEDED is no longer running, so it must not be counted.
    """
    last: dict[str, str] = {}
    for ev in events:
        if ev.get("kind") != "task":
            continue
        key = ev.get("task_key")
        if not key:
            continue
        msg = str(ev.get("message") or "")
        state = msg.split(":", 1)[0].strip()
        last[key] = state
    return sum(1 for state in last.values() if state in _ACTIVE_TASK_STATES)


def _explain_running(run: dict, events: list[dict]) -> Explanation:
    phase = str(run.get("phase") or run.get("state") or "")
    text = _RUNNING_PHASES.get(phase, "The run is in progress.")
    if phase == "TASK_EXECUTION":
        n = _running_tasks(events)
        text = f"Developers are coding {n} task{'s' if n != 1 else ''}."
    detail = text
    reason = run.get("state_reason")
    if reason:
        detail = f"{text}\n\nCurrent status: {reason}"
    return Explanation(
        headline="The run is in progress",
        detail=detail,
        severity="info",
        actions=[
            "Keep this page open to follow the progress.",
            "Use Cancel to stop the run.",
        ],
    )


def _explain_completed(run: dict) -> Explanation:
    reason = str(run.get("state_reason") or "")
    budget = run.get("budget_usd") or 0
    if run.get("pr_url"):
        url = run["pr_url"]
        return Explanation(
            headline="Done — a draft pull request is ready to review",
            detail=(f"The run finished successfully and opened a draft pull request: {url}\n"
                    f"(technical reason: {reason or 'completed'})"),
            severity="success",
            actions=[
                f"Open the pull request and review the changes:\n{url}",
                "Nothing is merged automatically — merge the PR yourself when you are happy with it.",
            ],
        )
    if "delivered to branch" in reason:
        # real format: "approved; delivered to branch {branch} in {src}"
        branch = run.get("integration_branch") or ""
        src = run.get("repo") or ""
        rest = reason.split("delivered to branch", 1)[1]
        parts = rest.split(" in ", 1)
        if not branch and parts:
            branch = parts[0].strip()
        if not src and len(parts) > 1:
            src = parts[1].strip()
        base = run.get("base_branch") or "main"
        branch = branch or "the integration branch"
        return Explanation(
            headline="Done — the work is on a new local branch",
            detail=(f"The run finished and delivered its work to the local branch '{branch}' "
                    f"in {src or 'the repository clone'}. Nothing was pushed or merged.\n"
                    f"(technical reason: {reason})"),
            severity="success",
            actions=[
                f"Review the changes:\ngit diff {base}...{branch}",
                f"Review the files first:\ngit show {branch}",
                f"Merge into your branch:\ngit merge {branch}",
            ],
        )
    if "PR not created" in reason:
        return Explanation(
            headline="Done — but no pull request could be pushed",
            detail=(f"The run finished and its work is on a local branch, but SwarmForge "
                    f"could not push it to GitHub.\n(technical reason: {reason})"),
            severity="warning",
            actions=[
                ("Add a GitHub token (set GITHUB_TOKEN in the .env file, or the token env "
                 "configured as cfg.github.token_env) and restart the dashboard."),
                "Use the 'Push branch and open draft PR' button on this page to publish the work.",
            ],
        )
    return Explanation(
        headline="Done — the run finished successfully",
        detail=f"The run reached the COMPLETED state.\n(technical reason: {reason or 'completed'})",
        severity="success",
        actions=[
            "Review the changes on the integration branch shown below.",
            f"Budget used: ${budget:.2f}.",
        ],
    )


def _explain_budget_exceeded(run: dict) -> Explanation:
    budget = run.get("budget_usd") or 0
    reason = str(run.get("state_reason") or "")
    return Explanation(
        headline="Stopped — the budget was used up",
        detail=(f"The run stopped because it reached its spend limit of ${budget:.2f}. "
                f"Nothing is lost — the work done so far is saved.\n(technical reason: {reason})"),
        severity="error",
        actions=[
            "Start a new run with a larger budget (raise the budget on the New task form).",
            "Or run again with the same settings and a higher budget to continue from where it stopped.",
        ],
    )


def _explain_blocked(run: dict) -> Explanation:
    reason = str(run.get("state_reason") or "")
    low = reason.lower()

    def exp(headline: str, detail: str, actions: list[str], severity: str = "warning") -> Explanation:
        return Explanation(headline=headline,
                            detail=f"{detail}\n(technical reason: {reason or 'blocked'})",
                            severity=severity, actions=actions)

    if "docker is not installed" in low:
        return exp(
            "Blocked — Docker is not installed",
            "SwarmForge runs the developers inside a Docker sandbox, but Docker was not found on this machine.",
            ["Install Docker Desktop: https://www.docker.com/products/docker-desktop/.",
             "Or set sandbox.mode: local in swarmforge.yaml to run unsandboxed."])
    if "docker is not responding" in low:
        return exp(
            "Blocked — Docker is not running",
            "Docker is installed but its engine did not answer, so no sandboxed work could start.",
            ["Start Docker Desktop and wait until it says 'Engine running'.",
             "If it is already running, quit it, run `wsl --shutdown`, and start it again."])
    if "api key for provider" in low:
        # real format: "The API key for provider 'name' is missing: add KEY=... to the .env ..."
        key = ""
        if "add " in reason:
            key = reason.split("add ", 1)[1].split("=", 1)[0].strip()
        return exp(
            "Blocked — an API key is missing",
            "One of the AI providers used by this run has no API key configured, so no model could be called.",
            [f"Add {key or 'the missing API key'} to the .env file next to swarmforge.yaml."
             if key else "Add the missing API key to the .env file next to swarmforge.yaml.",
             "Restart the dashboard and start the run again."])
    if "is not built yet" in low:
        # real format: "The sandbox image 'img' is not built yet. Run `swarmforge sandbox build` once."
        cmd = "swarmforge sandbox build"
        if "swarmforge sandbox build --dev" in reason:
            cmd = "swarmforge sandbox build --dev"
        return exp(
            "Blocked — the sandbox image is not built yet",
            "The Docker image the developers run inside has not been built on this machine.",
            [f"Build it once from a terminal:\n{cmd}",
             "Then start the run again."])
    if "has no commits" in low or "is empty" in low:
        return exp(
            "Blocked — the repository is empty",
            "The repository has no commits yet, so there is no base to branch from or open a pull request against.",
            [("Make an initial commit in the repository:\ngit init -b main\ngit add -A\n"
              'git commit -m "initial commit"'),
             "On GitHub you can also open the repository page and click 'Add a README'.",
             "Then start the run again."])
    if "cannot access repository" in low:
        return exp(
            "Blocked — the repository could not be reached",
            "SwarmForge could not access the repository. This is usually a spelling mistake, "
            "or a private repository without credentials.",
            ["Check the owner/repository spelling in the task's repository field.",
             "For private repositories, add GITHUB_TOKEN to the .env file and restart the dashboard.",
             "Then start the run again."])
    if "is not a git repository" in low:
        return exp(
            "Blocked — the folder is not a git repository",
            "The given path is a folder but not a git repository, so there is no history to work from.",
            ["Turn it into one:\ngit init -b main\ngit add -A\ngit commit -m \"initial commit\"",
             "Then start the run again."])
    if "could not clone" in low:
        return exp(
            "Blocked — the repository could not be cloned",
            "Cloning the repository failed, for example because the URL or branch is wrong, "
            "or there is no network connection.",
            ["Check the repository URL and branch.",
             "Check your network connection.",
             "Then start the run again."])
    if "waiting for the repository" in low:
        return exp(
            "Waiting — another run is using the repository",
            "Another run currently holds the lock on this repository, so this run waits for its turn.",
            ["Wait for the other run to finish; this one will continue automatically.",
             "Or cancel the other run to free the repository."],
            severity="info")
    return exp(
        "Blocked — the run could not start",
        "Something in the environment or the task setup prevented the run from starting.",
        ["Fix the issue described above, then start the run again.",
         "Check the Console tab for the full technical details."])


def _explain_failed(run: dict) -> Explanation:
    reason = str(run.get("state_reason") or "")
    if "repair limit reached" in reason:
        # real format: "repair limit reached (N cycle(s)); M unresolved problem(s): summary; ..."
        problems: list[str] = []
        if "unresolved problem(s): " in reason:
            tail = reason.split("unresolved problem(s): ", 1)[1]
            problems = [p.strip() for p in tail.split("; ") if p.strip()]
        bullets = "\n".join(f"• {p}" for p in problems) or "no details were recorded"
        return Explanation(
            headline="Finished with unresolved problems",
            detail=(f"The run wrote code and tried to fix the review findings, but ran out of "
                    f"repair cycles before everything was resolved. Unresolved problems:\n{bullets}\n"
                    f"(technical reason: {reason})"),
            severity="error",
            actions=[
                "Open the Review and Tests tabs to see the full findings.",
                ("Fix the listed problems yourself, or run again with more repair cycles "
                 "(raise limits.max_repair_cycles)."),
            ],
        )
    if "no code changes" in reason:
        return Explanation(
            headline="Failed — no code changes were produced",
            detail=(f"The developers finished but did not change any files, so there is nothing "
                    f"to review or merge.\n(technical reason: {reason})"),
            severity="error",
            actions=[
                "Check the Console tab to see what the developers did.",
                "Rewrite the task more concretely and run again.",
            ],
        )
    if "maximum runtime" in reason:
        return Explanation(
            headline="Stopped — the time limit was hit",
            detail=(f"The run exceeded its maximum runtime and was stopped. The work done so far "
                    f"is saved.\n(technical reason: {reason})"),
            severity="error",
            actions=[
                "Raise the time limit (limits.max_runtime_s) and run again.",
                "Or continue this run's work from a new task.",
            ],
        )
    return Explanation(
        headline="The run failed unexpectedly",
        detail=f"An unexpected error stopped the run.\n(technical reason: {reason or 'unknown error'})",
        severity="error",
        actions=[
            "Check the Console tab for the full error and stack trace.",
            "Fix the cause if you can, then run again.",
        ],
    )


def _explain_cancelled(run: dict) -> Explanation:
    reason = str(run.get("state_reason") or "")
    return Explanation(
        headline="The run was cancelled",
        detail=(f"The run was stopped before it finished. The work done so far is saved.\n"
                f"(technical reason: {reason or 'cancelled'})"),
        severity="info",
        actions=[
            "Use 'Run again with the same settings' to retry the task.",
            "Or start a new task with different instructions.",
        ],
    )


def explain_run(run: dict, events: list[dict]) -> Explanation:
    """Explain what a run's outcome means and what to do next.

    Pure function: no Streamlit, no database access.
    """
    state = str(run.get("state") or "")
    if state == "COMPLETED":
        return _explain_completed(run)
    if state == "BUDGET_EXCEEDED":
        return _explain_budget_exceeded(run)
    if state == "BLOCKED":
        return _explain_blocked(run)
    if state == "FAILED":
        return _explain_failed(run)
    if state == "CANCELLED":
        return _explain_cancelled(run)
    # Anything else (including all non-terminal RunState values) is a live run.
    return _explain_running(run, events)
