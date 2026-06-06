# Deploying snowtuner on AWS

This walks through deploying snowtuner to a single EC2 instance in your AWS
account. You'll reach the UI through an SSM port-forward — no public URL,
no extra vendors, no certificate management. **~$17/month** total.

This is the default deploy story. If later you want a real custom domain or
multi-user access, see the [Upgrade paths](#upgrade-paths) section at the bottom.

## When to actually do this

snowtuner runs fine on your laptop and that's the right way to evaluate it.
Reach for an EC2 deploy when you want:

- snowtuner to keep syncing/recommending while your laptop is asleep
- the automation loop to apply autonomous changes on a schedule
- to leave it running for a team

If you're just kicking the tires, **skip this guide** and run snowtuner
locally first.

## What this gets you

```
┌─────────────────────┐
│  your laptop        │
│                     │
│  $ aws ssm start... │ ──── SSM port-forward over outbound HTTPS ────┐
│  http://localhost:  │                                                │
│         8770        │                                                │
└─────────────────────┘                                                ▼
                                                       ┌────────────────────────┐
                                                       │  EC2 t3.small          │
                                                       │  (us-west-2, your VPC) │
                                                       │                        │
                                                       │  snowtuner :8770       │
                                                       │  (loopback only)       │
                                                       │                        │
                                                       │  EBS @ 20GB            │
                                                       │  /var/lib/snowtuner    │
                                                       └────────────┬───────────┘
                                                                    │
                                                          (outbound only)
                                                                    ▼
                                                       ┌──────────────────────┐
                                                       │  your Snowflake      │
                                                       │  account             │
                                                       └──────────────────────┘
```

**No inbound ports.** The security group has zero rules. snowtuner binds to
`127.0.0.1:8770` — only reachable from the EC2 instance itself. SSM Session
Manager forwards `localhost:8770` on your laptop to `localhost:8770` on the
instance, over an outbound HTTPS connection from the SSM agent.

## Prerequisites

- AWS account, with permissions to create IAM roles, EC2, EBS, Secrets Manager
- AWS CLI installed: `brew install awscli`
- AWS CLI configured: `aws configure` (region `us-west-2`)
- SSM plugin: `brew install --cask session-manager-plugin`
- Your Snowflake service-user **private key** (`~/.snowtuner/snowflake_rsa_key.p8`
  if you've been running snowtuner locally)
- Your Snowflake account locator, service-user name, default warehouse, role

Confirm AWS is wired up:
```bash
aws sts get-caller-identity
```

---

## 1. Stash the Snowflake credentials in Secrets Manager

One JSON secret carries everything: connection info **and** the RSA private key.
The bootstrap script fetches it on every boot and writes the key to `/var/lib/snowtuner/snowflake_rsa_key.p8` mode 0600.

```bash
cd /tmp
cat > snowtuner-secret.json <<'EOF'
{
  "account":         "REPLACE_ME-abc12345",
  "user":            "SNOWTUNER_SVC",
  "warehouse":       "COMPUTE_WH",
  "role":            "SNOWTUNER_ROLE",
  "private_key_pem": "REPLACE_WITH_PEM_CONTENT"
}
EOF

# Slurp your local PEM file in (preserves newlines as \n)
jq --rawfile pem ~/.snowtuner/snowflake_rsa_key.p8 \
   '.private_key_pem = $pem' \
   snowtuner-secret.json > snowtuner-secret-final.json

# Open and fix the other fields:
$EDITOR snowtuner-secret-final.json
```

Push it to Secrets Manager and capture the ARN:
```bash
SECRET_ARN=$(aws secretsmanager create-secret \
  --name snowtuner/snowflake \
  --description "Snowflake service-user creds for snowtuner" \
  --secret-string file:///tmp/snowtuner-secret-final.json \
  --region us-west-2 \
  --query ARN --output text)
echo "Secret ARN: ${SECRET_ARN}"
```

When you're done: `shred -u /tmp/snowtuner-secret*.json`.

---

## 2. Create an IAM role for the EC2 instance

The instance needs `secretsmanager:GetSecretValue` on the secret you just
created, and the AWS-managed SSM policy so we can shell in.

```bash
# 2a. Trust policy — EC2 can assume this role.
cat > /tmp/trust.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "ec2.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
EOF

aws iam create-role \
  --role-name snowtuner-ec2 \
  --assume-role-policy-document file:///tmp/trust.json

# 2b. SSM session manager.
aws iam attach-role-policy \
  --role-name snowtuner-ec2 \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

# 2c. Read the snowtuner secret.
cat > /tmp/secret-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "secretsmanager:GetSecretValue",
    "Resource": "${SECRET_ARN}"
  }]
}
EOF

aws iam put-role-policy \
  --role-name snowtuner-ec2 \
  --policy-name read-snowtuner-secret \
  --policy-document file:///tmp/secret-policy.json

# 2d. Instance profile (the thing EC2 actually attaches).
aws iam create-instance-profile --instance-profile-name snowtuner-ec2
aws iam add-role-to-instance-profile \
  --instance-profile-name snowtuner-ec2 \
  --role-name snowtuner-ec2

# IAM takes a few seconds to propagate.  Give it a moment.
sleep 10
```

---

## 3. Create a security group with no inbound rules

```bash
VPC_ID=$(aws ec2 describe-vpcs \
  --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].VpcId' --output text)

SG_ID=$(aws ec2 create-security-group \
  --group-name snowtuner-ec2 \
  --description "snowtuner: SSM-only, no inbound" \
  --vpc-id "${VPC_ID}" \
  --query 'GroupId' --output text)
echo "SG: ${SG_ID}"
```

The default outbound (all traffic) is what we need — SSM agent, package
mirrors, Secrets Manager, Snowflake. The default inbound is closed.

---

## 4. Create the EBS data volume

```bash
AZ=$(aws ec2 describe-availability-zones --region us-west-2 \
  --query 'AvailabilityZones[0].ZoneName' --output text)

VOL_ID=$(aws ec2 create-volume \
  --size 20 \
  --volume-type gp3 \
  --availability-zone "${AZ}" \
  --tag-specifications 'ResourceType=volume,Tags=[{Key=Name,Value=snowtuner-data}]' \
  --query 'VolumeId' --output text)
echo "Volume: ${VOL_ID}"
```

---

## 5. Launch the EC2 instance

```bash
AMI=$(aws ssm get-parameter \
  --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --query 'Parameter.Value' --output text)

SUBNET=$(aws ec2 describe-subnets \
  --filters "Name=vpc-id,Values=${VPC_ID}" "Name=availability-zone,Values=${AZ}" \
  --query 'Subnets[0].SubnetId' --output text)

INSTANCE_ID=$(aws ec2 run-instances \
  --image-id "${AMI}" \
  --instance-type t3.small \
  --subnet-id "${SUBNET}" \
  --security-group-ids "${SG_ID}" \
  --iam-instance-profile Name=snowtuner-ec2 \
  --metadata-options 'HttpTokens=required,HttpPutResponseHopLimit=2' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=snowtuner}]' \
  --query 'Instances[0].InstanceId' --output text)
echo "Instance: ${INSTANCE_ID}"

aws ec2 wait instance-running --instance-ids "${INSTANCE_ID}"

aws ec2 attach-volume \
  --instance-id "${INSTANCE_ID}" \
  --volume-id "${VOL_ID}" \
  --device /dev/sdf
```

---

## 6. SSM into the instance and run bootstrap

```bash
aws ssm start-session --target "${INSTANCE_ID}"
```

You're now sitting on the EC2 box. Switch to `ec2-user` (the one with sudo):

```bash
sudo -i -u ec2-user
```

Clone the repo and run bootstrap. **Replace `${SECRET_ARN}`** with the value
from step 1 (the session doesn't carry your local shell vars):

```bash
sudo dnf -y install git
sudo mkdir -p /opt/snowtuner
sudo chown ec2-user:ec2-user /opt/snowtuner
git clone https://github.com/austinkjensen/snowtuner.git /opt/snowtuner

sudo \
  SNOWTUNER_SECRET_ID=arn:aws:secretsmanager:us-west-2:...:secret:snowtuner/snowflake-XXXXX \
  AWS_REGION=us-west-2 \
  bash /opt/snowtuner/deploy/bootstrap.sh
```

The script:
- formats + mounts the EBS volume at `/var/lib/snowtuner`
- installs git, Node 22, Python 3.11, uv
- creates the `snowtuner` system user
- builds the SPA (`web/dist`)
- fetches the secret and writes the env file
- installs and starts the systemd unit

Wait for `✓ snowtuner API responding on :8770` at the end.

Grab the auto-generated API token — you'll paste it into the UI in step 8:

```bash
sudo cat /var/lib/snowtuner/api_token
```

Exit the SSM session: `exit` twice (once to leave `ec2-user`, once to leave `ssm-user`).

---

## 7. Reach the UI from your laptop

From your laptop terminal:

```bash
aws ssm start-session \
  --target ${INSTANCE_ID} \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["8770"],"localPortNumber":["8770"]}'
```

Leave that running. Open http://localhost:8770 in your browser. You should
get the snowtuner UI's auth screen.

Paste the API token from step 6 into Settings. The token is stored in
localStorage and attached to every subsequent request.

**Save the port-forward command as a shell alias** so you don't have to
re-type it:

```bash
# in ~/.zshrc or ~/.bashrc
alias snowtuner-up="aws ssm start-session --target ${INSTANCE_ID} \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{\"portNumber\":[\"8770\"],\"localPortNumber\":[\"8770\"]}'"
```

---

## 8. First sync

Still in your laptop terminal (separate from the port-forward), open a new
SSM session and run the sync:

```bash
aws ssm start-session --target "${INSTANCE_ID}"

# inside the session:
sudo -u snowtuner /opt/snowtuner/.venv/bin/snowtuner verify
sudo -u snowtuner /opt/snowtuner/.venv/bin/snowtuner sync
```

After 1–10 minutes (depending on your Snowflake account size), refresh the
UI — the freshness pill should turn green, warehouses populate, recommenders
fire on the next automation tick.

---

## Operating it

| Task | Command |
|------|---------|
| Logs | `journalctl -u snowtuner -f` (from inside an SSM session) |
| Restart | `sudo systemctl restart snowtuner` |
| Upgrade snowtuner | re-run `sudo bash /opt/snowtuner/deploy/bootstrap.sh` — clones latest, rebuilds, restarts |
| Rotate API token | `sudo -u snowtuner /opt/snowtuner/.venv/bin/snowtuner auth rotate`, then update Settings page |
| Rotate Snowflake creds | update the Secrets Manager secret; `sudo bash /opt/snowtuner/deploy/fetch-secrets.sh && sudo systemctl restart snowtuner` |
| Tear down | `aws ec2 terminate-instances --instance-ids ${INSTANCE_ID}` + delete volume, role, SG, secret |

---

## Cost

| Item | Monthly |
|------|---------|
| EC2 t3.small on-demand | ~$15 |
| EBS gp3 20GB | $1.60 |
| Secrets Manager (1 secret) | $0.40 |
| Data transfer (negligible) | $0 |
| **Total** | **~$17/mo** |

A 1-year reserved t3.small drops the compute portion ~30% if you commit to
keeping it running.

---

## Upgrade paths

The SSM-port-forward path is the lightest possible deploy. When you want
more, you can layer on:

- **Tailscale**: install `tailscale` on the instance + your laptops/team
  laptops; reach snowtuner at `https://snowtuner.your-tailnet.ts.net`.
  TLS handled by Tailscale, no domain needed. Free for personal use.

- **Cloudflare Tunnel**: see `deploy/install-cloudflared.sh`. Get a
  `*.trycloudflare.com` URL (quick) or a custom hostname if your domain
  is on Cloudflare. Useful when you want a public URL and don't want to
  put Tailscale on every device. Free.

- **ALB + ACM**: pure AWS, real custom domain. Add an Application Load
  Balancer, ACM cert, Route 53 record. ~$18/mo extra for the ALB. The
  fully-managed-by-AWS path.

Each is a layer on top of the same instance — you keep everything from
this guide and add the URL surface.

---

## Troubleshooting

| Symptom | Look at |
|---------|---------|
| `snowtuner` won't start | `sudo journalctl -u snowtuner -n 100` |
| Port-forward exits immediately | Instance not in SSM yet — `aws ssm describe-instance-information` should list it. Takes 1–2 min after launch. |
| UI returns 401 on every request | Token mismatch. Re-grab `sudo cat /var/lib/snowtuner/api_token` and paste into Settings. |
| Sync fails: "no Snowflake credentials" | env file missing. `sudo cat /var/lib/snowtuner/env` should list `SNOWTUNER_SNOWFLAKE_ACCOUNT=...`. Re-run `fetch-secrets.sh`. |
| Sync fails: "JWT token is invalid" | Snowflake hasn't seen your public key. From the Secrets Manager secret, extract the public half and `ALTER USER … SET RSA_PUBLIC_KEY = '…'` on Snowflake. |
| `bootstrap.sh` halts on `mkfs.ext4` | The EBS volume already had a filesystem. Either fine (script skips) or `wipefs -a /dev/nvme1n1` and re-run. |
