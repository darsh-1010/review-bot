#!/usr/bin/env bash
# Phase 3. Run once. Rerunning makes NEW random secrets: update GitHub's webhook secret too.
set -euo pipefail
read -rsp "Claude API key: " CLAUDE_KEY; echo
read -rsp "GitHub token (optional, Enter to skip: add GitHub accounts on the dashboard instead): " GH_TOKEN; echo
read -rsp "Slack incoming-webhook URL (optional, Enter to skip): " SLACK_URL; echo
WEBHOOK_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
ORIGIN_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(24))")
put() { aws ssm put-parameter --name "$1" --type SecureString --value "$2" --overwrite > /dev/null; }
put /reviewbot/anthropic-api-key "$CLAUDE_KEY"
[ -n "$GH_TOKEN" ] && put /reviewbot/github-token "$GH_TOKEN"
put /reviewbot/webhook-secret "$WEBHOOK_SECRET"
put /reviewbot/origin-secret "$ORIGIN_SECRET"
DASH_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(16))")
put /reviewbot/dashboard-password "$DASH_PASSWORD"
[ -n "$SLACK_URL" ] && put /reviewbot/slack-webhook "$SLACK_URL"
echo "Webhook secret for GitHub (Phase 4): $WEBHOOK_SECRET"
echo "Dashboard login (any username): $DASH_PASSWORD"
