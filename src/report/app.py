"""Weekly summary: EventBridge schedule -> this Lambda -> SNS email."""
import datetime, os
import boto3
from boto3.dynamodb.conditions import Key

table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
sns = boto3.client("sns")


def summarize(items, since):
    n = len(items)
    cost = sum(float(i["cost_micro"]) for i in items) / 1e6
    sev = {s: sum(int(i[s]) for i in items) for s in ("high", "medium", "low")}
    by_repo = {}
    for i in items:
        by_repo[i["repo"]] = by_repo.get(i["repo"], 0) + 1
    votes = [v for i in items for v in i.get("votes", {}).values()]
    lines = [f"ReviewBot weekly report (since {since[:10]})", "",
             f"Reviews: {n}", f"Issues: {sev['high']} high, {sev['medium']} medium, {sev['low']} low",
             f"Cost: ${cost:.4f} (avg ${cost / n if n else 0:.4f} per review)",
             f"Feedback: {votes.count('up')} thumbs up, {votes.count('down')} thumbs down", ""]
    lines += [f"  {r}: {c} reviews" for r, c in sorted(by_repo.items(), key=lambda x: -x[1])]
    return "\n".join(lines)


def scan_section(scans):
    """Repos whose latest finished scan scores worse than the one before, plus repos with new high-severity issues."""
    by_repo = {}
    for i in sorted((i for i in scans if i.get("status") == "done"), key=lambda i: i["sk"]):
        by_repo.setdefault(i["repo"], []).append(i)
    lines = []
    for repo, runs in sorted(by_repo.items()):
        if len(runs) < 2:
            continue
        old, new = runs[-2], runs[-1]
        highs = sum(1 for x in new.get("issues", []) if x["severity"] == "high")
        if int(new["score"]) < int(old["score"]):
            lines.append(f"  {repo}: score {int(old['score'])} -> {int(new['score'])} ({highs} high-severity issues now)")
    return ["", "Repo scans that got worse since the previous scan:"] + lines if lines else []


def handler(event, context):
    since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)).isoformat()
    items, kw = [], dict(KeyConditionExpression=Key("pk").eq("REVIEW") & Key("sk").gte(since))
    while True:
        r = table.query(**kw)
        items += r["Items"]
        if "LastEvaluatedKey" not in r:
            break
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    month = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=45)).isoformat()
    scans = table.query(KeyConditionExpression=Key("pk").eq("SCAN") & Key("sk").gte(month))["Items"]
    sns.publish(TopicArn=os.environ["TOPIC_ARN"], Subject="ReviewBot weekly report",
                Message="\n".join([summarize(items, since)] + scan_section(scans)))
