"""GitHub REST calls with stdlib only (no requests dependency)."""
import base64
import json
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"


class Unprocessable(Exception):
    """GitHub answered 422, usually because a newer commit moved the lines."""


def _call(method, path, token, body=None):
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "User-Agent": "reviewbot"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 422:
            raise Unprocessable(e.read().decode()) from e
        raise


def get_pr(repo, number, token):
    return _call("GET", f"/repos/{repo}/pulls/{number}", token)


def get_files(repo, number, token):
    files = []
    for page in range(1, 11):  # ponytail: first 1000 files only; GitHub allows 3000
        batch = _call("GET", f"/repos/{repo}/pulls/{number}/files?per_page=100&page={page}", token)
        files += batch
        if len(batch) < 100:
            break
    return files


def post_review(repo, number, token, sha, body, comments):
    """event=COMMENT always: the bot can never approve or request changes."""
    return _call("POST", f"/repos/{repo}/pulls/{number}/reviews", token,
                 {"commit_id": sha, "event": "COMMENT", "body": body, "comments": comments})


def get_file(repo, path, ref, token, max_bytes=60_000):
    """Text of one file at a commit, or None (missing, binary, or bigger than max_bytes)."""
    try:
        d = _call("GET", f"/repos/{repo}/contents/{urllib.parse.quote(path)}?ref={ref}", token)
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return None
        raise
    if not isinstance(d, dict) or d.get("encoding") != "base64" or d.get("size", 0) > max_bytes:
        return None
    try:
        return base64.b64decode(d["content"]).decode("utf-8")
    except UnicodeDecodeError:
        return None


def get_tree(repo, ref, token):
    """Every file in the repo at a ref: [{path, size}]. Also says if GitHub cut the list short."""
    d = _call("GET", f"/repos/{repo}/git/trees/{urllib.parse.quote(ref)}?recursive=1", token)
    return [{"path": t["path"], "size": t.get("size", 0)} for t in d["tree"] if t["type"] == "blob"], d.get("truncated", False)


def get_repo(repo, token):
    return _call("GET", f"/repos/{repo}", token)


def get_commit_patches(repo, token, n=25):
    """Added lines of the last n commits: [(sha, {path: added_text})]. Secrets deleted later still live in history."""
    out = []
    for c in _call("GET", f"/repos/{repo}/commits?per_page={n}", token)[:n]:
        d = _call("GET", f"/repos/{repo}/commits/{c['sha']}", token)
        added = {}
        for f in d.get("files", []):
            lines = [l[1:] for l in (f.get("patch") or "").splitlines() if l.startswith("+") and not l.startswith("+++")]
            if lines:
                added[f["filename"]] = "\n".join(lines)
        out.append((c["sha"], added))
    return out
