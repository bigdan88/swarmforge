"""Standalone trusted application probe, copied into the existing sandbox by the host.

No imports from the generated application's test suite. Commands run normally;
observations read real files, SQLite, HTTP responses and browser behavior.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class Blocked(RuntimeError):
    pass


class EmptyOutput(AssertionError):
    pass


def local_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.username or parsed.password:
        raise AssertionError("Only local HTTP observations are allowed")
    return url


class LocalRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return super().redirect_request(request, fp, code, message, headers, local_url(newurl))


def resolve(root, name):
    path = (root / name).resolve()
    path.relative_to(root.resolve())
    if not path.is_file():
        raise AssertionError(f"Expected output file does not exist: {name}")
    return path


def preflight(check, root):
    wanted = check.get("preflight", {})
    found = {"platform": platform.system(), "python": sys.version, "executables": {}, "modules": {}, "fixtures": {}}
    if wanted.get("platform") and wanted["platform"] != platform.system():
        raise Blocked(f"Requires {wanted['platform']}; runner is {platform.system()}")
    for binary in wanted.get("executables", []):
        path = shutil.which(binary)
        if not path:
            raise Blocked(f"Required executable unavailable: {binary}")
        found["executables"][binary] = path
    for module in wanted.get("modules", []):
        try:
            spec = importlib.util.find_spec(module)
        except (ImportError, ValueError, AttributeError):
            spec = None
        if spec is None:
            raise Blocked(f"Required Python module unavailable: {module}")
        found["modules"][module] = spec.origin
    for name in wanted.get("files", []):
        try:
            file = resolve(root, name)
        except (AssertionError, ValueError) as exc:
            raise Blocked(f"Required local model or fixture unavailable: {name}") from exc
        if not file.stat().st_size:
            raise Blocked(f"Required local file is empty: {name}")
    for name in check.get("fixtures", []):
        file = resolve(root, name)
        digest = hashlib.sha256()
        with file.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        found["fixtures"][name] = digest.hexdigest()
    if wanted.get("cuda"):
        try:
            import torch
            if not torch.cuda.is_available():
                raise Blocked("CUDA is unavailable in the selected verification sandbox")
            found["cuda"] = {"device": torch.cuda.get_device_name(0), "torch": torch.__version__}
        except ImportError as exc:
            raise Blocked("PyTorch is unavailable for the requested CUDA check") from exc
    return found


def stop(process):
    """Only terminate process trees started by this check, including background servers."""
    if os.name == "nt":
        if process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def observe(step, root, artifacts, remaining):
    kind = step["kind"]
    if kind == "file":
        file = resolve(root, step["path"])
        size = file.stat().st_size
        if size < step.get("min_bytes", 1):
            raise EmptyOutput(f"{step['path']}: {size} bytes; expected at least {step.get('min_bytes', 1)}")
        if "contains" in step:
            assert step["contains"].casefold() in file.read_text(encoding="utf-8").casefold(), f"{step['path']}: expected text absent"
        return {"path": step["path"], "bytes": size}
    if kind == "json":
        value = json.loads(resolve(root, step["path"]).read_text(encoding="utf-8"))
        for part in step.get("pointer", "").strip("/").split("/") if step.get("pointer", "") else []:
            part = part.replace("~1", "/").replace("~0", "~")
            value = value[int(part)] if isinstance(value, list) else value[part]
        if "equals" in step:
            assert value == step["equals"], f"{step['path']}: expected {step['equals']!r}, observed {value!r}"
        if "min_count" in step and len(value) < step["min_count"]:
            raise EmptyOutput(f"{step['path']}: expected at least {step['min_count']} results; observed {len(value)}")
        return {"path": step["path"], "observed": value}
    if kind == "sqlite":
        file = resolve(root, step["path"])
        connection = sqlite3.connect(file.as_uri() + "?mode=ro", uri=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.set_progress_handler(lambda: int(remaining() <= 0), 1000)
            rows = connection.execute(step["query"]).fetchmany(10000)
        finally:
            connection.close()
        if len(rows) < step.get("min_rows", 1):
            raise EmptyOutput(f"{step['path']}: query returned {len(rows)} rows; expected at least {step.get('min_rows', 1)}")
        if "contains" in step:
            assert step["contains"].casefold() in json.dumps(rows).casefold(), f"{step['path']}: expected query content absent"
        return {"path": step["path"], "rows": len(rows), "sample": rows[:3]}
    if kind == "http":
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), LocalRedirect())
        while True:
            try:
                response = opener.open(local_url(step["url"]), timeout=max(.1, min(2, remaining())))
                break
            except urllib.error.HTTPError as exc:
                response = exc
                break
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if remaining() <= .1:
                    raise AssertionError(f"Application did not become reachable: {step['url']}") from None
                time.sleep(min(.1, remaining()))
        with response:
            status, headers = response.status, dict(response.headers.items())
            body = response.read(1024 * 1024).decode("utf-8", errors="replace")
        assert status == step.get("status", 200), f"Expected HTTP {step.get('status', 200)}, observed {status}"
        if "contains" in step:
            assert step["contains"] in body, "Expected HTTP response text absent"
        lower = {k.lower(): v for k, v in headers.items()}
        for name, expected in step.get("headers", {}).items():
            assert expected.lower() in lower.get(name.lower(), "").lower(), f"Unexpected {name}: {lower.get(name.lower())!r}"
        return {"url": step["url"], "status": status, "headers": headers}
    if kind == "browser":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise Blocked("Playwright is unavailable in the verification sandbox") from exc
        observe({"kind": "http", "url": step["url"]}, root, artifacts, remaining)
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            except Exception as exc:
                raise Blocked(f"Chromium is unavailable: {exc}") from exc
            try:
                page = browser.new_page(accept_downloads=False)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                def route(request_route):
                    try:
                        local_url(request_route.request.url)
                    except AssertionError:
                        request_route.abort()
                    else:
                        request_route.continue_()
                page.route("**/*", route)
                page.set_default_timeout(max(1, min(15000, int(remaining() * 1000))))
                page.goto(local_url(step["url"]), wait_until="domcontentloaded")
                for action in step.get("actions", []):
                    locator = page.locator(action["selector"])
                    if action["kind"] == "click":
                        locator.click()
                    elif action["kind"] == "fill":
                        locator.fill(str(action.get("value", "")))
                    else:
                        locator.press(str(action.get("value", "Enter")))
                if "contains" in step:
                    page.get_by_text(step["contains"], exact=False).first.wait_for()
                if "media_time_s" in step:
                    target = step["media_time_s"]
                    page.wait_for_function("target => { const m=document.querySelector('video,audio'); return m && m.readyState>=2 && Math.abs(m.currentTime-target)<3; }", arg=target)
                    initial = page.locator("video,audio").first.evaluate("m => { m.muted=true; m.play(); return m.currentTime; }")
                    page.wait_for_function("initial => { const m=document.querySelector('video,audio'); return m && !m.paused && m.currentTime>initial+.1; }", arg=initial)
                assert not errors, "Browser errors: " + "; ".join(errors)
                screenshot = artifacts / "browser.png"
                page.screenshot(path=str(screenshot), full_page=True)
                return {"url": page.url, "screenshot": str(screenshot)}
            finally:
                browser.close()
    raise AssertionError(f"Unsupported observation: {kind}")


def execute(request, root, artifacts):
    check = request["check"]
    result = {"commit_sha": request["commit_sha"], "contract_hash": request["contract_hash"],
              "status": "failed", "reason": "Check did not finish", "steps": [], "environment": {}}
    processes, logs = [], []
    deadline = time.monotonic() + check["timeout_s"]
    remaining = lambda: max(0, deadline - time.monotonic())
    try:
        result["environment"] = preflight(check, root)
        for index, step in enumerate(check["steps"]):
            if remaining() <= 0:
                raise TimeoutError("Application check exceeded its time allowance")
            if step["kind"] == "command":
                argv = [x.replace("{workspace}", str(root)).replace("{artifacts}", str(artifacts)) for x in step["argv"]]
                log = (artifacts / f"command-{index}.log").open("w", encoding="utf-8")
                logs.append(log)
                process = subprocess.Popen(argv, cwd=root, stdout=log, stderr=subprocess.STDOUT,
                                           start_new_session=os.name != "nt")
                processes.append(process)
                observation = {"argv": argv, "log": str(artifacts / f"command-{index}.log")}
                if not step.get("background"):
                    code = process.wait(timeout=remaining())
                    observation["exit_code"] = code
                    assert code == step.get("expected_exit", 0), f"Application command exited {code}; inspect command-{index}.log"
            else:
                observation = observe(step, root, artifacts, remaining)
            result["steps"].append({"index": index, "kind": step["kind"], "status": "passed", "observation": observation})
        result.update(status="passed", reason="Application execution and all output expectations passed")
    except Blocked as exc:
        result.update(status="blocked", reason=str(exc))
    except EmptyOutput as exc:
        result.update(status="empty", reason=str(exc))
    except Exception as exc:
        result.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
    finally:
        if result["status"] != "passed":
            index = len(result["steps"])
            if index < len(check["steps"]):
                result["failed_step"] = {"index": index, "step": check["steps"][index]}
        for process in processes:
            try:
                stop(process)
            except Exception as exc:
                result.update(status="failed", reason=f"Could not stop verification process: {exc}")
        for log in logs:
            log.close()
    return result


def main():
    request_path, result_path = map(Path, sys.argv[1:3])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    result = execute(request, Path.cwd().resolve(), result_path.parent.resolve())
    result_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"status": result["status"], "reason": result["reason"]}), flush=True)
    return 0 if result["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
