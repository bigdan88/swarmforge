"""Command-line interface: `swarmforge <command>`."""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .config import ConfigError, load_config
from .db import Store

DEV_IMAGE = "swarmforge-dev-sandbox:latest"
TERMINAL = {"COMPLETED", "FAILED", "BLOCKED", "CANCELLED", "BUDGET_EXCEEDED"}


def _store(cfg) -> Store:
    return Store(cfg.db_path)


def _ask(question: str) -> bool:
    if not sys.stdin.isatty():
        print(f"{question} -> no interactive terminal; declining. Re-run with --yes to pre-approve.")
        return False
    ans = input(f"\n>>> {question} [y/N] ").strip().lower()
    return ans in ("y", "yes")


# -------------------------------------------------------------------- run
def cmd_run(args, cfg) -> int:
    from .approvals import dashboard_approver, prompt_approver
    from .orchestrator import Orchestrator
    store = _store(cfg)
    if args.max_cycles is not None:
        cfg.limits.max_repair_cycles = args.max_cycles
    if args.budget is not None:
        cfg.limits.budget_usd = args.budget
    if args.max_runtime_hours is not None:
        cfg.limits.max_runtime_s = int(args.max_runtime_hours * 3600)
    mode = "yes" if args.yes else args.approval
    holder = {}

    def make(c, seed=None, heal_context=None, heal_of=None, attempt=None):
        o = Orchestrator(c, store, mode="single" if args.single else "swarm", single_role=args.role,
                         test_command=args.test_command, sandbox_image=args.sandbox_image,
                         seed_run_id=seed, heal_of=heal_of, heal_attempt=attempt, heal_context=heal_context)
        holder["orch"] = o
        if mode == "yes":
            o.approve = lambda q: True  # noqa: E731
        elif mode == "dashboard":
            o.approve = dashboard_approver(store, lambda: holder["orch"].run_id, c.approval.timeout_s)
        else:
            o.approve = prompt_approver
        return o
    if args.yes:
        print("note: --yes pre-approves pushing the branch and opening a draft PR (recorded as an intervention)")
    issue_url = args.issue if args.issue and args.issue.startswith("http") else None
    issue_file = args.issue_file or (args.issue if args.issue and not args.issue.startswith("http") else None)

    async def go():
        from .postmortem import heal
        rid = await make(cfg, seed=args.continue_from).execute(
            issue_url=issue_url, issue_file=issue_file, repo=args.repo, base_branch=args.base, run_id=args.run_id)

        async def launch(c, seed, ctx, heal_of, attempt):
            return await make(c, seed, ctx, heal_of, attempt).execute(
                issue_url=issue_url, issue_file=issue_file, repo=args.repo, base_branch=args.base)
        return await heal(cfg, store, rid, launch, attempts=args.heal_attempts, echo=print)
    try:
        run_id = asyncio.run(go())
    except KeyboardInterrupt:
        print("interrupted")
        return 130
    run = store.get_run(run_id)
    print(f"\nRun {run_id}: {run['state']} - {run['state_reason'] or ''}")
    print(f"Report: {Path(run['run_dir']) / 'report.md'}")
    if run["pr_url"]:
        print(f"Draft PR: {run['pr_url']}")
    return 0 if run["state"] == "COMPLETED" else 1


# ------------------------------------------------------------- inspection
def _fmt_age(t):
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else "-"


def cmd_runs(args, cfg) -> int:
    store = _store(cfg)
    rows = store.list_runs(args.limit)
    if args.json:
        result = []
        for r in rows:
            c = store.cost_summary(r["id"])
            result.append({
                "id": r["id"],
                "mode": r["mode"],
                "state": r["state"],
                "state_reason": r.get("state_reason"),
                "issue_title": r.get("issue_title"),
                "issue_url": r.get("issue_url"),
                "repo": r.get("repo"),
                "created_at": r["created_at"],
                "finished_at": r.get("finished_at"),
                "cost_usd": c["total"],
                "total_tokens": c["total_tokens"],
            })
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    if not rows:
        print("no runs yet")
        return 0
    print(f"{'RUN ID':24} {'MODE':6} {'STATE':22} {'COST':>9}  {'STARTED':16}  ISSUE")
    for r in rows:
        c = store.cost_summary(r["id"])
        print(f"{r['id']:24} {r['mode']:6} {r['state']:22} ${c['total']:8.4f}  {_fmt_age(r['created_at']):16}  "
              f"{(r['issue_title'] or r['issue_url'] or '')[:60]}")
    return 0


def cmd_failures(args, cfg) -> int:
    from .postmortem import CATEGORIES
    store = _store(cfg)
    rows = store.failures(args.limit)
    if args.json:
        json.dump(rows, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 0
    if not rows:
        print("no failures recorded")
        return 0
    for f in rows:
        print(f"{_fmt_age(f['created_at'])}  {f['run_id']}  {CATEGORIES.get(f['category'], f['category'])}")
        print(f"  task:    {(f['title'] or '')[:90]}")
        print(f"  why:     {(f['root_cause'] or '')[:300]}")
        print(f"  fix:     {(f['fix'] or '')[:300]}")
        if f["outcome"]:
            print(f"  outcome: {f['outcome']}")
        print()
    return 0


def cmd_diagnose(args, cfg) -> int:
    from .postmortem import CATEGORIES, diagnose
    store = _store(cfg)
    run = store.get_run(args.run_id)
    if not run:
        print(f"no run {args.run_id}")
        return 1
    d = asyncio.run(diagnose(cfg, store, run["id"]))
    if not d:
        print("nothing to diagnose (the run completed, was cancelled, or is still running)")
        return 0
    print(f"{CATEGORIES.get(d.category, d.category)} (by {d.diagnosed_by})\n\nWhy: {d.root_cause}\n\nFix: {d.fix}")
    return 0


def cmd_status(args, cfg) -> int:
    store = _store(cfg)
    run = store.get_run(args.run_id)
    if not run:
        if args.json:
            print(json.dumps({"error": f"run not found: {args.run_id}"}))
            return 2
        print(f"run not found: {args.run_id}")
        return 2
    c = store.cost_summary(run["id"])
    if args.json:
        tasks = store.tasks(run["id"])
        for t in tasks:
            t["depends_on"] = t.get("depends_on") or []
        out = {
            "id": run["id"],
            "mode": run["mode"],
            "state": run["state"],
            "state_reason": run["state_reason"],
            "issue_title": run["issue_title"],
            "issue_url": run["issue_url"],
            "repo": run["repo"],
            "created_at": run["created_at"],
            "finished_at": run["finished_at"],
            "cost_usd": c["total"],
            "total_tokens": c["total_tokens"],
            "integration_branch": run["integration_branch"],
            "pr_url": run["pr_url"],
            "repair_cycle": run["repair_cycle"],
            "interventions": len(store.interventions(run["id"])),
            "tasks": [
                {
                    "task_key": t["task_key"],
                    "kind": t["kind"],
                    "cycle": t["cycle"],
                    "title": t["title"],
                    "role": t["role"],
                    "model": t["model"],
                    "state": t["state"],
                    "depends_on": t["depends_on"],
                }
                for t in tasks
            ],
        }
        print(json.dumps(out))
        return 0
    elapsed = (run["finished_at"] or time.time()) - run["created_at"]
    print(f"Run        {run['id']}  ({run['mode']})")
    print(f"Issue      {run['issue_title'] or '-'}  [{run['issue_url'] or '-'}]")
    print(f"State      {run['state']}" + (f" - {run['state_reason']}" if run["state_reason"] else ""))
    print(f"Elapsed    {int(elapsed // 60)}m{int(elapsed % 60):02d}s   repair cycle {run['repair_cycle']}")
    print(f"Cost       ${c['total']:.4f} (provider-reported ${c['measured']:.4f}, estimated ${c['estimated']:.4f}) "
          f"of ${run['budget_usd']:.2f};  tokens {c['total_tokens']:,}")
    print(f"Branch     {run['integration_branch'] or '-'}")
    print(f"PR         {run['pr_url'] or '-'}")
    print(f"Interventions {len(store.interventions(run['id']))}")
    print("\nTasks:")
    for t in store.tasks(run["id"]):
        print(f"  {t['task_key']:14} {t['state']:11} {t['role'] or '':20} {(t['model'] or '')[:36]:36} {(t['title'] or '')[:50]}")
    tr = store.test_runs(run["id"])
    if tr:
        t = tr[-1]
        print(f"\nLast tests [{t['label']}]: exit {t['exit_code']}, {t['passed']} passed, {t['failed']} failed, {t['errors']} errors")
    return 0


def _print_event(e):
    who = f"[{e['task_key']}/{e['role']}] " if e["role"] else (f"[{e['task_key']}] " if e["task_key"] else "")
    print(f"{datetime.fromtimestamp(e['ts']):%H:%M:%S} {e['level'].upper():5} {e['kind']:9} {who}{e['message']}")


def cmd_logs(args, cfg) -> int:
    store = _store(cfg)
    run = store.get_run(args.run_id)
    if not run:
        print(f"run not found: {args.run_id}")
        return 2
    evs = store.events(run["id"])
    if args.tail:
        evs = evs[-args.tail:]
    last = 0
    for e in evs:
        _print_event(e)
        last = e["id"]
    if args.follow:
        try:
            while True:
                time.sleep(1.5)
                for e in store.events(run["id"], after_id=last):
                    _print_event(e)
                    last = e["id"]
                if store.get_run(run["id"])["state"] in TERMINAL and not store.events(run["id"], after_id=last):
                    break
        except KeyboardInterrupt:
            pass
    return 0


def cmd_cancel(args, cfg) -> int:
    store = _store(cfg)
    run = store.get_run(args.run_id)
    if not run:
        print(f"run not found: {args.run_id}")
        return 2
    if run["state"] in TERMINAL:
        print(f"run already finished ({run['state']})")
        return 0
    store.request_cancel(run["id"])
    store.event(run["id"], "cancel", "cancel requested from CLI", level="warn")
    print(f"cancel requested for {run['id']}; the orchestrator stops at the next agent step")
    return 0


def cmd_report(args, cfg) -> int:
    from .report import build_report
    store = _store(cfg)
    run = store.get_run(args.run_id)
    if not run:
        print(f"run not found: {args.run_id}")
        return 2
    text = build_report(store, run["id"])
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(text)
    return 0


def cmd_dashboard(args, cfg) -> int:
    app = Path(__file__).with_name("dashboard.py")
    if not shutil.which("streamlit") and not _has_module("streamlit"):
        print("streamlit is not installed: pip install 'swarmforge[dashboard]'")
        return 2
    theme = getattr(args, "theme", "auto")
    env = dict(os.environ, SWARMFORGE_DB=str(cfg.db_path), SWARMFORGE_CONFIG=str(cfg.source_path),
               SWARMFORGE_DASH_THEME=theme)
    url = f"http://localhost:{args.port}"
    address = "127.0.0.1"
    if args.lan:
        if not os.environ.get("SWARMFORGE_DASH_PASSWORD"):
            print("--lan makes the dashboard reachable from other devices, so it needs a password first.\n"
                  "Add a line like  SWARMFORGE_DASH_PASSWORD=pick-a-long-passphrase  to your .env, then retry.")
            return 2
        address = "0.0.0.0"
    print(f"SwarmForge dashboard: {url}  (Ctrl+C to stop; runs started from it keep running)")
    if args.lan:
        for ip in _lan_ips():
            print(f"  from your iPad / other devices: http://{ip}:{args.port}")
        print("  (allow Python through the Windows firewall for private networks if the page doesn't load)")
    if not args.no_browser:
        import threading
        import webbrowser
        threading.Timer(2.5, lambda: webbrowser.open(url)).start()
    return subprocess.call([sys.executable, "-m", "streamlit", "run", str(app), "--server.port", str(args.port),
                            "--server.address", address, "--server.headless", "true", "--browser.gatherUsageStats", "false",
                            "--client.toolbarMode", "minimal", "--theme.base", theme], env=env)


def _lan_ips() -> list[str]:
    import socket
    ips = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("10.255.255.255", 1))  # no packet is sent; picks the outgoing interface
            ips.add(sk.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127.")) or ["<this-computer's-IP>"]


def _has_module(m: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(m) is not None


# ------------------------------------------------------------------ check
def cmd_check(args, cfg) -> int:
    from .checks import run_checks
    return asyncio.run(run_checks(cfg, live=args.live, roles=args.roles))


def cmd_sandbox(args, cfg) -> int:
    from .sandbox import DockerSandbox
    sb = DockerSandbox(cfg.sandbox)
    if args.action == "build":
        if args.dev:
            if not sb.image_exists():
                print(f"building base image {cfg.sandbox.image} first ...", flush=True)
                if sb.build_image().returncode != 0:
                    print("base image build failed")
                    return 1
            print(f"building {DEV_IMAGE} (adds git, httpx, PyYAML, pytest-asyncio, streamlit, pandas "
                  "so SwarmForge can test itself) ...", flush=True)
            p = sb.build_image(dockerfile="Dockerfile.dev-sandbox", tag=DEV_IMAGE,
                               build_args={"BASE": cfg.sandbox.image})
            print("dev sandbox image built" if p.returncode == 0 else f"build failed (exit {p.returncode})")
            return p.returncode
        print(f"building {cfg.sandbox.image} from {cfg.sandbox.base_image} "
              "(first build downloads the base image; usually 1-3 minutes) ...", flush=True)
        p = sb.build_image()
        print("sandbox image built" if p.returncode == 0 else
              f"build failed (exit {p.returncode}); see docs/TROUBLESHOOTING.md")
        return p.returncode
    return 0


def cmd_publish(args, cfg) -> int:
    from .publish import publish_run
    store = _store(cfg)
    approve = (lambda q: True) if args.yes else _ask
    return asyncio.run(publish_run(cfg, store, args.run_id, approve))


def cmd_bench(args, cfg) -> int:
    from .benchmark import run_benchmark
    store = _store(cfg)
    if args.max_cycles is not None:
        cfg.limits.max_repair_cycles = args.max_cycles
    return asyncio.run(run_benchmark(cfg, store, issue=args.issue, repo=args.repo,
                                     acceptance=args.acceptance, modes=args.modes,
                                     single_role=args.role, label=args.label,
                                     include_runs=args.include_run))


# ------------------------------------------------------------------ queue
def _hours(h):
    return int(h * 3600) if h else None


def cmd_queue(args, cfg) -> int:
    from . import runqueue as rq
    from .repos import RepoError, normalize_repo
    store = _store(cfg)

    def norm(repo: str) -> str:
        try:
            return normalize_repo(repo).source
        except RepoError as e:
            raise SystemExit(f"error: {e}")

    a = args.qcmd
    if a == "list":
        items = store.queue_items()
        if not items:
            print("queue is empty")
        for i in items:
            print(f"#{i['id']:<4} {i['status']:9} {(i['title'] or '')[:60]:60} {i['repo'][:40]:40} "
                  f"{i['run_id'] or ''}")
        pid = rq.worker_pid(store)
        print(f"\nworker: {'running (pid %d)' % pid if pid else 'stopped'}; "
              f"paused={store.kv_get(rq.PAUSED_KEY) == '1'}; on failure: {store.kv_get(rq.POLICY_KEY) or 'stop'}")
        return 0
    if a == "add":
        body = Path(args.file).read_text(encoding="utf-8") if args.file else (args.body or "")
        if not body.strip():
            print("error: give --file or --body")
            return 2
        iid = store.queue_add(args.title, body, norm(args.repo), mode=args.mode, budget=args.budget,
                              max_cycles=args.max_cycles, chain=0 if args.no_chain else 1,
                              approval=args.approval, max_runtime_s=_hours(args.max_runtime_hours))
        print(f"queued #{iid}")
        return 0
    if a == "import":
        items = rq.parse_backlog(Path(args.file).read_text(encoding="utf-8"))
        for it in items:
            iid = store.queue_add(it.title, it.body, norm(args.repo), mode=args.mode, budget=args.budget,
                                  max_cycles=args.max_cycles, chain=0 if args.no_chain else 1,
                                  approval=args.approval, max_runtime_s=_hours(args.max_runtime_hours))
            print(f"queued #{iid} {it.title}")
        print(f"{len(items)} item(s) imported")
        return 0
    if a == "remove":
        store.queue_remove(args.id)
        return 0
    if a == "clear":
        print(f"removed {store.queue_clear_finished()} finished item(s)")
        return 0
    if a == "pause":
        store.kv_set(rq.PAUSED_KEY, "1")
        print("queue will pause after the current run")
        return 0
    if a == "resume":
        store.kv_set(rq.PAUSED_KEY, "0")
        pid = rq.start_worker(cfg, store)
        print(f"queue worker running (pid {pid})")
        return 0
    if a == "policy":
        store.kv_set(rq.POLICY_KEY, args.policy)
        return 0
    if a == "work":
        store.kv_set(rq.PAUSED_KEY, "0")
        n = asyncio.run(rq.work(cfg, store))
        print(f"queue worker finished: {n} item(s) processed")
        return 0
    return 2


# ------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="swarmforge", description="Autonomous multi-agent software development")
    p.add_argument("--config", "-c", help="path to swarmforge.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run the swarm on a GitHub issue URL or a local issue file")
    r.add_argument("issue", nargs="?", help="GitHub issue URL (or path to an issue .md file)")
    r.add_argument("--issue-file", help="local markdown issue (first '# ' line is the title)")
    r.add_argument("--repo", help="repository to work on (URL or local path); defaults to the issue's repo")
    r.add_argument("--base", help="base branch (default: repository default branch)")
    r.add_argument("--yes", action="store_true", help="pre-approve pushing the branch / opening the draft PR")
    r.add_argument("--approval", choices=["prompt", "dashboard", "yes"], default="prompt",
                   help="how to ask before pushing: terminal prompt (default), dashboard button, or pre-approved")
    r.add_argument("--run-id", help=argparse.SUPPRESS)
    r.add_argument("--test-command", help="override the test command for this run")
    r.add_argument("--max-runtime-hours", type=float, help="override limits.max_runtime_s for this run")
    r.add_argument("--heal-attempts", type=int, metavar="N",
                   help="if the run fails, diagnose it and retry up to N times with more effort "
                        "(default self_heal.max_attempts; 0 disables)")
    r.add_argument("--continue-from", metavar="RUN",
                   help="start from a stopped run's result and its unresolved problems")
    r.add_argument("--sandbox-image", help="override the sandbox image for this run (must already be built)")
    r.add_argument("--single", action="store_true", help="single-agent baseline mode")
    r.add_argument("--role", help="developer role for --single (default: first developer role)")
    r.add_argument("--max-cycles", type=int, help="override limits.max_repair_cycles")
    r.add_argument("--budget", type=float, help="override limits.budget_usd")
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("runs", help="list recent runs")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--json", action="store_true", help="output JSON array of run objects")
    s.set_defaults(fn=cmd_runs)

    s = sub.add_parser("failures", help="list failed runs with their diagnosis")
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_failures)

    s = sub.add_parser("diagnose", help="(re)write the post-mortem of a failed run")
    s.add_argument("run_id")
    s.set_defaults(fn=cmd_diagnose)

    s = sub.add_parser("status", help="show run status")
    s.add_argument("run_id", nargs="?", default="latest")
    s.add_argument("--json", action="store_true", help="output machine-readable JSON")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("logs", help="show run event log")
    s.add_argument("run_id", nargs="?", default="latest")
    s.add_argument("--follow", "-f", action="store_true")
    s.add_argument("--tail", type=int)
    s.set_defaults(fn=cmd_logs)

    s = sub.add_parser("cancel", help="request cancellation of a running run")
    s.add_argument("run_id")
    s.set_defaults(fn=cmd_cancel)

    s = sub.add_parser("report", help="print or save the execution report (aka summary)", aliases=["summary"])
    s.add_argument("run_id", nargs="?", default="latest")
    s.add_argument("--output", "-o")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("dashboard", help="launch the Streamlit dashboard")
    s.add_argument("--port", type=int, default=8501)
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--lan", action="store_true",
                   help="also serve to other devices (iPad, phone; works over Tailscale too); "
                        "requires SWARMFORGE_DASH_PASSWORD in .env")
    s.add_argument("--theme", choices=["auto", "light", "dark"], default="auto",
                   help="dashboard color theme; 'dark' = dark mode (you can also switch live from the sidebar)")
    s.set_defaults(fn=cmd_dashboard)

    s = sub.add_parser("check", help="validate config, tools and provider connectivity")
    s.add_argument("--live", action="store_true", help="also send a tiny tool-calling request to each role's model")
    s.add_argument("--roles", nargs="*", help="limit --live to these roles")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("sandbox", help="manage the Docker sandbox image")
    s.add_argument("action", choices=["build"])
    s.add_argument("--dev", action="store_true",
                   help="build swarmforge-dev-sandbox, used when SwarmForge works on its own code")
    s.set_defaults(fn=cmd_sandbox)

    s = sub.add_parser("publish", help="push an approved run's branch and open the draft PR")
    s.add_argument("run_id")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(fn=cmd_publish)

    s = sub.add_parser("bench", help="benchmark single agent vs swarm on the same issue")
    s.add_argument("issue", help="GitHub issue URL or local issue .md file")
    s.add_argument("--repo", required=True, help="repository (URL or local path) - same start state for all modes")
    s.add_argument("--acceptance", help="directory of hidden acceptance tests copied in only for evaluation")
    s.add_argument("--modes", nargs="*", default=["single", "swarm"], choices=["single", "swarm"])
    s.add_argument("--include-run", action="append", default=[],
                   help="add an already finished run of the same issue to the comparison")
    s.add_argument("--role", help="developer role for the single-agent mode")
    s.add_argument("--label", default="")
    s.add_argument("--max-cycles", type=int)
    s.set_defaults(fn=cmd_bench)
    q = sub.add_parser("queue", help="queue tasks and run them one after another")
    qs = q.add_subparsers(dest="qcmd", required=True)
    qs.add_parser("list", help="show the queue")
    for name, hlp in (("add", "add one task"), ("import", "add every task from a backlog Markdown file")):
        x = qs.add_parser(name, help=hlp)
        if name == "add":
            x.add_argument("title")
            x.add_argument("--file", help="Markdown file with the description")
            x.add_argument("--body", help="description text")
        else:
            x.add_argument("file", help="e.g. docs/SELF_IMPROVEMENT_BACKLOG.md")
        x.add_argument("--repo", required=True, help="local folder, owner/repo or GitHub URL")
        x.add_argument("--mode", choices=["swarm", "single"], default="swarm")
        x.add_argument("--budget", type=float, default=None)
        x.add_argument("--max-cycles", type=int, default=None)
        x.add_argument("--max-runtime-hours", type=float, default=None)
        x.add_argument("--no-chain", action="store_true", help="start from the default branch, not the previous result")
        x.add_argument("--approval", choices=["yes", "dashboard"], default="yes",
                       help="GitHub repos: open draft PRs without asking (yes) or ask in the dashboard")
    x = qs.add_parser("remove")
    x.add_argument("id", type=int)
    qs.add_parser("clear", help="remove finished items")
    qs.add_parser("pause", help="stop after the current run")
    qs.add_parser("resume", help="start the background worker")
    x = qs.add_parser("policy", help="what to do when an item fails")
    x.add_argument("policy", choices=["stop", "continue"])
    qs.add_parser("work", help="run the queue in this terminal (the dashboard starts it in the background)")
    q.set_defaults(fn=cmd_queue)

    w = sub.add_parser("watch", help="queue GitHub issues that get the watch label (see watch: in the config)")
    w.add_argument("--once", action="store_true", help="poll once and exit")
    w.add_argument("--interval", type=int, help="seconds between polls (default watch.interval_s)")
    w.add_argument("--no-worker", action="store_true", help="only queue; don't start the queue worker")
    w.set_defaults(fn=cmd_watch)

    sb = sub.add_parser("swebench", help="SWE-bench Lite: fetch tasks, run the swarm, evaluate with the official harness")
    sbs = sb.add_subparsers(dest="sb_cmd", required=True)
    x = sbs.add_parser("fetch", help="download task instances (problem statements only)")
    x.add_argument("--limit", type=int, default=10)
    x.add_argument("--offset", type=int, default=0)
    x = sbs.add_parser("run", help="run SwarmForge on the fetched instances and write predictions.jsonl")
    x.add_argument("--label", required=True, help="name of this attempt (folder under <workdir>/swebench)")
    x.add_argument("--ids", nargs="*", help="only these instance ids")
    x.add_argument("--limit", type=int, help="at most this many instances")
    x.add_argument("--single", action="store_true", help="single-agent baseline instead of the swarm")
    x.add_argument("--budget", type=float, help="budget per instance (USD)")
    x.add_argument("--test-command", help="test command inside the sandbox (default: none; the harness tests)")
    x = sbs.add_parser("eval", help="score predictions with the official harness and record the result")
    x.add_argument("--label", required=True)
    x.add_argument("--workers", type=int, default=2)
    x.add_argument("--variant", help="record the score on this variant (default: the variant the runs used)")
    sb.set_defaults(fn=cmd_swebench)

    va = sub.add_parser("variants", help="archive of agent variants; evolve them on a benchmark suite")
    vas = va.add_subparsers(dest="v_cmd", required=True)
    x = vas.add_parser("list", help="show the variant tree with scores")
    x.add_argument("--suite", help="show scores for this suite name only")
    x = vas.add_parser("show", help="show one variant's overlay, rationale and scores")
    x.add_argument("id")
    x = vas.add_parser("add", help="add a hand-written variant from a YAML/JSON overlay file")
    x.add_argument("file")
    x.add_argument("--parent", default="v0")
    x.add_argument("--why", default="hand-written")
    x = vas.add_parser("eval", help="score a variant on a suite")
    x.add_argument("id")
    x.add_argument("--suite", default="bench/suite.yaml")
    x.add_argument("--runs", type=int, default=1, help="runs per case (more = less noise, more cost)")
    x = vas.add_parser("evolve", help="propose, score and archive new variants (Darwin Gödel Machine style)")
    x.add_argument("--suite", default="bench/suite.yaml")
    x.add_argument("--generations", type=int, default=1)
    x.add_argument("--runs", type=int, default=1)
    x.add_argument("--seed", type=int)
    x = vas.add_parser("use", help="make a variant the default for every run (writes active_variant)")
    x.add_argument("id", help="variant id, or 'none' for the base config")
    va.set_defaults(fn=cmd_variants)
    return p


def cmd_watch(args, cfg) -> int:
    from .watch import watch
    try:
        return watch(cfg, _store(cfg), once=args.once, interval_s=args.interval, start_worker=not args.no_worker)
    except KeyboardInterrupt:
        return 0


def cmd_swebench(args, cfg) -> int:
    from . import swebench as sb
    store = _store(cfg)
    if args.sb_cmd == "fetch":
        path = sb.fetch(cfg, limit=args.limit, offset=args.offset)
        print(f"saved {sum(1 for _ in path.open(encoding='utf-8'))} instance(s) to {path}")
        return 0
    if args.sb_cmd == "run":
        asyncio.run(sb.run_instances(cfg, store, label=args.label, ids=args.ids, limit=args.limit,
                                     mode="single" if args.single else "swarm", budget=args.budget,
                                     test_command=args.test_command))
        return 0
    return sb.evaluate(cfg, store, label=args.label, workers=args.workers, variant=args.variant)


def cmd_variants(args, cfg) -> int:
    import yaml as _yaml
    from . import variants as V
    store = _store(cfg)
    V.ensure_root(store)
    if args.v_cmd == "list":
        print(V.render_archive(store.variants(), args.suite))
        if cfg.active_variant:
            print(f"\nactive variant: {cfg.active_variant}")
        return 0
    if args.v_cmd == "show":
        v = store.variant(args.id)
        if not v:
            print(f"no variant {args.id}")
            return 2
        print(f"{v['id']} (parent {v['parent'] or '-'}, {v['status']})\n{v['rationale'] or ''}\n")
        print(_yaml.safe_dump(v["overlay"] or {"(empty)": "base configuration"}, sort_keys=False))
        for s in v["scores"]:
            print(f"- {s['suite']}: score {s['score']:.2f}, ${s.get('cost_usd', 0):.2f}, runs {len(s.get('runs', []))}")
        return 0
    if args.v_cmd == "add":
        overlay = _yaml.safe_load(Path(args.file).read_text(encoding="utf-8")) or {}
        errs = V.validate_overlay(overlay, cfg)
        if errs:
            print("invalid overlay:\n- " + "\n- ".join(errs))
            return 2
        vid = V.next_id(store)
        store.variant_add(vid, overlay, args.parent, args.why)
        print(f"added {vid}")
        return 0
    if args.v_cmd == "eval":
        asyncio.run(V.evaluate(cfg, store, args.id, args.suite, runs_per_case=args.runs))
        return 0
    if args.v_cmd == "evolve":
        asyncio.run(V.evolve(cfg, store, args.suite, generations=args.generations, runs_per_case=args.runs,
                             seed=args.seed))
        return 0
    if args.v_cmd == "use":
        vid = None if args.id.lower() in ("none", "base") else args.id
        if vid and not store.variant(vid):
            print(f"no variant {vid}")
            return 2
        if cfg.source_path is None:
            print(f"set `active_variant: {vid}` in your swarmforge.yaml")
            return 0
        V.set_active(cfg.source_path, vid)
        print(f"{cfg.source_path}: active_variant = {vid or '(none: base configuration)'}. "
              "Restart the dashboard and queue worker to apply it.")
        return 0
    return 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    cfg.workdir.mkdir(parents=True, exist_ok=True)
    return args.fn(args, cfg) or 0


if __name__ == "__main__":
    sys.exit(main())
