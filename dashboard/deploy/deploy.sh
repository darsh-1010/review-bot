#!/bin/bash
# Runs on the EC2 server as root, through SSM Run Command.
set -euo pipefail
BUCKET=$1; KEY=$2; TABLE=$3; WEBHOOK_URL=$4; REGION=ap-southeast-2
aws s3 cp "s3://$BUCKET/$KEY" /tmp/dashboard.zip --region $REGION
rm -rf /opt/reviewbot/dashboard.new
unzip -q /tmp/dashboard.zip -d /opt/reviewbot/dashboard.new
[ -d /opt/reviewbot/venv ] || python3.12 -m venv /opt/reviewbot/venv
/opt/reviewbot/venv/bin/pip install -q -r /opt/reviewbot/dashboard.new/requirements.txt
rm -rf /opt/reviewbot/dashboard && mv /opt/reviewbot/dashboard.new /opt/reviewbot/dashboard
chown -R reviewbot:reviewbot /opt/reviewbot
printf 'TABLE_NAME=%s\nAWS_DEFAULT_REGION=%s\nWEBHOOK_URL=%s\nWEBHOOK_SECRET_PARAM=/reviewbot/webhook-secret\nGITHUB_TOKEN_PARAM=/reviewbot/github-token\nDASHBOARD_PASSWORD_PARAM=/reviewbot/dashboard-password\n' \
  "$TABLE" "$REGION" "$WEBHOOK_URL" > /etc/reviewbot.env
chmod 640 /etc/reviewbot.env && chown root:reviewbot /etc/reviewbot.env
SECRET=$(aws ssm get-parameter --name /reviewbot/origin-secret --with-decryption \
  --query Parameter.Value --output text --region $REGION)
sed "s/__ORIGIN_SECRET__/$SECRET/" /opt/reviewbot/dashboard/deploy/nginx.conf \
  > /etc/nginx/conf.d/reviewbot.conf
cp /opt/reviewbot/dashboard/deploy/reviewbot-dashboard.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable reviewbot-dashboard
systemctl restart reviewbot-dashboard
nginx -t && systemctl reload nginx
dnf -y upgrade --security   # Phase 9: every deploy also patches the OS
