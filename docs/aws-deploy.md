# Deploying snowtuner on AWS

One CloudFormation stack + one Secrets Manager secret = snowtuner running
in your AWS account, reachable from your laptop via SSM port-forward.
Total cost: **~$17/month**. End-to-end deploy: **~10 minutes**.

If you've never deployed anything to AWS before and are evaluating
snowtuner, **run it on your laptop first**. The local dev path (`snowtuner
api` + `cd web && npm run dev`) needs zero infrastructure. See the
[main README](../README.md). Come back here when you want it to keep
running while your laptop is asleep, or when you want a team to share it.

## What gets provisioned

```
┌─────────────────────┐
│  your laptop        │
│                     │
│  aws ssm start-...  │ ──── SSM port-forward over outbound HTTPS ────┐
│  http://localhost:  │                                                │
│         8770        │                                                │
└─────────────────────┘                                                ▼
                                                       ┌────────────────────────┐
                                                       │  EC2 t3.small          │
                                                       │  (us-west-2, your VPC) │
                                                       │                        │
                                                       │  snowtuner :8770       │
                                                       │  (loopback only)       │
                                                       │  + cloned source       │
                                                       │  + DuckDB              │
                                                       │  + 30GB gp3 root vol   │
                                                       └────────────┬───────────┘
                                                                    │
                                  (EC2 initiates; stateful SG       │
                                   permits the response packets     │
                                   on the established connection)   ▼
                                                       ┌──────────────────────┐
                                                       │  your Snowflake      │
                                                       │  account             │
                                                       └──────────────────────┘
```

**No inbound ports.** The security group has zero ingress rules. Everything
the box does — talking to Snowflake, fetching the secret, accepting the
SSM port-forward — happens over connections **the EC2 instance opens**.
AWS security groups are stateful, so response packets on those established
connections flow back automatically. Nobody from the public internet can
initiate a connection to the box.

**Why SSM port-forward instead of a public URL?** Simplest possible deploy
that avoids real-world problems (cert renewals, leaked URLs, abuse) for
the case where one person or a small team is using snowtuner. When you
outgrow it, see [Upgrade paths](#upgrade-paths).

## Prerequisites

- AWS account with permission to create IAM roles, EC2, EBS, Secrets Manager, and CloudFormation stacks
- AWS CLI installed: `brew install awscli`
- AWS CLI configured: `aws configure` (region `us-west-2` is what the rest of this doc assumes)
- SSM plugin: `brew install --cask session-manager-plugin`
- Your Snowflake service-user **private key** (a `.p8` file — `~/.snowtuner/snowflake_rsa_key.p8` if you've been running snowtuner locally)
- Your Snowflake account locator, service-user name, default warehouse, role

Confirm AWS is wired up:
```bash
aws sts get-caller-identity
```

---

## 1. Push the Snowflake credentials to Secrets Manager

This step stays a CLI command (not part of the CloudFormation stack)
because the RSA PEM is multi-line, and CloudFormation's console form
fields are single-line. One command on your laptop:

```bash
# Fill these in:
SNOWFLAKE_ACCOUNT="xy12345.us-west-2"
SNOWFLAKE_USER="SNOWTUNER_SVC"
SNOWFLAKE_WAREHOUSE="COMPUTE_WH"
SNOWFLAKE_ROLE="SNOWTUNER_ROLE"
PRIVATE_KEY_PATH="$HOME/.snowtuner/snowflake_rsa_key.p8"

# Build the JSON + push it in one shot.  --rawfile preserves the PEM newlines
# as \n inside the JSON string.
SECRET_ARN=$(
  jq -n \
    --arg account   "$SNOWFLAKE_ACCOUNT"   \
    --arg user      "$SNOWFLAKE_USER"      \
    --arg warehouse "$SNOWFLAKE_WAREHOUSE" \
    --arg role      "$SNOWFLAKE_ROLE"      \
    --rawfile pem   "$PRIVATE_KEY_PATH"    \
    '{account: $account, user: $user, warehouse: $warehouse, role: $role, private_key_pem: $pem}' \
  | aws secretsmanager create-secret \
      --name snowtuner/snowflake \
      --description "Snowflake service-user creds for snowtuner" \
      --secret-string file:///dev/stdin \
      --region us-west-2 \
      --query ARN --output text
)
echo "Secret ARN: ${SECRET_ARN}"
```

That ARN is what the CloudFormation stack reads. Save it; you'll paste it
in step 2.

---

## 2. Launch the CloudFormation stack

The big-button way (assumes the template is at `main` in the public repo):

[![Launch Stack](https://s3.amazonaws.com/cloudformation-examples/cloudformation-launch-stack.png)](https://console.aws.amazon.com/cloudformation/home?region=us-west-2#/stacks/quickcreate?templateURL=https://raw.githubusercontent.com/austinkjensen/snowtuner/main/deploy/snowtuner.cf.yaml&stackName=snowtuner)

Click it. The AWS console opens with the template pre-loaded. Fill in:

| Field | What to enter |
|-------|---------------|
| **Stack name** | `snowtuner` (already prefilled) |
| **Snowflake credentials secret ARN** | Paste the ARN from step 1 |
| **EC2 instance type** | Leave as `t3.small` |
| **Root volume size (GB)** | Leave as `30` |
| **VPC** | Pick your default VPC (the only one shown unless you've created others) |
| **Subnet** | Pick any subnet in that VPC |
| **snowtuner repo URL** | Leave as default unless you forked |
| **Branch / tag / commit** | Leave as `main` (or pin to a tag for stable deploys) |

Click "Create stack." Wait 5–10 minutes — the stack waits for snowtuner to
finish bootstrapping before marking CREATE_COMPLETE, so when the green check
appears you know it's actually up.

### CLI alternative

If you'd rather drive from the CLI than the console:

```bash
# Look up your default VPC + a subnet
VPC_ID=$(aws ec2 describe-vpcs \
  --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].VpcId' --output text)
SUBNET_ID=$(aws ec2 describe-subnets \
  --filters "Name=vpc-id,Values=${VPC_ID}" \
  --query 'Subnets[0].SubnetId' --output text)

# Create the stack
aws cloudformation create-stack \
  --stack-name snowtuner \
  --template-url https://raw.githubusercontent.com/austinkjensen/snowtuner/main/deploy/snowtuner.cf.yaml \
  --capabilities CAPABILITY_IAM \
  --parameters \
    ParameterKey=SnowflakeSecretArn,ParameterValue="${SECRET_ARN}" \
    ParameterKey=VpcId,ParameterValue="${VPC_ID}" \
    ParameterKey=SubnetId,ParameterValue="${SUBNET_ID}" \
  --region us-west-2

# Wait for it to finish (5-10 min)
aws cloudformation wait stack-create-complete \
  --stack-name snowtuner --region us-west-2
```

If the stack fails, before deleting it run:
```bash
aws cloudformation describe-stack-events --stack-name snowtuner \
  --region us-west-2 --max-items 20
```
…and grab the userdata log with SSM:
```bash
INSTANCE_ID=$(aws cloudformation describe-stack-resource \
  --stack-name snowtuner \
  --logical-resource-id EC2Instance \
  --query 'StackResourceDetail.PhysicalResourceId' --output text \
  --region us-west-2)
aws ssm start-session --target "${INSTANCE_ID}"
# inside the session:
sudo cat /var/log/snowtuner-userdata.log | tail -100
```

---

## 3. Reach the UI

Pull the SSM port-forward command from the stack outputs:

```bash
aws cloudformation describe-stacks \
  --stack-name snowtuner --region us-west-2 \
  --query 'Stacks[0].Outputs[?OutputKey==`PortForwardCommand`].OutputValue' \
  --output text
```

That prints something like:
```
aws ssm start-session --target i-0abc...  --document-name AWS-StartPortForwardingSession --parameters '{"portNumber":["8770"],"localPortNumber":["8770"]}' --region us-west-2
```

Run it. Leave it running. Open <http://localhost:8770> in your browser.

You'll get the snowtuner auth screen — paste in your API token.

To grab the token, open another terminal and SSM-session in:
```bash
INSTANCE_ID=$(aws cloudformation describe-stacks \
  --stack-name snowtuner --region us-west-2 \
  --query 'Stacks[0].Outputs[?OutputKey==`InstanceId`].OutputValue' \
  --output text)
aws ssm start-session --target "${INSTANCE_ID}"
# inside:
sudo cat /var/lib/snowtuner/api_token
```

Paste it into the UI's Settings page. It's stored in localStorage and
attached to every subsequent request.

### Save the port-forward as an alias

```bash
# in ~/.zshrc
alias snowtuner-up='aws ssm start-session --target i-0abc...  --document-name AWS-StartPortForwardingSession --parameters "{\"portNumber\":[\"8770\"],\"localPortNumber\":[\"8770\"]}" --region us-west-2'
```

---

## 4. First sync

Still in your SSM shell session on the instance:

```bash
sudo -u snowtuner /opt/snowtuner/.venv/bin/snowtuner verify
sudo -u snowtuner /opt/snowtuner/.venv/bin/snowtuner sync
```

After 1–10 minutes (depending on your Snowflake account size), refresh the
UI — the freshness pill turns green, warehouses populate, recommenders fire
on the next automation tick (default 1 hour after boot).

---

## Operating it

| Task | Command (from inside an SSM session) |
|------|---------|
| Tail logs | `journalctl -u snowtuner -f` |
| Restart | `sudo systemctl restart snowtuner` |
| Upgrade snowtuner | re-run `sudo bash /opt/snowtuner/deploy/bootstrap.sh` — pulls latest, rebuilds, restarts |
| Rotate API token | `sudo -u snowtuner /opt/snowtuner/.venv/bin/snowtuner auth rotate`, then update Settings page |
| Rotate Snowflake creds | update the secret value in Secrets Manager, then `sudo bash /opt/snowtuner/deploy/fetch-secrets.sh && sudo systemctl restart snowtuner` |

## Tearing it down

```bash
aws cloudformation delete-stack --stack-name snowtuner --region us-west-2
```

The CF stack deletes the EC2 instance, role, instance profile, SG.
**The Snowflake secret survives** — it's intentionally outside the stack
because credentials should outlive infrastructure. Delete it separately
when you're sure you're done:

```bash
aws secretsmanager delete-secret \
  --secret-id snowtuner/snowflake \
  --recovery-window-in-days 7 \
  --region us-west-2
```

(Use `--force-delete-without-recovery` to skip the 7-day grace if you're
really sure.)

---

## Cost

| Item | Monthly |
|------|---------|
| EC2 t3.small on-demand | ~$15 |
| EBS gp3 30GB (root) | $2.40 |
| Secrets Manager (1 secret) | $0.40 |
| Data transfer (negligible) | $0 |
| **Total** | **~$18/mo** |

A 1-year reserved t3.small drops the compute portion ~30%.

---

## Upgrade paths

The SSM-port-forward path is the lightest deploy. When you want more, you
layer one of these onto the same instance:

- **Tailscale**: install `tailscale` on the instance + your laptops; reach
  snowtuner at `https://snowtuner.your-tailnet.ts.net`. TLS handled by
  Tailscale, no domain needed, free for personal use. Good for small teams.

- **Cloudflare Tunnel**: `dnf install cloudflared`, `cloudflared tunnel
  login`, and a `cloudflared.yml` with `ingress: snowtuner.example.com →
  http://127.0.0.1:8770`. Get a `*.trycloudflare.com` URL (quick) or a
  custom hostname if your domain's on Cloudflare. Useful when you want a
  public URL without putting Tailscale on every device.

- **ALB + ACM**: pure AWS, real custom domain. Add an Application Load
  Balancer, ACM cert, Route 53 record. Need to open port 8770 from the
  ALB's SG to snowtuner's SG. ~$18/mo extra for the ALB. The
  fully-managed-by-AWS path.

Each is additive — you keep everything from this guide and add the URL
surface.

---

## Troubleshooting

| Symptom | What to look at |
|---------|-----------------|
| Stack creation stuck > 15 min | `aws cloudformation describe-stack-events --stack-name snowtuner` — usually a user-data crash. SSM in and grep `/var/log/snowtuner-userdata.log`. |
| Stack creation failed; can't SSM in | The instance may have already been terminated by rollback. Re-launch with `--on-failure DO_NOTHING` so the instance survives for inspection. |
| Port-forward exits immediately | Instance not registered with SSM yet — usually clears in 1-2 min after launch. `aws ssm describe-instance-information` should list it. |
| UI returns 401 on every request | Token mismatch. Re-grab `sudo cat /var/lib/snowtuner/api_token` and paste into Settings. |
| `snowtuner verify` fails: "no Snowflake credentials" | env file missing. `sudo cat /var/lib/snowtuner/env` should list `SNOWTUNER_SNOWFLAKE_ACCOUNT=...`. Re-run `sudo bash /opt/snowtuner/deploy/fetch-secrets.sh`. |
| `snowtuner sync` fails: "JWT token is invalid" | Snowflake hasn't seen your public key yet. Extract the public half from your local `.p8` and `ALTER USER ... SET RSA_PUBLIC_KEY = '...'` in Snowflake. |
