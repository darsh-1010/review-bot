import hashlib, hmac, importlib.util, json, os, pathlib

os.environ.update(TABLE_NAME="t", QUEUE_URL="q", WEBHOOK_SECRET_PARAM="p", AWS_DEFAULT_REGION="ap-southeast-2")
ROOT = pathlib.Path(__file__).parent
# load by path: the reviewer also has an app.py
spec = importlib.util.spec_from_file_location("receiver_app", ROOT.parent / "src/receiver/app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

SECRET = b"s3cret"
BODY = (ROOT / "events/pull_request_opened.json").read_text()


def sign(raw: bytes, secret=SECRET):
    return "sha256=" + hmac.new(secret, raw, hashlib.sha256).hexdigest()


class FakeTable:
    def __init__(self, repos=("me/reviewbot-playground",)): self.repos = repos
    def put_item(self, **kw): pass
    def get_item(self, Key):  # only repos added on the dashboard are in REPOS
        return {"Item": {}} if Key["pk"] == "REPOS" and Key["sk"] in self.repos else {}


class FakeSQS:
    def __init__(self): self.sent = []
    def send_message(self, **kw): self.sent.append(kw)


def event(kind, sig, body=BODY):
    return {"body": body, "isBase64Encoded": False,
            "headers": {"x-hub-signature-256": sig, "x-github-event": kind, "x-github-delivery": "d1"}}


def setup(monkeypatch, repos=("me/reviewbot-playground",)):
    monkeypatch.setattr(app, "_secret", lambda: SECRET)
    monkeypatch.setattr(app, "table", FakeTable(repos))
    q = FakeSQS()
    monkeypatch.setattr(app, "sqs", q)
    return q


def test_verify():
    assert app.verify(SECRET, b"x", sign(b"x"))
    assert not app.verify(SECRET, b"x", sign(b"x", b"wrong"))
    assert not app.verify(SECRET, b"x", "sha256=0")
    assert not app.verify(SECRET, b"x", "")


def test_bad_signature_401(monkeypatch):
    setup(monkeypatch)
    assert app.handler(event("pull_request", "sha256=0"), None)["statusCode"] == 401


def test_ping_pong(monkeypatch):
    setup(monkeypatch)
    assert app.handler(event("ping", sign(BODY.encode())), None)["body"] == "pong"


def test_pr_is_queued(monkeypatch):
    q = setup(monkeypatch)
    r = app.handler(event("pull_request", sign(BODY.encode())), None)
    assert r["statusCode"] == 202
    assert json.loads(q.sent[0]["MessageBody"]) == {
        "repo": "me/reviewbot-playground", "number": 7, "head_sha": "abc123", "delivery": "d1"}


def test_unconnected_repo_ignored(monkeypatch):
    q = setup(monkeypatch, repos=())
    assert app.handler(event("pull_request", sign(BODY.encode())), None)["statusCode"] == 204
    assert not q.sent


def test_ignored_action(monkeypatch):
    q = setup(monkeypatch)
    body = BODY.replace('"opened"', '"closed"')
    assert app.handler(event("pull_request", sign(body.encode()), body), None)["statusCode"] == 204
    assert not q.sent
