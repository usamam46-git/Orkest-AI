#!/usr/bin/env bash
# Provisions everything DEPLOY-AWS.md sections 1-3 and 10 describe, with the AWS CLI.
# Safe to re-run: every create step checks for the resource first.
#
#   ALERT_EMAIL=you@example.com ./aws-provision.sh
#
# Creates: budget, key pair, security group, backup bucket + lifecycle, documents
# bucket (knowledge-base uploads; replaces MinIO in production), IAM role/profile, t4g.medium instance, Elastic IP. Nothing else — no NAT, no ALB.
set -euo pipefail

: "${ALERT_EMAIL:?set ALERT_EMAIL for the budget alerts}"
REGION="${AWS_REGION:-us-east-1}"
NAME="orkest"
# t4g.medium is refused on the free plan; c7i-flex.large (2 vCPU x86, 4 GB) is the
# 4 GB type it allows. Override both together for another size/arch.
INSTANCE_TYPE="${INSTANCE_TYPE:-c7i-flex.large}"
UBUNTU_ARCH="${UBUNTU_ARCH:-amd64}"
export AWS_DEFAULT_REGION="$REGION" AWS_PAGER=""

HERE="$(cd "$(dirname "$0")" && pwd)"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="orkest-backups-${ACCOUNT}"
DOCS_BUCKET="orkest-documents-${ACCOUNT}"
MY_IP="$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')"
KEY_FILE="$HOME/.ssh/${NAME}.pem"

echo "account=$ACCOUNT region=$REGION ssh-from=$MY_IP/32 backups=$BUCKET documents=$DOCS_BUCKET"

# --- 1. budget: $75/mo, credits NOT netted out, 80% actual + 100% forecast ----
if ! aws budgets describe-budget --account-id "$ACCOUNT" --budget-name orkest-monthly >/dev/null 2>&1; then
  aws budgets create-budget --account-id "$ACCOUNT" \
    --budget '{"BudgetName":"orkest-monthly","BudgetType":"COST","TimeUnit":"MONTHLY",
      "BudgetLimit":{"Amount":"75","Unit":"USD"},
      "CostTypes":{"IncludeCredit":false,"IncludeRefund":false,"IncludeDiscount":false}}' \
    --notifications-with-subscribers "[
      {\"Notification\":{\"NotificationType\":\"ACTUAL\",\"ComparisonOperator\":\"GREATER_THAN\",\"Threshold\":80,\"ThresholdType\":\"PERCENTAGE\"},
       \"Subscribers\":[{\"SubscriptionType\":\"EMAIL\",\"Address\":\"$ALERT_EMAIL\"}]},
      {\"Notification\":{\"NotificationType\":\"FORECASTED\",\"ComparisonOperator\":\"GREATER_THAN\",\"Threshold\":100,\"ThresholdType\":\"PERCENTAGE\"},
       \"Subscribers\":[{\"SubscriptionType\":\"EMAIL\",\"Address\":\"$ALERT_EMAIL\"}]}]"
  echo "budget created"
fi

# --- 2. key pair (private key only exists at creation time) -------------------
if ! aws ec2 describe-key-pairs --key-names "$NAME" >/dev/null 2>&1; then
  mkdir -p "$HOME/.ssh"
  aws ec2 create-key-pair --key-name "$NAME" --key-type ed25519 \
    --query KeyMaterial --output text > "$KEY_FILE"
  chmod 400 "$KEY_FILE"
  echo "key pair created -> $KEY_FILE"
elif [ ! -f "$KEY_FILE" ]; then
  echo "key pair '$NAME' exists in AWS but $KEY_FILE is missing; delete the key pair and re-run" >&2
  exit 1
fi

# --- 3. security group: 22 from my IP only, 80/443 open -----------------------
VPC="$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)"
SG="$(aws ec2 describe-security-groups --filters Name=group-name,Values="$NAME" Name=vpc-id,Values="$VPC" \
      --query 'SecurityGroups[0].GroupId' --output text)"
if [ "$SG" = "None" ]; then
  SG="$(aws ec2 create-security-group --group-name "$NAME" --description "Orkest single-box stack" \
        --vpc-id "$VPC" --query GroupId --output text)"
  aws ec2 authorize-security-group-ingress --group-id "$SG" --ip-permissions \
    "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=${MY_IP}/32,Description=ssh-my-ip}]" \
    "IpProtocol=tcp,FromPort=80,ToPort=80,IpRanges=[{CidrIp=0.0.0.0/0,Description=acme-and-redirect}]" \
    "IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges=[{CidrIp=0.0.0.0/0,Description=app}]" >/dev/null
  echo "security group $SG created"
fi

# --- 4. buckets: private, encrypted; backups also expire after 30 days -------
make_bucket() {
  if ! aws s3api head-bucket --bucket "$1" 2>/dev/null; then
    # us-east-1 rejects a LocationConstraint; every other region requires one.
    if [ "$REGION" = "us-east-1" ]; then
      aws s3api create-bucket --bucket "$1" >/dev/null
    else
      aws s3api create-bucket --bucket "$1" \
        --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
    fi
  fi
  aws s3api put-public-access-block --bucket "$1" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  aws s3api put-bucket-encryption --bucket "$1" --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
}
make_bucket "$BUCKET"
make_bucket "$DOCS_BUCKET"
aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --lifecycle-configuration \
  '{"Rules":[{"ID":"expire-db-dumps","Status":"Enabled","Filter":{"Prefix":"db/"},
    "Expiration":{"Days":30},"AbortIncompleteMultipartUpload":{"DaysAfterInitiation":2}}]}'

# --- 5. IAM: PutObject-only on backups; read/write/delete on the documents bucket
if ! aws iam get-role --role-name "${NAME}-backup" >/dev/null 2>&1; then
  aws iam create-role --role-name "${NAME}-backup" --assume-role-policy-document \
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
fi
aws iam put-role-policy --role-name "${NAME}-backup" --policy-name orkest-s3 --policy-document "{
  \"Version\":\"2012-10-17\",\"Statement\":[
   {\"Sid\":\"BackupsPutOnly\",\"Effect\":\"Allow\",\"Action\":\"s3:PutObject\",\"Resource\":\"arn:aws:s3:::${BUCKET}/db/*\"},
   {\"Sid\":\"DocumentsList\",\"Effect\":\"Allow\",\"Action\":\"s3:ListBucket\",\"Resource\":\"arn:aws:s3:::${DOCS_BUCKET}\"},
   {\"Sid\":\"DocumentsObjects\",\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:PutObject\",\"s3:DeleteObject\"],\"Resource\":\"arn:aws:s3:::${DOCS_BUCKET}/*\"}]}"
aws iam delete-role-policy --role-name "${NAME}-backup" --policy-name put-only 2>/dev/null || true
if ! aws iam get-instance-profile --instance-profile-name "${NAME}-backup" >/dev/null 2>&1; then
  aws iam create-instance-profile --instance-profile-name "${NAME}-backup" >/dev/null
  aws iam add-role-to-instance-profile --instance-profile-name "${NAME}-backup" --role-name "${NAME}-backup"
  sleep 10   # IAM is eventually consistent; run-instances rejects a profile seconds old
fi

# --- 6. instance --------------------------------------------------------------
IID="$(aws ec2 describe-instances \
  --filters Name=tag:Name,Values="$NAME" Name=instance-state-name,Values=pending,running,stopped \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)"
if [ "$IID" = "None" ]; then
  AMI="$(aws ssm get-parameter \
    --name /aws/service/canonical/ubuntu/server/24.04/stable/current/${UBUNTU_ARCH}/hvm/ebs-gp3/ami-id \
    --query Parameter.Value --output text)"
  IID="$(aws ec2 run-instances --image-id "$AMI" --instance-type "$INSTANCE_TYPE" \
    --key-name "$NAME" --security-group-ids "$SG" \
    --iam-instance-profile Name="${NAME}-backup" \
    --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=40,VolumeType=gp3,DeleteOnTermination=true}' \
    --metadata-options HttpTokens=required,HttpPutResponseHopLimit=2 \
    --user-data "file://${HERE}/ec2-user-data.sh" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME}]" \
                         "ResourceType=volume,Tags=[{Key=Name,Value=$NAME}]" \
    --query 'Instances[0].InstanceId' --output text)"
  echo "instance $IID launched ($AMI)"
fi
aws ec2 wait instance-running --instance-ids "$IID"

# --- 7. Elastic IP ------------------------------------------------------------
EIP="$(aws ec2 describe-addresses --filters Name=tag:Name,Values="$NAME" \
       --query 'Addresses[0].PublicIp' --output text)"
if [ "$EIP" = "None" ]; then
  ALLOC="$(aws ec2 allocate-address --domain vpc \
    --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=$NAME}]" \
    --query AllocationId --output text)"
  aws ec2 associate-address --instance-id "$IID" --allocation-id "$ALLOC" >/dev/null
  EIP="$(aws ec2 describe-addresses --allocation-ids "$ALLOC" --query 'Addresses[0].PublicIp' --output text)"
fi

cat <<EOF

instance : $IID
elastic  : $EIP
backups  : $BUCKET   (BACKUP_S3_URI=s3://$BUCKET/db/)
documents: $DOCS_BUCKET   (S3_DOCUMENTS_BUCKET=$DOCS_BUCKET, AWS_REGION=$REGION)
ssh      : ssh -i $KEY_FILE ubuntu@$EIP

Next: set the Namecheap A record  orkest -> $EIP,  then  dig +short \${DOMAIN:-your.domain}
EOF
