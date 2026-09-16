import json
import os
from pathlib import Path
import urllib.request
import urllib.error

token = os.environ.get("GH_TOKEN", "")
if not token:
    raise SystemExit("REPO_PAT is not available to this workflow")

def api(method, path, data=None):
    request = urllib.request.Request(
        "https://api.github.com" + path,
        data=json.dumps(data).encode() if data is not None else None,
        method=method,
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            body = response.read()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read())
        raise SystemExit(f"GitHub {method} {path}: HTTP {exc.code}: {body.get('message', 'request rejected')}")

if api("GET", "/user")["login"].lower() != "reallocall":
    raise SystemExit("Token owner is not ReallocAll")
repo = api("POST", "/user/repos", {
    "name": "endstone-event-lab", "private": True, "auto_init": True,
    "description": "Independent real-BDS event regression runner pinned to Endstone PR #526",
    "has_issues": True, "has_wiki": False})
base = "/repos/ReallocAll/endstone-event-lab"
branch = repo["default_branch"]
ref = api("GET", base + "/git/ref/heads/" + branch)
parent = api("GET", base + "/git/commits/" + ref["object"]["sha"])
files = json.loads(Path(".bootstrap-endstone-event-lab/files.json").read_text())
tree = api("POST", base + "/git/trees", {"base_tree": parent["tree"]["sha"], "tree": files})
commit = api("POST", base + "/git/commits", {
    "message": "Add pinned PR526 live BDS event test workflow", "tree": tree["sha"],
    "parents": [parent["sha"]]})
api("PATCH", base + "/git/refs/heads/" + branch, {"sha": commit["sha"], "force": False})
if branch != "main":
    api("POST", base + "/git/refs", {"ref": "refs/heads/main", "sha": commit["sha"]})
    api("PATCH", base, {"default_branch": "main"})
result = {"repository": repo["html_url"], "private": True, "sha": commit["sha"], "branch": "main"}
print(json.dumps(result))
with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
    summary.write("# Repository created\n\n" + json.dumps(result, indent=2) + "\n")
