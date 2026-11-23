"""One pass through the whole app on a simulated AWS (moto): GitHub accounts, repos, reviews, feedback, cost cap."""
import importlib.util, io, json, os, pathlib, sys, types, urllib.error

os.environ.update(AWS_DEFAULT_REGION="ap-southeast-2", AWS_ACCESS_KEY_ID="x", AWS_SECRET_ACCESS_KEY="x",
                  TABLE_NAME="t", CLAUDE_MODEL="claude-haiku-5-5", ANALYTICS_BUCKET="analytics-bkt",
                  WEBHOOK_URL="https://hook/webhook", CLAUDE_KEY_PARAM="k", GITHUB_TOKEN_PARAM="/reviewbot/github-token")
import boto3
from boto3.dynamodb.conditions import Key
from moto import mock_aws

ROOT = pathlib.Path(__file__).parent.parent


def load(name, path):
    sys.path.insert(0, str(ROOT / pathlib.Path(path).parent))  # reviewer imports its siblings by name
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.path.pop(0)
    return mod


def http_error(code, body=b""):
    return urllib.error.HTTPError("https://api.github.com/repos/Me/Play/hooks", code, "x", {}, io.BytesIO(body))


@mock_aws
def test_end_to_end(monkeypatch):
    ddb, ssm = boto3.resource("dynamodb"), boto3.client("ssm")
    ddb.create_table(TableName="t", BillingMode="PAY_PER_REQUEST",
                     AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
                     KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}])
    boto3.client("s3").create_bucket(Bucket="analytics-bkt", CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"})
    ssm.put_parameter(Name="/reviewbot/github-token", Type="SecureString", Value="legacy")

    dash = load("dash_app", "dashboard/app.py")
    calls = []

    def fake_gh(method, path, body=None, account=None, token=None):
        calls.append((method, path, account))
        if path == "/user":
            if token and token.startswith("good"):
                return {"login": "alice"}
            raise http_error(401)
        if path.startswith("/user/repos"):
            return [{"full_name": "Me/Play", "permissions": {"admin": True}},
                    {"full_name": "Me/ReadOnly", "permissions": {"admin": False}},
                    {"full_name": "Me/Other", "permissions": {"admin": True}}]
        if method == "GET" and path.count("/") == 3:
            return {"full_name": "Me/Play"}
        return {"id": 42} if method == "POST" else None

    monkeypatch.setattr(dash, "_gh", fake_gh)
    monkeypatch.setattr(dash, "_param", lambda env: "secret")
    c = dash.app.test_client()

    # --- GitHub accounts: validated, stored encrypted in SSM, never returned
    assert c.post("/api/accounts", json={"token": "short"}).status_code == 400
    assert c.post("/api/accounts", json={"token": "bad" + "x" * 30}).status_code == 400
    r = c.post("/api/accounts", json={"token": "good" + "x" * 30})
    assert r.status_code == 201 and r.get_json() == {"login": "alice"}
    assert ssm.get_parameter(Name="/reviewbot/gh/alice", WithDecryption=True)["Parameter"]["Value"] == "good" + "x" * 30
    accounts = c.get("/api/accounts").get_json()
    assert [a["login"] for a in accounts] == ["alice"] and "good" not in json.dumps(accounts)
    assert c.post("/api/repos", json={"repo": "a/b", "account": "nobody"}).status_code == 400
    assert c.get("/api/accounts/alice/repos").get_json() == ["Me/Other", "Me/Play"]  # admin only, no read-only repos
    assert c.get("/api/accounts/nobody/repos").status_code == 404

    # --- repos: connecting creates the webhook with that account's token
    # a token without Webhooks permission: the dashboard must relay GitHub's reason
    real_gh = dash._gh
    def denied(method, path, body=None, account=None, token=None):
        if method == "POST" and path.endswith("/hooks"):
            raise http_error(403, b'{"message": "Resource not accessible by personal access token"}')
        return real_gh(method, path, body, account, token)
    monkeypatch.setattr(dash, "_gh", denied)
    r = c.post("/api/repos", json={"repo": "me/play", "account": "alice"})
    assert r.status_code == 502 and "403 on /repos/Me/Play/hooks" in r.get_json()["error"] and "not accessible" in r.get_json()["error"]
    monkeypatch.setattr(dash, "_gh", real_gh)
    ok = c.post("/api/repos", json={"repo": "https://github.com/me/play", "account": "alice",
                                    "model": "claude-sonnet-5-5", "max_cost_usd": 0.05})
    assert ok.status_code == 201 and ("POST", "/repos/Me/Play/hooks", "alice") in calls
    assert c.post("/api/repos", json={"repo": "nonsense"}).status_code == 400
    assert c.post("/api/repos", json={"repo": "a/b", "model": "gpt"}).status_code == 400
    assert c.get("/api/accounts/alice/repos").get_json() == ["Me/Other"]  # connected repos drop out of the list
    repo = c.get("/api/repos").get_json()[0]
    assert (repo["repo"], repo["max_cost_micro"], repo["account"]) == ("Me/Play", 50000, "alice")
    assert c.put("/api/repos", json={"repo": "Me/Play", "model": "claude-haiku-5-5", "max_cost_usd": 0.25}).status_code == 200
    assert c.put("/api/repos", json={"repo": "No/Pe", "model": "claude-haiku-5-5", "max_cost_usd": 0.25}).status_code == 404

    # --- reviewer: per-repo settings and token
    rev = load("rev_app", "src/reviewer/app.py")
    monkeypatch.setattr(rev, "_param", lambda env: "tok")
    monkeypatch.setattr(rev, "_claude", lambda: None)
    assert rev.repo_settings("Me/Play") == ("claude-haiku-5-5", 250000, "alice")
    assert rev.repo_settings("other/x") == ("claude-haiku-5-5", 500000, None)
    assert rev._gh_token("alice") == "good" + "x" * 30 and rev._gh_token(None) == "legacy"

    posted, seen, slack = [], [], []
    monkeypatch.setattr(rev.gh, "get_pr", lambda *a: {"title": "T", "html_url": "http://pr"})
    monkeypatch.setattr(rev.gh, "get_files", lambda *a: [{"filename": "f.py", "patch": "@@ -1,1 +1,2 @@\n a\n+b"}])
    monkeypatch.setattr(rev.gh, "get_file", lambda *a: None)  # no full-file context in this test
    monkeypatch.setattr(rev.gh, "post_review", lambda *a: posted.append(a))
    monkeypatch.setattr(rev, "_slack", lambda *a: slack.append(a))

    def fake_chunk(client, model, text, hints=""):
        seen.append((model, hints))
        issue = {"path": "f.py", "line": 2, "severity": "high", "category": "bug", "message": "null deref"}
        return {"summary": "s", "issues": [issue]}, types.SimpleNamespace(input_tokens=1000, output_tokens=200)
    monkeypatch.setattr(rev.cc, "review_chunk", fake_chunk)

    rev.review({"repo": "Me/Play", "number": 3, "head_sha": "abcdef123", "delivery": "d"})
    assert posted and slack and seen[0][1] == ""
    item = ddb.Table("t").query(KeyConditionExpression=Key("pk").eq("REVIEW"))["Items"][0]
    assert item["issues"][0]["message"] == "null deref" and item["high"] == 1
    s3 = boto3.client("s3")
    key = s3.list_objects_v2(Bucket="analytics-bkt")["Contents"][0]["Key"]
    rec = json.loads(s3.get_object(Bucket="analytics-bkt", Key=key)["Body"].read())
    assert rec["cost_usd"] > 0 and rec["issues"]

    # --- feedback flows into the next review's prompt, and undo removes it
    sk = item["sk"]
    assert c.post("/api/feedback", json={"sk": sk, "idx": 0, "vote": "down"}).get_json()["votes"] == {"0": "down"}
    assert c.post("/api/feedback", json={"sk": sk, "idx": 5, "vote": "up"}).status_code == 404
    assert c.get("/api/reviews").get_json()[0]["votes"] == {"0": "down"}
    rev.review({"repo": "Me/Play", "number": 4, "head_sha": "bbbbbb", "delivery": "d2"})
    assert "NOT useful" in seen[-1][1] and "null deref" in seen[-1][1]
    c.post("/api/feedback", json={"sk": sk, "idx": 0, "vote": ""})
    assert rev.feedback_hints("Me/Play") == ""

    # --- a tiny cost cap stops the review before calling Claude
    c.put("/api/repos", json={"repo": "Me/Play", "model": "claude-sonnet-5-5", "max_cost_usd": 0.01})
    n = len(seen)
    rev.review({"repo": "Me/Play", "number": 5, "head_sha": "cccccc", "delivery": "d3"})
    assert len(seen) == n and "cost cap" in posted[-1][4]

    # --- stats, weekly report, removing things in the right order
    assert c.get("/api/stats").get_json()["reviews"] == 3
    assert "Reviews: 3" in load("report_app", "src/report/app.py").summarize(dash._items(), "2026-01-01T")
    assert c.delete("/api/accounts", json={"login": "alice"}).status_code == 409  # still used by a repo
    assert c.delete("/api/repos", json={"repo": "Me/Play"}).status_code == 200
    assert ("DELETE", "/repos/Me/Play/hooks/42", "alice") in calls
    assert c.delete("/api/accounts", json={"login": "alice"}).status_code == 200
    assert c.get("/api/accounts").get_json() == []
    assert not ssm.describe_parameters(ParameterFilters=[{"Key": "Name", "Option": "BeginsWith", "Values": ["/reviewbot/gh/"]}])["Parameters"]

    # --- the dashboard needs the password once it is configured
    monkeypatch.setenv("DASHBOARD_PASSWORD_PARAM", "p")
    import base64
    assert c.get("/api/stats").status_code == 401 and c.get("/health").status_code == 200
    auth = {"Authorization": "Basic " + base64.b64encode(b"u:secret").decode()}
    assert c.get("/api/stats", headers=auth).status_code == 200


def _table():
    ddb = boto3.resource("dynamodb")
    ddb.create_table(TableName="t", BillingMode="PAY_PER_REQUEST",
                     AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
                     KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}])
    boto3.client("s3").create_bucket(Bucket="analytics-bkt", CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"})
    boto3.client("ssm").put_parameter(Name="/reviewbot/gh/alice", Type="SecureString", Value="tok")
    ddb.Table("t").put_item(Item={"pk": "REPOS", "sk": "Me/Play", "repo": "Me/Play", "model": "claude-haiku-5-5",
                                  "account": "alice", "hook_id": 1, "max_cost_micro": 500000})
    return ddb


AKEY = "AKIA" + "ABCDEFGHIJKLMNOP"


@mock_aws
def test_pr_review_gets_full_file_context_and_scanners(monkeypatch):
    _table()
    rev = load("rev_app2", "src/reviewer/app.py")
    prompts, posted = [], []
    patch = "@@ -1,1 +1,3 @@\n import subprocess\n+subprocess.call(cmd, shell=True)\n+KEY = '" + AKEY + "'"
    full = "import subprocess\nsubprocess.call(cmd, shell=True)\nKEY = '" + AKEY + "'\n"
    monkeypatch.setattr(rev, "_claude", lambda: None)
    monkeypatch.setattr(rev, "_gh_token", lambda a: "tok")
    monkeypatch.setattr(rev.gh, "get_pr", lambda *a: {"title": "T", "html_url": "http://pr"})
    monkeypatch.setattr(rev.gh, "get_files", lambda *a: [{"filename": "x.py", "patch": patch}])
    monkeypatch.setattr(rev.gh, "get_file", lambda repo, path, ref, tok: full)
    monkeypatch.setattr(rev.gh, "post_review", lambda *a: posted.append(a))
    monkeypatch.setattr(rev, "_slack", lambda *a: None)

    def fake_chunk(client, model, text, hints=""):
        prompts.append(text)
        return {"summary": "s", "issues": []}, types.SimpleNamespace(input_tokens=10, output_tokens=5)
    monkeypatch.setattr(rev.cc, "review_chunk", fake_chunk)
    rev.review({"repo": "Me/Play", "number": 1, "head_sha": "abc123", "delivery": "d"})

    assert "Full file (context only)" in prompts[0] and "Scanner findings" in prompts[0]
    assert "bandit B602" in prompts[0] and AKEY not in prompts[0].split("#### Scanner findings")[1]  # findings never echo the key
    # Claude reported nothing, but the leaked key is still posted as a high-severity inline comment
    comments = posted[0][5]
    assert any("AWS access key" in c["body"] and c["line"] == 3 for c in comments)


@mock_aws
def test_repo_scan_from_dashboard_click_to_saved_result(monkeypatch):
    ddb = _table()
    sqs = boto3.client("sqs")
    sqs.create_queue(QueueName="reviewbot-scan-jobs")
    dash, scan = load("dash_app2", "dashboard/app.py"), load("scan_mod", "src/reviewer/scan.py")
    c = dash.app.test_client()

    assert c.post("/api/scans", json={"repo": "Nope/x"}).status_code == 404
    assert c.post("/api/scans", json={"repo": "Me/Play", "max_cost_usd": 99}).status_code == 400
    r = c.post("/api/scans", json={"repo": "Me/Play", "model": "claude-haiku-5-5", "max_cost_usd": 0.5})
    assert r.status_code == 202
    assert c.post("/api/scans", json={"repo": "Me/Play"}).status_code == 409  # one at a time per repo
    url = sqs.get_queue_url(QueueName="reviewbot-scan-jobs")["QueueUrl"]
    msg = sqs.receive_message(QueueUrl=url)["Messages"][0]["Body"]

    files = {"app.py": "import subprocess\nsubprocess.call(cmd, shell=True)\n", ".env": "AWS_KEY=" + AKEY + "\n"}
    tree = [{"path": p, "size": len(t)} for p, t in files.items()] + [{"path": "README.md", "size": 5}, {"path": "logo.png", "size": 9}]
    monkeypatch.setattr(scan.app, "_claude", lambda: None)
    monkeypatch.setattr(scan.app, "_gh_token", lambda a: "tok")
    monkeypatch.setattr(scan.gh, "get_repo", lambda *a: {"default_branch": "main"})
    monkeypatch.setattr(scan.gh, "get_tree", lambda *a: (tree, False))
    monkeypatch.setattr(scan.gh, "get_file", lambda repo, p, ref, tok, mx=0: files.get(p))
    seen = []

    def fake_scan(client, model, text):
        seen.append(text)
        issue = {"path": "app.py", "line": 2, "severity": "high", "category": "security",
                 "message": "shell injection", "suggestion": "pass a list, no shell=True"}
        return {"summary": "A tiny app.", "issues": [issue]}, types.SimpleNamespace(input_tokens=2000, output_tokens=300)
    monkeypatch.setattr(scan.cc, "scan_chunk", fake_scan)
    ctx = types.SimpleNamespace(get_remaining_time_in_millis=lambda: 800_000)

    scan.handler({"Records": [{"body": msg}]}, ctx)
    row = c.get("/api/scans").get_json()[0]
    assert row["status"] == "done" and row["files_scanned"] == 2 and row["files_total"] == 4 and row["branch"] == "main"
    assert "L2 subprocess.call" in seen[0] and "Scanner findings" in seen[0]
    got = {(i["path"], i["line"]) for i in row["issues"]}
    assert ("app.py", 2) in got and (".env", 1) in got                      # Claude's issue + the leaked key
    assert row["issues"][0]["severity"] == "high" and row["cost_micro"] > 0
    assert {ch["name"]: ch["ok"] for ch in row["checks"]}["No .env file committed"] is False and row["score"] < 80

    n = len(seen)
    scan.handler({"Records": [{"body": msg}]}, ctx)                          # duplicate delivery: no second Claude bill
    assert len(seen) == n

    # a failure is saved on the row and never re-raised (a raise would make SQS retry and pay again)
    sk2 = "2999-01-01T00:00:00+00:00#Me/Play"
    ddb.Table("t").put_item(Item={"pk": "SCAN", "sk": sk2, "repo": "Me/Play", "status": "queued", "model": "claude-haiku-5-5"})
    monkeypatch.setattr(scan.gh, "get_repo", lambda *a: 1 / 0)
    scan.handler({"Records": [{"body": json.dumps({"sk": sk2, "repo": "Me/Play", "model": "claude-haiku-5-5", "max_cost_micro": 500000})}]}, ctx)
    failed = ddb.Table("t").get_item(Key={"pk": "SCAN", "sk": sk2})["Item"]
    assert failed["status"] == "failed" and "division" in failed["error"]
