import base64, functools, hashlib, hmac, json, os, time
import boto3

ssm, sqs = boto3.client("ssm"), boto3.client("sqs")
table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}


@functools.cache  # fetched once per container, not per request
def _secret():
    return ssm.get_parameter(Name=os.environ["WEBHOOK_SECRET_PARAM"],
                             WithDecryption=True)["Parameter"]["Value"].encode()


def verify(secret: bytes, raw: bytes, header: str) -> bool:
    expected = "sha256=" + hmac.new(secret, raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header, expected)  # constant-time, never ==


def handler(event, context):
    body = event.get("body") or ""
    raw = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
    headers = event.get("headers", {})  # HTTP API lowercases header names
    if not verify(_secret(), raw, headers.get("x-hub-signature-256", "")):
        return {"statusCode": 401, "body": "bad signature"}
    kind = headers.get("x-github-event")
    if kind == "ping":
        return {"statusCode": 200, "body": "pong"}
    payload = json.loads(raw)
    if (kind != "pull_request" or payload["action"] not in ACTIONS
            or payload["pull_request"]["draft"]):
        return {"statusCode": 204}
    repo = payload["repository"]["full_name"]
    if "Item" not in table.get_item(Key={"pk": "REPOS", "sk": repo}):  # only repos added on the dashboard
        return {"statusCode": 204}
    delivery = headers["x-github-delivery"]
    try:  # duplicate check
        table.put_item(
            Item={"pk": f"DELIVERY#{delivery}", "sk": "-",
                  "expires_at": int(time.time()) + 7 * 86400},
            ConditionExpression="attribute_not_exists(pk)")
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return {"statusCode": 200, "body": "duplicate"}
    pr = payload["pull_request"]
    sqs.send_message(QueueUrl=os.environ["QUEUE_URL"], MessageBody=json.dumps({
        "repo": payload["repository"]["full_name"], "number": pr["number"],
        "head_sha": pr["head"]["sha"], "delivery": delivery}))
    return {"statusCode": 202, "body": "queued"}
