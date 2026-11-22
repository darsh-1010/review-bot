"""One pass through the whole app on a simulated AWS (moto): GitHub accounts, repos, reviews, feedback, cost cap."""
import importlib.util, json, os, pathlib, sys, types, urllib.error

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


def http_error(code):
    return urllib.error.HTTPError("u", code, "x", {}, None)


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

    # --- repos: connecting creates the webhook with that account's token
    ok = c.post("/api/repos", json={"repo": "https://github.com/me/play", "account": "alice",
                                    "model": "claude-sonnet-5-5", "max_cost_usd": 0.05})
    assert ok.status_code == 201 and ("POST", "/repos/Me/Play/hooks", "alice") in calls
    assert c.post("/api/repos", json={"repo": "nonsense"}).status_code == 400
    assert c.post("/api/repos", json={"repo": "a/b", "model": "gpt"}).status_code == 400
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
