"""GitHub adapter. Constructing it performs no I/O; callers enforce launch approval."""
from __future__ import annotations

import base64
from urllib.parse import quote

from ..github import GitHubClient
from .spec import ProjectError, fixed


class Repository:
    def __init__(self, cfg, repo):
        self.gh = GitHubClient(cfg.github)
        self.repo = repo
        self.root = "/repos/" + repo

    def close(self):
        self.gh._client.close()

    def request(self, method, path, **kw):
        r = self.gh._client.request(method, path, **kw)
        if r.status_code >= 400:
            raise ProjectError(f"GitHub {method} {path}: HTTP {r.status_code}")
        return r.json()

    def ensure(self, kind, pid):
        r = self.gh._client.get(self.root)
        if r.status_code == 404 and kind == "new":
            owner, name = self.repo.split("/")
            me = self.request("GET", "/user")["login"]
            endpoint = "/user/repos" if me.lower() == owner.lower() else f"/orgs/{owner}/repos"
            info = self.request("POST", endpoint, json={"name": name, "private": True, "auto_init": True, "description": "SwarmForge project " + pid})
        elif r.status_code >= 400:
            raise ProjectError(f"Repository unavailable: HTTP {r.status_code}")
        else:
            info = r.json()
            # Retry after a crash is safe, but do not adopt an unrelated existing repository.
            if kind == "new" and info.get("description") != "SwarmForge project " + pid:
                raise ProjectError("The requested new repository already exists; choose existing or a different name")

        head = self.root + "/commits/" + quote(info["default_branch"], safe="")
        response = self.gh._client.get(head)
        if response.status_code == 409 and response.json().get("message") == "Git Repository is empty.":
            # Omitting the branch lets GitHub create the default branch on the first commit.
            self.request("PUT", self.root + "/contents/README.md", json={
                "message": "Initialize project repository",
                "content": base64.b64encode(f"# {self.repo.split('/')[-1]}\n".encode("utf-8")).decode("ascii"),
            })
        elif response.status_code >= 400:
            raise ProjectError(f"GitHub GET {head}: HTTP {response.status_code}")

    def snapshot(self):
        info = self.request("GET", self.root)
        branch = info["default_branch"]
        commit = self.request("GET", self.root + "/commits/" + quote(branch, safe=""))
        sha = commit["sha"]
        tree = self.request("GET", self.root + "/git/trees/" + sha, params={"recursive": "1"})
        paths = [x["path"] for x in tree.get("tree", []) if x["type"] == "blob"]
        key_names = {"README.md", "pyproject.toml", "package.json", "AGENTS.md", "Makefile", "swarmforge.project.yml"}
        keys = {}
        selected = [p for p in paths if p in key_names]
        selected += [p for p in paths if p.startswith(("src/", "app/")) and p.endswith((".py", ".ts", ".tsx", ".go", ".rs"))][:4]
        for path in selected[:10]:
            keys[path] = self.read(path, sha)[0][:10000]
        return {"sha": sha, "branch": branch, "tree": paths[:1500], "key_files": keys, "truncated": tree.get("truncated", False)}

    def read(self, path, ref=None):
        r = self.gh._client.get(self.root + "/contents/" + quote(path, safe="/"), params={"ref": ref} if ref else {})
        if r.status_code == 404:
            return "", None
        if r.status_code != 200:
            raise ProjectError(f"Cannot read {path}: HTTP {r.status_code}")
        d = r.json()
        if d.get("encoding") != "base64":
            raise ProjectError(f"Cannot read {path}: unsupported content encoding")
        return base64.b64decode(d["content"]).decode("utf-8"), d["sha"]

    def write_spec(self, p, text, snapshot):
        old, sha = self.read(p["spec_path"], snapshot["branch"])
        permitted = {fixed(p["spec"])}
        if p.get("previous_spec"):
            permitted.add(fixed(p["previous_spec"]))
        if old and fixed(old) not in permitted:
            raise ProjectError("Repository fixed spec differs from the approved spec; reconcile it before continuing")
        if old == text:
            return
        now = self.request("GET", self.root + "/commits/" + quote(snapshot["branch"], safe=""))["sha"]
        if now != snapshot["sha"]:
            raise ProjectError("Main changed while planning; retry from the latest code")
        body = {"message": "docs: update project specification", "content": base64.b64encode(text.encode()).decode(), "branch": snapshot["branch"]}
        if sha:
            body["sha"] = sha
        self.request("PUT", self.root + "/contents/" + p["spec_path"], json=body)

    def pull(self, url):
        n = int(url.rstrip("/").split("/")[-1])
        d = self.request("GET", self.root + f"/pulls/{n}")
        if d["base"]["repo"]["full_name"].lower() != self.repo.lower():
            raise ProjectError("PR belongs to a different repository")
        return d

    def change(self, url):
        n = int(url.rstrip("/").split("/")[-1])
        files = self.request("GET", self.root + f"/pulls/{n}/files", params={"per_page": 50})
        return [{"path": f["filename"], "patch": f.get("patch", "Binary or omitted by GitHub")[:3000]} for f in files][:30]
