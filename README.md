# ReviewBot

Claude reviews every GitHub pull request; a dashboard shows the results and lets you tune it.

```
GitHub webhook -> API Gateway -> Receiver Lambda -> SQS -> Reviewer Lambda -> GitHub review
                                                                   |-> DynamoDB  (dashboard reads it)
                                                                   |-> S3 JSON   (Athena queries it)
                                                                   '-> Slack alert on high-severity issues
Dashboard: Flask on EC2 behind CloudFront + WAF.   EventBridge (Mon 13:00 UTC) -> Report Lambda -> SNS email.
Repo scan tab: dashboard -> SQS (reviewbot-scan-jobs) -> Scan Lambda (15 min) -> GitHub files -> bandit + secrets + Claude -> DynamoDB.
```

## Features

| Feature | How it works |
|---|---|
| **GitHub accounts** | Paste a personal access token on the dashboard (one per GitHub account or org). It is checked, then stored encrypted in SSM; it is never shown again. |
| **Connect repos** | Pick an account, enter a repo, and the dashboard creates the GitHub webhook for you. Only connected repos are reviewed. |
| **Review settings** | Per repo: Haiku or Sonnet, and a max cost per review (worst-case estimate; the review stops early and says so). |
| **Filters + detail** | Search and filter by repo, model, severity. Click a row for the full issue list. |
| **Learning from feedback** | 👍/👎 on an issue is stored; the next reviews of that repo get the latest ones in the prompt. |
| **Full-file context** | Claude sees each changed file in full (up to 25 files, 40k chars each), not just the diff, but only comments on changed lines. |
| **Scanners** | Before Claude, `bandit` (Python security linter) and a secret scanner run on the changed files. Bandit findings go into the prompt for Claude to explain and rank; a leaked key is always posted as a high-severity comment. `semgrep` and `eslint` are not included: they don't fit a plain Lambda zip (add them later in a container image). |
| **Repo scan tab** | Pick a connected repo, model and cost cap. The Scan Lambda reads up to 80 prioritised files (config and auth code first), runs the scanners, has Claude audit them against best practice, and saves a health score, a best-practice checklist (README, tests, CI, lockfile, SECURITY.md, no committed `.env`...) and issues with a suggested fix each. One scan per repo at a time. |
| **Slack alerts** | Optional. A message when a review finds a high-severity issue. |
| **Weekly report** | EventBridge schedule -> Lambda -> SNS email (set `AlertEmail`). |
| **Athena** | Every review is also written to S3; query it in the `reviewbot` workgroup (two saved queries included). |
| **WAF** | Created by `scripts/create-waf.sh`, attached to CloudFront: 300 requests / 5 min / IP, plus AWS common and known-bad-input rule sets. |
| **Login** | The dashboard asks for a password (HTTP Basic, any username). It has write buttons, so it is not open. |

## One-time setup (normal AWS account)

1. Accounts: Anthropic key and your GitHub repos. GitHub tokens are added later on the dashboard (fine-grained, one per account,
   with **Pull requests: write, Contents: read, Webhooks: read & write** on the repos you will connect); `put-secrets.sh` can skip it. Optional: a Slack incoming-webhook URL.
2. Tools: Python 3.13, AWS CLI v2 (`aws configure` with an IAM user or SSO), SAM CLI, Git, GitHub CLI.
3. `aws configure set region ap-southeast-2` (the course account only allows most services in Sydney), then:
   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install anthropic boto3 flask pytest
   pytest -q
   ./scripts/put-secrets.sh   # asks for the keys; prints the dashboard password
   ```

## First deploy

```bash
PREFIX=$(aws ec2 describe-managed-prefix-lists \
  --filters Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing \
  --query "PrefixLists[0].PrefixListId" --output text)
SECRET=$(aws ssm get-parameter --name /reviewbot/origin-secret --with-decryption \
  --query Parameter.Value --output text)
sam build
./scripts/create-waf.sh   # optional WAF: made in us-east-1 by the CLI (CloudFront requires it); prints WebAclArn=...
sam deploy --guided   # stack reviewbot, ap-southeast-2, allow IAM role creation, accept the no-auth warning
# parameters: CloudFrontPrefixList=$PREFIX  OriginSecret=$SECRET  DashboardOrigin=(empty)  AlertEmail=you@example.com  WebAclArn=(the ARN, or empty to skip WAF)
```

Then upload the dashboard once by hand (zip -> S3 -> `aws ssm send-command`, see `deploy.yml` for the exact commands),
run `./scripts/start-session.sh` to point CloudFront at the server, open the `DashboardUrl` output, log in, and
**connect a repo** in the form. Open a PR there; a review appears within about a minute. Confirm the SNS email subscription.

Athena: console -> workgroup `reviewbot` -> Saved queries (`issues-by-category`, `cost-by-repo-and-model`).

## CI/CD

`ci.yml` runs tests and `cfn-lint` on PRs; `deploy.yml` deploys on push to `main`. Repo secrets
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (a deploy-only IAM user); variables `CLAUDE_MODEL`, `CF_PREFIX_LIST`, and optionally `WEB_ACL_ARN`, `ALERT_EMAIL` and `ALERT_EMAIL2` (otherwise a deploy drops the WAF and alerts).
Protect `main` and require the `ci` check.

## Layout

```
template.yaml            all AWS resources (own IAM roles per function and for the dashboard server)
src/receiver/            webhook: verify signature, only connected repos, dedupe, enqueue
src/reviewer/            SQS job -> GitHub files -> Claude (+feedback) -> review -> DynamoDB, S3, Slack
src/report/              weekly summary -> SNS
dashboard/               Flask API + 3D/GSAP UI; deploy/ (Nginx, systemd, deploy.sh)
scripts/                 put-secrets, start-session
tests/                   signature, diff, prompt-hint and cost-cap tests
```

## Cost notes

Small: Lambda/SQS/DynamoDB/Athena are pennies at this volume. The steady costs are the EC2 t3.micro and WAF
(about $8-10/month). Add an AWS Budget alarm (e.g. $20). A per-query Athena cap (1 GB) is already set.

## Cleanup

Empty the release and analytics buckets, `sam delete --stack-name reviewbot`, delete the `/reviewbot/*` SSM
parameters and old log groups, remove the webhooks (Remove buttons on the dashboard do it), revoke the GitHub and Anthropic keys.
