#!/usr/bin/env bash
# Re-points CloudFront at the dashboard server's current address (run after the instance is stopped/started).
set -euo pipefail
cd "$(dirname "$0")/.."
out() { aws cloudformation describe-stacks --stack-name reviewbot \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
INSTANCE=$(out DashboardInstanceId)
aws ec2 wait instance-running --instance-ids "$INSTANCE"
DNS=$(aws ec2 describe-instances --instance-ids "$INSTANCE" \
  --query "Reservations[0].Instances[0].PublicDnsName" --output text)
PREFIX=$(aws ec2 describe-managed-prefix-lists \
  --filters Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing \
  --query "PrefixLists[0].PrefixListId" --output text)
SECRET=$(aws ssm get-parameter --name /reviewbot/origin-secret --with-decryption \
  --query Parameter.Value --output text)
MODEL=${CLAUDE_MODEL:-claude-haiku-5-5}
sam deploy --no-confirm-changeset --no-fail-on-empty-changeset --parameter-overrides \
  ClaudeModel="$MODEL" CloudFrontPrefixList="$PREFIX" OriginSecret="$SECRET" DashboardOrigin="$DNS" \
  ${WEB_ACL_ARN:+WebAclArn=$WEB_ACL_ARN} ${ALERT_EMAIL:+AlertEmail=$ALERT_EMAIL} ${ALERT_EMAIL2:+AlertEmail2=$ALERT_EMAIL2}   # optional: keep the WAF / alert email on every deploy
echo "Dashboard: $(out DashboardUrl)"
