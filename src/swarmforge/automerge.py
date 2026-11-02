"""Auto-merge: merge the swarm's pull requests without waiting for a human, when it is safe.

Decided right after a run opens its PR (`decide`), then applied through the GitHub API (`apply`):

* never: the run did not complete, the final tests did not pass, an unresolved [critical]/[major]
  finding is listed (code review, tester or screenshots), the PR conflicts or a check fails, or the
  change touches a file the repository lists under `protected_paths` in swarmforge.project.yml
  (for SwarmForge: its core, where one bad merge stops every task);
* straight away: small changes (`auto_merge.max_lines` added + deleted lines);
* larger ones: a model (`auto_merge.judge_role`) reads the task, the diff and the evidence and says
  merge or hold; anything above `judge_max_lines` always waits for you.

Every decision is recorded (interventions `auto_merge` / `auto_merge_hold`) and shown on the Pull
requests page, where a merge can be reverted with one click. SwarmForge never restarts itself after
an auto-merge: the new code arrives with the next Update & restart."""
from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass

MERGED, HELD = "auto_merge", "auto_merge_hold"
FIXING, CLOSED, MANUAL = "auto_merge_fixing", "auto_merge_closed", "auto_merge_manual"


@dataclass
class Decision:
    merge: bool
    reason: str
    lines: int = 0
    judged: bool = False
    risk: str | None = None


def repo_enabled(cfg, repo: str) -> bool:
    am = cfg.auto_merge
    return bool(am.enabled) and (not am.repos or repo.lower() in {r.lower() for r in am.repos})


def protected_hits(files: list[str], patterns: list[str]) -> list[str]:
    return sorted({f for f in files for p in patterns if fnmatch.fnmatch(f, p)})


def serious(findings: list[dict]) -> list[dict]:
    return [f for f in findings if f["severity"] in ("critical", "major")]


def rule_decision(run: dict, tests_ok: bool, open_findings: list[dict], files: list[str], lines: int,
                  protected: list[str], max_lines: int, judge_max_lines: int, *, protected_mode: str = "hold",
                  majors_mode: str = "hold") -> Decision | None:
    """The fixed rules. None = allowed, but the judge decides (see judge_bar for how strict)."""
    if run.get("state") != "COMPLETED":
        return Decision(False, f"the run did not complete ({run.get('state')})", lines)
    if not tests_ok:
        return Decision(False, "the final test run did not pass", lines)
    if lines > judge_max_lines:
        return Decision(False, f"very large change ({lines} lines > {judge_max_lines}); review it yourself", lines)
    bad = serious(open_findings)
    if bad and majors_mode == "hold":
        return Decision(False, f"{len(bad)} unresolved [major] finding(s), e.g. "
                               f"'{bad[0]['title']}' — check them in the PR", lines)
    hits = protected_hits(files, protected)
    if hits and protected_mode == "hold":
        return Decision(False, "touches protected file(s) " + ", ".join(hits[:5])
                        + (" …" if len(hits) > 5 else "") + " (protected_paths)", lines)
    if not bad and not hits and lines <= max_lines:
        return Decision(True, f"small change ({lines} lines), tests passed, no open findings", lines)
    return None


def judge_bar(files: list[str], protected: list[str]) -> str:
    """Highest risk the judge may report and still merge: core files need "low"."""
    return "low" if protected_hits(files, protected) else "medium"


def judge_messages(system: str, title: str, body: str, files: list[str], lines: int, diff: str,
                   findings: list[dict], tests: str, protected: list[str] | None = None) -> list[dict]:
    listed = "\n".join(f"- [{f['severity']}] {f['title']} ({f.get('file') or ''}): "
                       f"{(f.get('description') or '')[:300]}" for f in findings[:20])
    hits = protected_hits(files, protected or [])
    user = "\n\n".join([
        f"Task: {title}\n\n{(body or '')[:3000]}",
        f"Change: {lines} lines added+deleted in {len(files)} file(s):\n" + "\n".join(files[:80]),
        ("CORE FILES touched (a mistake here can stop the whole system; merge only if the risk is LOW): "
         + ", ".join(hits)) if hits else "No core files touched.",
        f"Final test run (executed by SwarmForge): {tests}",
        "Open review findings (not fixed):\n" + (listed or "(none)"),
        f"<diff>\n{diff[:60000]}\n</diff>" + ("\n[diff truncated]" if len(diff) > 60000 else ""),
    ])
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_judgement(text: str, max_risk: str = "medium") -> tuple[bool, str, str]:
    """(merge, risk, reason). Anything unclear means hold; risk above max_risk means hold."""
    m = re.search(r"\{.*\}", text or "", flags=re.S)
    if not m:
        return False, "high", "the judge did not answer with a decision"
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return False, "high", "the judge's answer was not valid JSON"
    order = {"low": 0, "medium": 1, "high": 2}
    risk = str(d.get("risk") or "high").lower()
    risk = risk if risk in order else "high"
    merge = str(d.get("decision") or "").lower() == "merge" and order[risk] <= order.get(max_risk, 1)
    return merge, risk, str(d.get("reason") or "")[:600]


# ------------------------------------------------------------------ fix-up tasks for held PRs
FIX_MARK = re.compile(r"<!-- sf-autofix depth=(\d+) supersedes=(\S+) -->")


def fix_depth(body: str | None) -> tuple[int, str | None]:
    """(how many fix-up rounds deep this task is, the PR it replaces)."""
    m = FIX_MARK.search(body or "")
    return (int(m.group(1)), m.group(2)) if m else (0, None)


def fix_task(*, title: str, body: str, url: str, reason: str, findings: list[dict]) -> tuple[str, str]:
    """(title, body) of the fix-up task for a held PR: continue its code, address why it was held."""
    depth, _ = fix_depth(body)
    base_body = FIX_MARK.sub("", body or "").split("\n\n## Fix-up round")[0].rstrip()
    listed = "\n".join(f"- [{f['severity']}] {f['title']} ({f.get('file') or ''}): "
                       f"{(f.get('description') or '')[:400]}"
                       + (f" Fix: {f['recommendation'][:300]}" if f.get("recommendation") else "")
                       for f in findings[:20])
    new_body = (f"{base_body}\n\n## Fix-up round {depth + 1}: why {url} was not merged\n{reason}\n\n"
                + (f"Open findings to address:\n{listed}\n\n" if listed else "")
                + "You continue from that pull request's code. Change only what is needed to address the points "
                  "above; no drive-by reformatting or unrelated rewrites, and keep the change as small as possible "
                  "(split nothing out, remove nothing that works). Its new pull request replaces the old one.\n"
                + f"<!-- sf-autofix depth={depth + 1} supersedes={url} -->")
    return "Fix up: " + re.sub(r"^(Fix up: )+", "", title or ""), new_body


def start_fix(cfg, run_id: str, *, title: str, body: str, repo: str, url: str, reason: str,
              findings: list[dict]) -> str:
    """Start the fix-up run right now (alongside whatever the queue is doing). Returns its run id.
    Raises ValueError if it can't be started here (e.g. no config path); callers then queue it."""
    from .launcher import start_run
    t, b = fix_task(title=title, body=body, url=url, reason=reason, findings=findings)
    rid, _ = start_run(cfg, repo=repo, issue_title=t, issue_body=b, seed_run_id=run_id, approval="yes")
    return rid


def queue_fix(store, run_id: str, *, title: str, body: str, repo: str, url: str, reason: str,
              findings: list[dict], queue: str | None = None) -> int:
    """Fallback: queue the fix-up task at the front of its queue (same content as start_fix)."""
    from . import runqueue as rq
    t, b = fix_task(title=title, body=body, url=url, reason=reason, findings=findings)
    qid = store.queue_add(t, b, repo, seed_run_id=run_id, chain=0, approval="yes", queue=queue or "main")
    mine = rq.lane(store.queue_item(qid))
    waiting = [q for q in store.queue_items() if q["status"] == "queued" and q["id"] != qid and rq.lane(q) == mine]
    if waiting:
        store.queue_update(qid, position=min(q["position"] for q in waiting) - 0.5)
    return qid


def close_pr(gh, repo: str, number: int, comment: str) -> None:
    from .github import GitHubError
    gh._client.post(f"/repos/{repo}/issues/{number}/comments", json={"body": comment})
    r = gh._client.patch(f"/repos/{repo}/pulls/{number}", json={"state": "closed"})
    if r.status_code >= 400:
        raise GitHubError(f"#{number}: could not close it: HTTP {r.status_code} {r.text[:200]}")


def pr_files(gh, repo: str, number: int) -> list[str]:
    out, page = [], 1
    while page <= 30:
        r = gh._client.get(f"/repos/{repo}/pulls/{number}/files", params={"per_page": 100, "page": page})
        if r.status_code >= 400:
            break
        batch = r.json()
        out += [f["filename"] for f in batch]
        if len(batch) < 100:
            break
        page += 1
    return out


def get_pr(gh, repo: str, number: int):
    from .prs import PR, refresh
    from .github import GitHubError
    r = gh._client.get(f"/repos/{repo}/pulls/{number}")
    if r.status_code >= 400:
        raise GitHubError(f"#{number}: HTTP {r.status_code} {r.text[:200]}")
    d = r.json()
    pr = PR(repo=repo, number=number, title=d["title"], url=d["html_url"], head=d["head"]["ref"],
            base=d["base"]["ref"], draft=bool(d.get("draft")), author=(d.get("user") or {}).get("login", ""),
            created_at=d.get("created_at", ""), node_id=d.get("node_id", ""), head_sha=d["head"]["sha"])
    return refresh(gh, pr, tries=5)


def apply(gh, pr, method: str = "squash", sleep=None) -> str:
    """Merge one PR (updating its branch first if main moved on). Raises GitHubError if GitHub refuses."""
    import time

    from .github import GitHubError
    from .prs import merge, refresh, update_branch
    sleep = sleep or time.sleep
    if pr.state == "dirty" or pr.mergeable is False:
        raise GitHubError("it conflicts with main now")
    if pr.state == "behind":
        update_branch(gh, pr)
        sleep(3)
        refresh(gh, pr, tries=5, sleep=sleep)
    if pr.state == "blocked":
        raise GitHubError("branch protection or required checks block it")
    return merge(gh, pr, method=method)


def revert(gh, repo: str, number: int, method: str = "squash") -> str:
    """Undo an auto-merge: GitHub creates a revert PR, which is merged right away. Returns its URL."""
    from .github import GitHubError
    pr = get_pr(gh, repo, number)
    base = gh.cfg.api_url.rstrip("/")
    url = (base[:-3] if base.endswith("/v3") else base) + "/graphql"
    q = ("mutation($id: ID!, $t: String!) { revertPullRequest(input: {pullRequestId: $id, title: $t, draft: false})"
         " { revertPullRequest { number url } } }")
    r = gh._client.post(url, json={"query": q, "variables": {"id": pr.node_id, "t": f"Revert \"{pr.title}\""}})
    data = r.json() if r.status_code < 400 else {}
    rev = ((data.get("data") or {}).get("revertPullRequest") or {}).get("revertPullRequest")
    if not rev:
        raise GitHubError(f"GitHub could not create the revert: {r.text[:300]}")
    rpr = get_pr(gh, repo, rev["number"])
    apply(gh, rpr, method)
    return rev["url"]
