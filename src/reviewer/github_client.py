"""GitHub REST calls with stdlib only (no requests dependency)."""
import json
import urllib.error
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
