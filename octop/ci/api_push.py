"""Push the local commit to GitHub through the Git Data API.

`git push` over HTTPS stalls in this sandbox; the REST API does not.
Only paths under `octop/` are touched; deletions are mirrored from the remote tree.
"""

import base64
import json
import subprocess
import sys
from pathlib import Path

REPO, BRANCH = "WJMA-GIT/ponytail", "main"
ROOT = Path("/data/.octop/agents/D3JK04/generated/ponytail-fork")


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout


def api(*args: str, payload: dict | None = None) -> dict:
    cmd = ["gh", "api", *args] + (["--input", "-"] if payload is not None else [])
    proc = subprocess.run(
        cmd, input=json.dumps(payload) if payload else None, capture_output=True, text=True
    )
    if proc.returncode != 0:
        print("API FAILED:", proc.stderr[:500])
        sys.exit(1)
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


remote_sha = api(f"repos/{REPO}/git/ref/heads/{BRANCH}")["object"]["sha"]
remote_tree = api(f"repos/{REPO}/git/commits/{remote_sha}")["tree"]["sha"]
local_sha = git("rev-parse", "HEAD").strip()
print("remote:", remote_sha[:7], "local:", local_sha[:7])

entries = []
for line in git("ls-tree", "-r", local_sha).splitlines():
    meta, path = line.split("\t", 1)
    mode, _type, _sha = meta.split()
    if not path.startswith("octop/"):
        continue
    blob = api(
        f"repos/{REPO}/git/blobs",
        payload={
            "content": base64.b64encode((ROOT / path).read_bytes()).decode(),
            "encoding": "base64",
        },
    )["sha"]
    entries.append({"path": path, "mode": mode, "type": "blob", "sha": blob})
print("uploaded:", len(entries))

head_paths = {entry["path"] for entry in entries}
remote_tree_api = api(f"repos/{REPO}/git/trees/{remote_tree}?recursive=1")
# 只镜像 blob：目录条目（type=tree）不是文件，按名字"删除"会把整棵子树带走。
remote_paths = [
    entry["path"]
    for entry in remote_tree_api.get("tree", [])
    if entry.get("type") == "blob" and entry["path"].startswith("octop/")
]
gone = [path for path in remote_paths if path not in head_paths]
for path in gone:
    entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
print("deletions:", len(gone))

tree = api(f"repos/{REPO}/git/trees", payload={"base_tree": remote_tree, "tree": entries})
author = {
    "name": "WJMA-GIT",
    "email": "35687330+WJMA-GIT@users.noreply.github.com",
    "date": git("log", "-1", "--format=%cI", local_sha).strip(),
}
commit = api(
    f"repos/{REPO}/git/commits",
    payload={
        "message": git("log", "-1", "--format=%B", local_sha),
        "tree": tree["sha"],
        "parents": [remote_sha],
        "author": author,
        "committer": author,
    },
)
api(f"repos/{REPO}/git/refs/heads/{BRANCH}", "-X", "PATCH", payload={"sha": commit["sha"]})
print(
    "pushed:",
    commit["sha"][:7],
    "tree:",
    tree["sha"][:7],
    "| local tree:",
    git("rev-parse", "HEAD^{tree}").strip()[:7],
)
