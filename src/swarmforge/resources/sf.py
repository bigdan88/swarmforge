# SwarmForge developer tools for agents running in the Pi harness: `sf <tool> ...` from bash.
# Each command is executed by SwarmForge itself (same tools built-in agents get) through the model
# relay, authenticated with this agent's one-off token. Standard library only.
"""sf - SwarmForge developer tools

  sf start NAME [--port P] [--text T] [--wait S] -- COMMAND...   start an app/server in the background
  sf logs NAME [--tail N]                       is it running? latest output
  sf stop NAME                                  stop it
  sf browse URL [ACTIONS...] [--screenshot] [--max-chars N] [--timeout S]
        open a local page in headless Chromium: visible text, errors on the page, JS/console errors,
        failed requests, clickable elements. ACTIONS run in order before the report:
          --click SEL  --fill SEL=VALUE  --press KEY  --select SEL=VALUE  --check SEL
          --wait-for SEL  --wait-ms N  --goto URL
        SEL is a Playwright selector: text=Save, role=button[name="Save"], #id, .class
  sf tests [PATHS...] [-k EXPR] [--lf]          pytest with a structured summary of failures
  sf check [PATHS...] [--no-types]              ruff + mypy (default: the Python files you changed)
  sf fmt [PATHS...]                             ruff format (default: the Python files you changed)
  sf outline PATH                               classes/functions with signatures and line numbers
  sf symbol NAME                                where NAME is defined and every reference to it
  sf changes [PATH]                             your diff against the run's base commit
  sf history PATH [--max N] [--lines A-B]       git log (and blame for a line range) of a file
  sf http METHOD URL [--json JSON] [-H 'Name: value']...   call a local API
  sf packages [MODULE]                          installed packages / where a module comes from
  sf skills                                     list available skills/playbooks
  sf skill NAME                                 print a knowledge-base playbook (reusable lesson)
  sf call TOOL '{"json": "args"}'               any tool with raw JSON arguments

Example for a UI change:
  sf start web --port 8501 -- streamlit run app.py --server.headless true --server.port 8501
  sf browse http://localhost:8501 --click 'text=Settings' --screenshot
  sf stop web
"""
import json
import os
import sys
import urllib.error
import urllib.request

ACTIONS = {"--click": "click", "--fill": "fill", "--press": "press", "--select": "select", "--check": "check",
           "--wait-for": "wait_for", "--wait-ms": "wait_ms", "--goto": "goto"}


class Usage(Exception):
    pass


def _take(argv, i, flag):
    if i + 1 >= len(argv):
        raise Usage(f"{flag} needs a value")
    return argv[i + 1]


def _split(v, flag):
    if "=" not in v:
        raise Usage(f"{flag} expects SELECTOR=VALUE")
    sel, _, val = v.partition("=")
    return sel, val


def parse(argv):
    """argv (without the program name) -> (tool, args)."""
    if not argv or argv[0] in ("-h", "--help", "help"):
        raise Usage("")
    cmd, rest = argv[0], argv[1:]
    a = {}
    if cmd == "call":
        if not rest:
            raise Usage("sf call TOOL [JSON]")
        try:
            args = json.loads(rest[1]) if len(rest) > 1 else {}
        except json.JSONDecodeError as e:
            raise Usage(f"arguments are not valid JSON: {e}")
        return rest[0], args
    if cmd == "start":
        if "--" not in rest:
            raise Usage("sf start NAME [--port P] [--text T] [--wait S] -- COMMAND...")
        k = rest.index("--")
        opts, command = rest[:k], rest[k + 1:]
        if not opts or not command:
            raise Usage("sf start NAME [--port P] [--text T] [--wait S] -- COMMAND...")
        a["name"], i = opts[0], 1
        while i < len(opts):
            f = opts[i]
            if f == "--port":
                a["ready_port"] = int(_take(opts, i, f))
            elif f == "--text":
                a["ready_text"] = _take(opts, i, f)
            elif f == "--wait":
                a["wait_s"] = int(_take(opts, i, f))
            else:
                raise Usage(f"unknown option {f}")
            i += 2
        a["command"] = " ".join(_q(c) for c in command)
        return "start_process", a
    if cmd == "logs":
        if not rest:
            raise Usage("sf logs NAME [--tail N]")
        a["name"] = rest[0]
        if "--tail" in rest:
            a["tail_chars"] = int(_take(rest, rest.index("--tail"), "--tail"))
        return "read_process_output", a
    if cmd == "stop":
        if not rest:
            raise Usage("sf stop NAME")
        return "stop_process", {"name": rest[0]}
    if cmd == "browse":
        if not rest or rest[0].startswith("-"):
            raise Usage("sf browse URL [ACTIONS...] [--screenshot]")
        a = {"url": rest[0], "actions": []}
        i = 1
        while i < len(rest):
            f = rest[i]
            if f == "--screenshot":
                a["screenshot"] = True
                i += 1
                continue
            v = _take(rest, i, f)
            if f in ("--fill", "--select"):
                sel, val = _split(v, f)
                a["actions"].append({ACTIONS[f]: sel, "value": val})
            elif f == "--wait-ms":
                a["actions"].append({"wait_ms": int(v)})
            elif f in ACTIONS:
                a["actions"].append({ACTIONS[f]: v})
            elif f == "--max-chars":
                a["max_chars"] = int(v)
            elif f == "--timeout":
                a["timeout_s"] = int(v)
            else:
                raise Usage(f"unknown option {f}")
            i += 2
        return "browse", a
    if cmd == "tests":
        paths, i = [], 0
        while i < len(rest):
            f = rest[i]
            if f == "-k":
                a["keyword"] = _take(rest, i, f)
                i += 2
            elif f == "--lf":
                a["last_failed"] = True
                i += 1
            else:
                paths.append(f)
                i += 1
        if paths:
            a["paths"] = paths
        return "run_tests", a
    if cmd == "check":
        paths = [p for p in rest if p != "--no-types"]
        if "--no-types" in rest:
            a["type_check"] = False
        if paths:
            a["paths"] = paths
        return "check_code", a
    if cmd == "fmt":
        return "format_code", ({"paths": rest} if rest else {})
    if cmd == "outline":
        if len(rest) != 1:
            raise Usage("sf outline PATH")
        return "outline", {"path": rest[0]}
    if cmd == "symbol":
        if len(rest) != 1:
            raise Usage("sf symbol NAME")
        return "find_symbol", {"name": rest[0]}
    if cmd == "changes":
        return "git_changes", ({"path": rest[0]} if rest else {})
    if cmd == "history":
        if not rest:
            raise Usage("sf history PATH [--max N] [--lines A-B]")
        a["path"] = rest[0]
        if "--max" in rest:
            a["max_commits"] = int(_take(rest, rest.index("--max"), "--max"))
        if "--lines" in rest:
            lo, _, hi = _take(rest, rest.index("--lines"), "--lines").partition("-")
            a["line_start"], a["line_end"] = int(lo), int(hi or lo)
        return "git_history", a
    if cmd == "http":
        if len(rest) < 2:
            raise Usage("sf http METHOD URL [--json JSON] [-H 'Name: value']...")
        a = {"method": rest[0].upper(), "url": rest[1]}
        i = 2
        while i < len(rest):
            f, v = rest[i], _take(rest, i, rest[i])
            if f == "--json":
                try:
                    a["json"] = json.loads(v)
                except json.JSONDecodeError as e:
                    raise Usage(f"--json is not valid JSON: {e}")
            elif f == "-H":
                k, _, val = v.partition(":")
                a.setdefault("headers", {})[k.strip()] = val.strip()
            else:
                raise Usage(f"unknown option {f}")
            i += 2
        return "http_request", a
    if cmd == "packages":
        return "list_packages", ({"name": rest[0]} if rest else {})
    if cmd == "skills":
        return "list_skills", {}
    if cmd == "skill":
        if len(rest) != 1:
            raise Usage("sf skill NAME")
        return "load_skill", {"name": rest[0]}
    raise Usage(f"unknown command '{cmd}'")


def _q(s):
    if s and all(c.isalnum() or c in "-_./:=,@%+" for c in s):
        return s
    return "'" + s.replace("'", "'\"'\"'") + "'"


def call(tool, args, url=None, token=None, timeout=None):
    url = (url or os.environ.get("SF_TOOLS_URL") or "").rstrip("/")
    token = token or os.environ.get("SF_RELAY_TOKEN") or ""
    if not url or not token:
        return False, "ERROR: sf only works inside a SwarmForge agent session (SF_TOOLS_URL/SF_RELAY_TOKEN not set)."
    req = urllib.request.Request(url + "/sf/tool", data=json.dumps({"tool": tool, "args": args}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout or 900) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return False, f"ERROR: SwarmForge answered HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:500]}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"ERROR: could not reach SwarmForge: {e}"
    return bool(data.get("ok")), str(data.get("output", ""))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        tool, args = parse(argv)
    except (Usage, ValueError) as e:
        msg = str(e)
        print((f"sf: {msg}\n\n" if msg else "") + __doc__, file=sys.stderr if msg else sys.stdout)
        return 2 if msg else 0
    ok, out = call(tool, args)
    print(out)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
