#!/usr/bin/env bash
# Creates the CloudFront web ACL (rate limit + AWS managed rules). CloudFront web ACLs only exist in
# us-east-1, and the org blocks CloudFormation there, so this uses the CLI. Prints the ARN for sam deploy.
# Safe to rerun: reuses an existing ACL. Delete later with: aws wafv2 delete-web-acl (needs its lock token).
set -euo pipefail
cd "$(dirname "$0")/.."
NAME=reviewbot-dashboard-acl
ARN=$(aws wafv2 list-web-acls --scope CLOUDFRONT --region us-east-1 \
  --query "WebACLs[?Name=='$NAME'].ARN | [0]" --output text)
if [ "$ARN" = "None" ]; then
  ARN=$(aws wafv2 create-web-acl --name "$NAME" --scope CLOUDFRONT --region us-east-1 \
    --default-action Allow={} \
    --visibility-config SampledRequestsEnabled=true,CloudWatchMetricsEnabled=true,MetricName=reviewbot-waf \
    --rules file://waf/rules.json --query Summary.ARN --output text)
fi
echo "WebAclArn=$ARN"
