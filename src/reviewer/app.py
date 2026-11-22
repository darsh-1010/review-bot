import datetime, functools, json, os, time, urllib.request
import boto3
from boto3.dynamodb.conditions import Key
import claude_client as cc
import diff_utils as du
import github_client as gh

ssm = boto3.client("ssm")
s3 = boto3.client("s3")
table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
MODEL = os.environ["CLAUDE_MODEL"]            # default; a repo's own setting overrides it
DEFAULT_CAP_MICRO = 500_000                   # $0.50 per review unless the repo sets its own
MAX_INLINE = 5
SEV = {"high": 0, "medium": 1, "low": 2}


@functools.cache  # loaded once per container
def _param(env_name):
    return ssm.get_parameter(Name=os.environ[env_name], WithDecryption=True)["Parameter"]["Value"]


@functools.cache
def _claude():
    return cc.make_client(_param("CLAUDE_KEY_PARAM"))


def handler(event, context):
    for rec in event["Records"]:  # BatchSize is 1; any exception -> SQS retry -> DLQ
        review(json.loads(rec["body"]))


def repo_settings(repo):
    """Per-repo model, cost cap and GitHub account, set from the dashboard. Falls back to the stack defaults."""
    item = table.get_item(Key={"pk": "REPOS", "sk": repo}).get("Item", {})
    model = item.get("model")
    return (model if model in cc.PRICES else MODEL), int(item.get("max_cost_micro", DEFAULT_CAP_MICRO)), item.get("account")


def _gh_token(account):
    """Not cached: a token re-entered on the dashboard applies to the very next review."""
    name = f"/reviewbot/gh/{account}" if account else os.environ["GITHUB_TOKEN_PARAM"]
    return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


def feedback_hints(repo):
    r = table.query(KeyConditionExpression=Key("pk").eq(f"FB#{repo}"), ScanIndexForward=False, Limit=30)
    return cc.hints_text(r["Items"])


def review(job):
    repo, number, sha = job["repo"], job["number"], job["head_sha"]
    key = {"pk": f"PR#{repo}#{number}", "sk": sha}
    if table.get_item(Key=key).get("Item", {}).get("status") == "done":
        return
    model, cap, account = repo_settings(repo)
    t0, token = time.time(), _gh_token(account)
    hints = feedback_hints(repo)

    pr = gh.get_pr(repo, number, token)
    sections, valid, skipped = [], {}, []
    for f in gh.get_files(repo, number, token):
        why = du.skip_reason(f)
        if why:
            skipped.append(f"{f['filename']} ({why})")
            continue
        text, lines = du.number_lines(f["patch"])
        if lines:
            valid[f["filename"]] = lines
            sections.append(f"### {f['filename']}\n{text}")

    summaries, issues, tin, tout = [], [], 0, 0
    for c in du.chunk(sections):
        spent = cc.cost_micro(model, tin, tout)
        if spent + cc.estimate_micro(model, c) > cap:  # worst case would pass the cap: stop here
            skipped.append(f"rest of the diff (cost cap ${cap / 1e6:.2f} reached)")
            break
        out, usage = cc.review_chunk(_claude(), model, c, hints)
        summaries.append(out["summary"])
        issues += out["issues"]
        tin, tout = tin + usage.input_tokens, tout + usage.output_tokens
    issues.sort(key=lambda i: SEV[i["severity"]])

    inline, rest = [], []
    for i in issues:  # only lines GitHub will accept; at most MAX_INLINE
        ok = i["line"] in valid.get(i["path"], ())
        (inline if ok and len(inline) < MAX_INLINE else rest).append(i)

    body = _body(summaries, rest, skipped, model)
    comments = [{"path": i["path"], "line": i["line"], "side": "RIGHT",
                 "body": f"**{i['severity']} · {i['category']}**\n\n{i['message']}"} for i in inline]
    try:
        gh.post_review(repo, number, token, sha, body, comments)
    except gh.Unprocessable:  # e.g. a new commit arrived: repost with everything in the body
        gh.post_review(repo, number, token, sha, _body(summaries, issues, skipped, model), [])

    counts = {s: sum(i["severity"] == s for i in issues) for s in SEV}
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    cost_micro, ms = cc.cost_micro(model, tin, tout), int((time.time() - t0) * 1000)
    table.put_item(Item={
        "pk": "REVIEW", "sk": f"{now}#{repo}#{number}", "repo": repo, "number": number,
        "title": pr["title"], "url": pr["html_url"], "sha": sha, "model": model,
        "high": counts["high"], "medium": counts["medium"], "low": counts["low"],
        "issues": issues,                      # full list, so the dashboard can show details and collect votes
        "input_tokens": tin, "output_tokens": tout, "cost_micro": cost_micro, "duration_ms": ms})
    table.put_item(Item={**key, "status": "done"})

    # Best effort: the review is already posted and saved, so a failure here must not retry the job.
    try:
        _export(repo, number, pr["title"], sha, model, now, counts, tin, tout, cost_micro, ms, issues)
        if counts["high"]:
            _slack(repo, number, pr["title"], pr["html_url"], counts["high"])
    except Exception as e:
        print("post-review step failed:", repr(e))


def _export(repo, number, title, sha, model, now, counts, tin, tout, cost_micro, ms, issues):
    """One JSON line per review in S3, partitioned by day: Athena reads it with SQL."""
    rec = {"repo": repo, "number": number, "title": title, "model": model, "sha": sha, "created_at": now,
           **counts, "input_tokens": tin, "output_tokens": tout, "cost_usd": cost_micro / 1e6,
           "duration_ms": ms, "issues": issues}
    s3.put_object(Bucket=os.environ["ANALYTICS_BUCKET"], Body=json.dumps(rec).encode(),
                  Key=f"reviews/dt={now[:10]}/{repo.replace('/', '_')}-{number}-{sha[:7]}.json")


def _slack(repo, number, title, url, n_high):
    try:
        hook = _param("SLACK_PARAM")
    except ssm.exceptions.ParameterNotFound:
        return  # Slack is optional
    text = f":rotating_light: {n_high} high-severity issue(s) in <{url}|{repo}#{number}> {title}"
    req = urllib.request.Request(hook, data=json.dumps({"text": text}).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()


def _body(summaries, issues, skipped, model):
    parts = ["**ReviewBot** · " + model, "\n\n".join(summaries) or "No reviewable changes."]
    if issues:
        parts.append("\n".join(f"- **{i['severity']}/{i['category']}** `{i['path']}:{i['line']}` {i['message']}"
                               for i in issues))
    if skipped:
        parts.append("_Skipped: " + ", ".join(skipped) + "_")
    return "\n\n".join(parts)
