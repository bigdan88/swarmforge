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


def rule_decision(run: dict, tests_ok: bool, open_findings: list[dict], files: list[str], lines: int,
                  protected: list[str], max_lines: int, judge_max_lines: int) -> Decision | None:
    """The fixed rules. None = allowed but large: ask the judge."""
    if run.get("state") != "COMPLETED":
        return Decision(False, f"the run did not complete ({run.get('state')})", lines)
    if not tests_ok:
        return Decision(False, "the final test run did not pass", lines)
    serious = [f for f in open_findings if f["severity"] in ("critical", "major")]
    if serious:
        return Decision(False, f"{len(serious)} unresolved [major] finding(s), e.g. "
                               f"'{serious[0]['title']}' — check them in the PR", lines)
    hits = protected_hits(files, protected)
    if hits:
        return Decision(False, "touches protected file(s) " + ", ".join(hits[:5])
                        + (" …" if len(hits) > 5 else "") + " (protected_paths)", lines)
    if lines <= max_lines:
        return Decision(True, f"small change ({lines} lines), tests passed, no open findings", lines)
    if lines > judge_max_lines:
        return Decision(False, f"very large change ({lines} lines > {judge_max_lines}); review it yourself", lines)
    return None


def judge_messages(system: str, title: str, body: str, files: list[str], lines: int, diff: str,
                   findings: list[dict], tests: str) -> list[dict]:
    minor = "\n".join(f"- [{f['severity']}] {f['title']} ({f.get('file') or ''})" for f in findings[:20])
    user = "\n\n".join([
        f"Task: {title}\n\n{(body or '')[:3000]}",
        f"Change: {lines} lines added+deleted in {len(files)} file(s):\n" + "\n".join(files[:80]),
        f"Final test run (executed by SwarmForge): {tests}",
        "Remaining minor findings:\n" + (minor or "(none)"),
        f"<diff>\n{diff[:60000]}\n</diff>" + ("\n[diff truncated]" if len(diff) > 60000 else ""),
    ])
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_judgement(text: str) -> tuple[bool, str, str]:
    """(merge, risk, reason). Anything unclear means hold."""
    m = re.search(r"\{.*\}", text or "", flags=re.S)
    if not m:
        return False, "high", "the judge did not answer with a decision"
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return False, "high", "the judge's answer was not valid JSON"
    risk = str(d.get("risk") or "high").lower()
    risk = risk if risk in ("low", "medium", "high") else "high"
    merge = str(d.get("decision") or "").lower() == "merge" and risk != "high"
    return merge, risk, str(d.get("reason") or "")[:600]


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
