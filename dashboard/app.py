import functools, hmac, json, os, re, time, urllib.error, urllib.request
import boto3
from boto3.dynamodb.conditions import Key
from flask import Flask, Response, abort, render_template, request

app = Flask(__name__)
table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])  # creds come from the instance role
ssm = boto3.client("ssm")
MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5")
REPO_RE = re.compile(r"^(?:https?://github\.com/)?([\w.-]+/[\w.-]+?)(?:\.git)?/?$")


@functools.cache
def _param(env_name):
    return ssm.get_parameter(Name=os.environ[env_name], WithDecryption=True)["Parameter"]["Value"]


@app.before_request
def login():
    """Everything except /health needs the dashboard password (HTTP Basic, any username).
    Unset locally, so `flask run` works without AWS."""
    if request.path == "/health" or "DASHBOARD_PASSWORD_PARAM" not in os.environ:
        return
    a = request.authorization
    if not (a and hmac.compare_digest((a.password or "").encode(), _param("DASHBOARD_PASSWORD_PARAM").encode())):
        return Response("login required", 401, {"WWW-Authenticate": 'Basic realm="ReviewBot"'})


def _items(limit=None):
    """REVIEW items, newest first. limit=None reads them all (volume is tiny)."""
    kw = dict(KeyConditionExpression=Key("pk").eq("REVIEW"), ScanIndexForward=False)
    if limit:
        kw["Limit"] = limit
    items = []
    while True:
        r = table.query(**kw)
        items += r["Items"]
        if limit or "LastEvaluatedKey" not in r:
            return items
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def _json(data, status=200):
    return Response(json.dumps(data, default=lambda d: int(d) if d == int(d) else float(d)),
                    status=status, mimetype="application/json")


def _err(msg, status=400):
    return _json({"error": msg}, status)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/reviews")
def reviews():
    return _json([{**i, "cost_usd": float(i["cost_micro"]) / 1e6, "time": i["sk"].split("#")[0]}
                  for i in _items(200)])


@app.get("/api/stats")
def stats():
    items = _items()
    n = len(items)
    cost = sum(float(i["cost_micro"]) for i in items) / 1e6
    return _json({
        "reviews": n,
        "issues": {s: sum(int(i[s]) for i in items) for s in ("high", "medium", "low")},
        "total_cost_usd": cost,
        "avg_cost_usd": cost / n if n else 0,
        "avg_duration_s": sum(float(i["duration_ms"]) for i in items) / n / 1000 if n else 0})


# ---------- feedback: thumbs on one issue; the reviewer reads these back into its prompt ----------
@app.post("/api/feedback")
def feedback():
    d = request.get_json(silent=True) or {}
    sk, idx, vote = d.get("sk"), d.get("idx"), d.get("vote")
    if vote not in ("up", "down", "") or not isinstance(idx, int) or not isinstance(sk, str):
        return _err("sk, idx and vote (up/down/'') required")
    item = table.get_item(Key={"pk": "REVIEW", "sk": sk}).get("Item")
    if not item or not 0 <= idx < len(item.get("issues", [])):
        return _err("no such issue", 404)
    issue, votes = item["issues"][idx], item.get("votes", {})
    fb = {"pk": f"FB#{item['repo']}", "sk": f"{sk}#{idx}"}
    if vote:
        votes[str(idx)] = vote
        table.put_item(Item={**fb, "vote": vote, "category": issue["category"], "severity": issue["severity"],
                             "message": issue["message"], "expires_at": int(time.time()) + 180 * 86400})
    else:
        votes.pop(str(idx), None)
        table.delete_item(Key=fb)
    table.update_item(Key={"pk": "REVIEW", "sk": sk}, UpdateExpression="SET votes = :v",
                      ExpressionAttributeValues={":v": votes})
    return _json({"votes": votes})


# ---------- repos: connecting one creates the GitHub webhook, so nobody opens GitHub settings ----------
def _token(account):
    """Each GitHub account's token lives in SSM; no account = the optional single token from put-secrets.sh."""
    if account:
        return ssm.get_parameter(Name=f"/reviewbot/gh/{account}", WithDecryption=True)["Parameter"]["Value"]
    return _param("GITHUB_TOKEN_PARAM")


def _gh(method, path, body=None, account=None, token=None):
    req = urllib.request.Request(
        "https://api.github.com" + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token or _token(account)}",
                 "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                 "User-Agent": "reviewbot"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def _settings(d):
    """Validated model + cost cap (micro-dollars) from a request body, or an error string."""
    model, cap = d.get("model", MODELS[0]), d.get("max_cost_usd", 0.5)
    if model not in MODELS:
        return "model must be one of " + ", ".join(MODELS)
    if not isinstance(cap, (int, float)) or not 0 < cap <= 10:
        return "max_cost_usd must be between 0 and 10"
    return {"model": model, "max_cost_micro": int(cap * 1e6)}


# ---------- GitHub accounts: paste a personal access token once; it is checked, then stored encrypted in SSM ----------
@app.get("/api/accounts")
def accounts():
    return _json(table.query(KeyConditionExpression=Key("pk").eq("GHACCT"))["Items"])  # logins only, never tokens


@app.post("/api/accounts")
def add_account():
    token = str((request.get_json(silent=True) or {}).get("token", "")).strip()
    if not 20 <= len(token) <= 255:
        return _err("paste a GitHub personal access token")
    try:
        login = _gh("GET", "/user", token=token)["login"]
    except urllib.error.HTTPError:
        return _err("GitHub rejected that token")
    if not re.fullmatch(r"[\w-]+", login):
        return _err("unexpected GitHub login")
    ssm.put_parameter(Name=f"/reviewbot/gh/{login}", Type="SecureString", Value=token, Overwrite=True)
    table.put_item(Item={"pk": "GHACCT", "sk": login, "login": login})
    return _json({"login": login}, 201)


@app.delete("/api/accounts")
def remove_account():
    login = (request.get_json(silent=True) or {}).get("login")
    if not isinstance(login, str) or "Item" not in table.get_item(Key={"pk": "GHACCT", "sk": login}):
        return _err("account not found", 404)
    used = [r["repo"] for r in table.query(KeyConditionExpression=Key("pk").eq("REPOS"))["Items"]
            if r.get("account") == login]
    if used:
        return _err("still used by " + ", ".join(used) + ": remove those repos first", 409)
    ssm.delete_parameter(Name=f"/reviewbot/gh/{login}")
    table.delete_item(Key={"pk": "GHACCT", "sk": login})
    return _json({"removed": login})


@app.get("/api/repos")
def repos():
    return _json(table.query(KeyConditionExpression=Key("pk").eq("REPOS"))["Items"])


@app.post("/api/repos")
def add_repo():
    d = request.get_json(silent=True) or {}
    m = REPO_RE.match(str(d.get("repo", "")).strip())
    s = _settings(d)
    if not m or isinstance(s, str):
        return _err("enter a repo like owner/name or its GitHub URL" if not m else s)
    account = d.get("account") or None
    if account and "Item" not in table.get_item(Key={"pk": "GHACCT", "sk": str(account)}):
        return _err("unknown GitHub account: add its token first")
    try:
        repo = _gh("GET", f"/repos/{m.group(1)}", account=account)["full_name"]  # canonical case; also proves the token can see it
        hook_url = os.environ["WEBHOOK_URL"]
        try:
            hook = _gh("POST", f"/repos/{repo}/hooks", {
                "name": "web", "active": True, "events": ["pull_request"],
                "config": {"url": hook_url, "content_type": "json", "secret": _param("WEBHOOK_SECRET_PARAM")}},
                account=account)
        except urllib.error.HTTPError as e:
            if e.code != 422:  # 422 = our hook already exists: reuse it
                raise
            hook = next(h for h in _gh("GET", f"/repos/{repo}/hooks", account=account) if h["config"].get("url") == hook_url)
    except urllib.error.HTTPError as e:
        return _err(f"GitHub answered {e.code}: the token needs Webhooks write access to {m.group(1)}", 502)
    except StopIteration:
        return _err("a different webhook blocks this repo; remove it in GitHub settings", 409)
    item = {"pk": "REPOS", "sk": repo, "repo": repo, "hook_id": hook["id"], **s, **({"account": account} if account else {})}
    table.put_item(Item=item)
    return _json(item, 201)


@app.put("/api/repos")
def update_repo():
    d = request.get_json(silent=True) or {}
    s = _settings(d)
    if isinstance(s, str):
        return _err(s)
    try:
        table.update_item(Key={"pk": "REPOS", "sk": d.get("repo")}, ConditionExpression="attribute_exists(pk)",
                          UpdateExpression="SET model = :m, max_cost_micro = :c",
                          ExpressionAttributeValues={":m": s["model"], ":c": s["max_cost_micro"]})
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return _err("repo not connected", 404)
    return _json(s)


@app.delete("/api/repos")
def remove_repo():
    repo = (request.get_json(silent=True) or {}).get("repo")
    item = table.get_item(Key={"pk": "REPOS", "sk": repo}).get("Item") if isinstance(repo, str) else None
    if not item:
        return _err("repo not connected", 404)
    try:
        _gh("DELETE", f"/repos/{repo}/hooks/{item['hook_id']}", account=item.get("account"))
    except urllib.error.HTTPError as e:
        if e.code != 404:  # already gone is fine
            return _err(f"GitHub answered {e.code} while removing the webhook", 502)
    table.delete_item(Key={"pk": "REPOS", "sk": repo})
    return _json({"removed": repo})


@app.get("/health")
def health():
    return "ok"
