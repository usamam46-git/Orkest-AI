# Deploying to AWS EC2

This is the **prelude** to [`DEPLOY.md`](DEPLOY.md), not a replacement for it.
It gets you from an empty AWS account to a box that satisfies `DEPLOY.md` §0,
then hands off at step 1. Nothing in `DEPLOY.md` is repeated here, and nothing
in it changes except the `dc` alias.

---

## 0. What this is

Vol. 6 §4's deployment model is a single Linux VPS running the full Compose
stack. EC2 *is* that VPS — no managed services, no rearchitecting.

What is different is size. Vol. 6 §4 specifies 8 vCPU / 32 GB; this targets a
**`c7i-flex.large` (2 vCPU x86, 4 GB)**, because this deployment is for
learning and portfolio use and is funded by AWS credits. (It was planned as a
`t4g.medium`, but a free-plan account refuses that type at launch — see §1. The
`c7i-flex` family is the cheapest 4 GB type such an account may use.) That is a deliberate
deviation, and it lives in exactly one file:
[`docker-compose.aws.yml`](docker-compose.aws.yml), a sizing override layered
on top of `docker-compose.prod.yml`. Its header says what it changes and — more
importantly — the two things it deliberately does *not* change (the three
worker containers stay three containers; Postgres `max_connections` goes *up*).

**Object storage is real S3, not MinIO.** MinIO stopped publishing container
images (Docker Hub and quay both refuse the pull), so the production stack has
no MinIO service at all. `core/storage.py` selects S3 when `MINIO_ENDPOINT` and
both keys are empty and lets boto3 use the instance's IAM role — there is no
storage secret on the box. Local development is unchanged and still runs MinIO.

**Architecture.** The stack is multi-arch, so x86 (this box) and Graviton both
work; `aws-provision.sh` takes `INSTANCE_TYPE` and `UBUNTU_ARCH` to switch.

**Fast path.** `infra/aws-provision.sh` performs §1–§3 and the AWS half of §10 in
one idempotent run (budget, key pair, security group, both buckets, IAM role,
instance, Elastic IP): `ALERT_EMAIL=you@example.com ./aws-provision.sh`. The
sections below are what it does, and the manual console route if you prefer it.
The A record in §4 is still yours to create — it needs the printed Elastic IP.

## 1. Cost — set this up before launching anything

| item | per month (us-east-1) |
|---|---|
| `c7i-flex.large` on-demand, 730 h | ~$62 (verify against current pricing) |
| 40 GB gp3 EBS | ~$3.20 |
| public IPv4 address (Elastic IP) | ~$3.65 |
| **running** | **~$69** → roughly 3 months of $200 |
| **stopped** (EBS + IPv4 still bill) | ~$6.85 |

`t4g.medium` (~$24.53) would be ~$31/month running, but is not available on the
free plan.

Data transfer out is negligible at demo traffic. **OpenAI spend is not covered
by AWS credits** — it is the budget model in `Docs/15-day-build-plan.md`, and
it is separate.

**First action in the account: create a budget.** Billing and Cost Management →
Budgets → *Create budget* → Cost budget, monthly, **$75**:

- alert at **80% of actual** and **100% of forecasted**, to your email;
- under *Advanced options*, make sure **credits are not netted out** of the
  spend being tracked. If credits are included, they cancel the charges, the
  tracked spend stays at $0, and the alert that exists to protect the credits
  never fires.

A budget does not stop anything. It emails you — which is enough, as long as it
exists before the mistake does.

**Credits expire.** On AWS's current free plan they last a fixed window (around
six months) or until spent, whichever comes first; check *Billing → Credits* for
your actual expiry date. Two consequences:

- Stopping the instance to "stretch" the credits can save credits that expire
  unused. At ~$31/mo the budget and the window line up — leaving it running is
  the reasonable default.
- Free-plan accounts are restricted to instance types flagged free-tier
  eligible. `t4g.medium` is refused with `InvalidParameterCombination … not
  eligible for Free Tier`; list what is allowed with
  `aws ec2 describe-instance-types --filters Name=free-tier-eligible,Values=true`.

**What actually burns credits unexpectedly is rarely the instance.** It's the
thing launched to try something and then forgotten: a NAT Gateway (~$32/mo
idle), a second EBS volume left after terminating an instance, an unassociated
Elastic IP. None of this deployment needs any of them.

## 2. Launch the instance

EC2 → *Launch instance*:

| setting | value |
|---|---|
| Name | `orkest` |
| AMI | **Ubuntu Server 24.04 LTS**, architecture **64-bit (x86)** |
| Instance type | **`c7i-flex.large`** |
| Key pair | create one (ed25519); keep the `.pem` — it is the only way in |
| Storage | **40 GiB gp3** (the default 8 GiB fills on the first build) |
| Security group | new, rules below |
| Advanced → User data | the full contents of [`ec2-user-data.sh`](ec2-user-data.sh) |

Security group, **inbound**:

| port | source | why |
|---|---|---|
| 22 | **My IP** | SSH. Not `0.0.0.0/0`. If your home IP changes and you're locked out, edit this rule — or use EC2 Instance Connect from the console. |
| 80 | `0.0.0.0/0` | Let's Encrypt HTTP-01 challenge, and the redirect to HTTPS |
| 443 | `0.0.0.0/0` | the application |

Nothing else. Postgres and Redis publish no ports in
`docker-compose.prod.yml`, and **the security group is the only thing that keeps
that true if someone publishes one later.** `ec2-user-data.sh` enables `ufw` with
the same three ports, but ufw does not protect container ports: Docker writes its
iptables rules ahead of ufw's, so a port published by Compose is open to the
internet whatever `ufw status` reports. ufw guards host-level listeners; the
security group guards everything.

### CPU credits

`c7i-flex` is not a burstable family, so there is no CPU-credit mode to manage.
(On a `t4g`/`t3` box the default Unlimited mode bills sustained overage; switch it
to Standard once the first build is done.)

## 3. Elastic IP

EC2 → Elastic IPs → *Allocate* → *Associate* with the instance.

Since February 2024 AWS bills every public IPv4 address, so an Elastic IP costs
the same as the auto-assigned one. The difference is that an auto-assigned address
**changes on every stop/start**, which silently breaks the DNS record, which
breaks certificate renewal. The Elastic IP keeps the address across stop/start.

If you later terminate the instance, **release** the Elastic IP too — an
unassociated one keeps billing.

## 4. DNS

At your domain registrar, create an **A record** for the hostname you'll use
(e.g. `demo.yourdomain.xyz`) pointing at the Elastic IP. No Route 53 hosted zone
is needed — that would be $0.50/mo to do what the registrar's DNS already does.

Do not continue until it resolves **from your own machine**:

```sh
dig +short demo.yourdomain.xyz     # must print the Elastic IP
```

`DEPLOY.md` §0 requires this, and §5 there (certificate issuance) fails if DNS
is not live yet — Let's Encrypt validates from the public internet.

## 5. First SSH — verify the bootstrap, then clone

```sh
chmod 400 orkest.pem
ssh -i orkest.pem ubuntu@<elastic-ip>
```

User data takes a few minutes after the instance shows *running*. Verify it
finished rather than assuming:

```sh
test -f /var/lib/cloud/bootstrap-complete && echo done || sudo tail -n 40 /var/log/user-data.log
docker compose version      # Compose v2 plugin present
free -h                     # Swap: 4.0Gi
sudo ufw status             # 22, 80, 443 ALLOW; nothing else
docker run --rm hello-world # no sudo — confirms docker group membership
```

If `docker` needs `sudo`, log out and back in: the group was added during boot,
before your session existed.

**Clone with a read-only deploy key**, generated on the instance (never in user
data — see the header of `ec2-user-data.sh` for why):

```sh
ssh-keygen -t ed25519 -f ~/.ssh/orkest_deploy -N "" -C "orkest-ec2"
cat ~/.ssh/orkest_deploy.pub
```

GitHub → the repository → Settings → Deploy keys → *Add deploy key* → paste it,
leave **Allow write access unchecked**. Then:

```sh
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/orkest_deploy
  IdentitiesOnly yes
EOF

git clone git@github.com:usamam46-git/AI-Automation.git
cd AI-Automation/infra
```

A deploy key reaches this one repository, read-only, and is revoked from the
repository settings without touching your account.

## 6. Hand-off to `DEPLOY.md`

**Everything from here is [`DEPLOY.md`](DEPLOY.md), starting at step 1,
unchanged** — with one substitution. Wherever it defines or uses `dc`, use
this instead:

```sh
alias dc='docker compose -f docker-compose.prod.yml -f docker-compose.aws.yml --env-file .env.prod'
```

Add it to `~/.bashrc` so every future session has it. Like every command in
`DEPLOY.md`, it uses relative paths and only works from the `infra/` directory. Forgetting the second
`-f` does not fail — it quietly deploys the 16-worker-process production
sizing onto a 4 GB box, and the first symptom is a container being OOM-killed
an hour later.

Confirm it's in effect before building:

```sh
dc config --format json | python3 -c 'import json,sys; s=json.load(sys.stdin)["services"]; [print(n, "-c", s[n]["command"][s[n]["command"].index("-c")+1]) for n in sorted(s) if n.startswith("worker_")]'
```

All three workers must print `-c 1`. (A plain `grep` over `dc config` does not
work: Compose renders `command` as a YAML list, so `-c` and `"1"` land on
separate lines.) Without the override applied this prints `-c 4`, `-c 8`, `-c 4`.

**One change to how you do step 4.** `DEPLOY.md` §4 runs `dc up -d --build`,
which builds every image in parallel while starting services. On this box, build
the web image first, on its own, then the rest, then start:

```sh
dc build web      # npm ci + next build — the largest memory spike in the deployment
dc build          # the api image; migrate/api/workers/beat all share its layers
dc up -d
```

Watch `free -m` in a second SSH session during `dc build web`. Using a GiB or
two of swap is expected and is why the swapfile exists; a build killed with
exit 137 means the swapfile didn't come up — go back to §5.

Then continue with `DEPLOY.md` §4's `dc logs migrate` and onward, through §6's
seven verification checks. They all apply unchanged.

## 7. Running on 4 GB

```sh
free -m                                   # swap in steady state should be near zero
docker stats --no-stream                  # per-container usage vs its mem_limit
df -h /                                   # 40 GB goes faster than expected
```

**Disk.** Every rebuild leaves old image layers and build cache behind. After a
deploy has come up healthy, **with the stack running** (so the images in use
are protected):

```sh
docker image prune -af && docker builder prune -af
```

`docker system prune -af` with the stack *down* deletes every image, and the
next `dc up` rebuilds from nothing.

**A container that keeps restarting.** Check whether its limit killed it:

```sh
docker inspect -f '{{.Name}} OOMKilled={{.State.OOMKilled}}' $(dc ps -aq)
sudo dmesg -T | grep -i 'killed process'
```

The fix is in `docker-compose.aws.yml`, not in the prod file. The likely
candidate is `worker_documents` on a large PDF — its comment says what to try
first. Raising a `mem_limit` is fine; the limits are caps, not a budget that must
add up to 4 GB.

## 8. Stopping and starting

**Stop** ≠ **terminate**. Stopping keeps the EBS volume (the database and
the certificates; uploaded documents are in S3, not on the volume) and the Elastic IP; it stops compute billing and
leaves ~$6.85/mo. **Terminate deletes the root volume — the database with it.**

After a start, every service has `restart: unless-stopped`, so Docker brings the
stack back on its own. Confirm rather than assume:

```sh
dc ps     # all healthy; migrate shows Exited (0), which is correct
```

## 9. Not set up here

Listed so the next reader knows these were considered, not missed:

- **Backups** are set up in §10 below.
- **S3 instead of MinIO** is done — see §0.
- **RDS / ElastiCache.** ~$25/mo more between them, which roughly halves the
  credit runway, and it gives up the self-contained pgvector-in-Compose story.
  Not worth it at this scale.
- **CI/CD** (Vol. 6 §3). Deploy is `git pull && dc build && dc up -d`.
- **ALB, autoscaling, multiple AZs.** One instance is the design (Vol. 6 §4:
  vertical first), not a shortcut.
- **Scheduled stop/start.** See §1 on why stopping may not save anything that
  matters.

## 10. Backups

`backup.sh` dumps Postgres, verifies the gzip and a minimum size, uploads to S3,
and exits non-zero on any failure. It needs the AWS CLI (installed by
`ec2-user-data.sh`; on an instance launched before that change run the three
`curl`/`unzip`/`install` lines from its §2b by hand).

**S3 bucket.** Private, default encryption on, public access blocked, and a
lifecycle rule expiring objects after 30 days, otherwise nightly dumps grow
forever. `aws-provision.sh` creates this and the documents bucket.

**IAM role.** The instance role (created by `aws-provision.sh`) can only put
objects under `db/` in the backup bucket, plus list/get/put/delete on the
documents bucket. For the backup bucket that is:

```json
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"s3:PutObject",
 "Resource":"arn:aws:s3:::YOUR-BUCKET/db/*"}]}
```

Attach it: EC2 → instance → Actions → Security → *Modify IAM role*. Put-only is
deliberate: a compromised box can add backups but not read or delete them.

**Configure.** In `.env.prod` set `BACKUP_S3_URI=s3://YOUR-BUCKET/db/`. Optionally
create a check at healthchecks.io and set `BACKUP_PING_URL` — cron fails
silently, and this is what turns "the backup stopped" into an email.

**Test once by hand, then schedule:**

```sh
./backup.sh        # then confirm the object in the S3 console (the put-only role cannot list)
echo '17 3 * * * ubuntu /home/ubuntu/AI-Automation/infra/backup.sh >> /var/log/orkest-backup.log 2>&1' \
  | sudo tee /etc/cron.d/orkest-backup
sudo touch /var/log/orkest-backup.log && sudo chown ubuntu /var/log/orkest-backup.log
```

**Restore** into an empty database (test this once before you need it):

```sh
gunzip -c orkest-aap_db-<stamp>.sql.gz | dc exec -T postgres psql -U "$POSTGRES_USER" "$POSTGRES_DB"
```

**Store `INTEGRATION_ENCRYPTION_KEY` outside this bucket** (a password manager).
The dump holds credentials encrypted under it; without the key a restore brings
back rows nobody can decrypt. Uploaded documents live in the documents S3 bucket
and are not in the dump; their chunks are. That bucket has no lifecycle rule and
no versioning — enable versioning if accidental deletion matters.

`backup.sh` refuses dumps under 5,000 bytes (`MIN_BYTES` overrides). A fresh
migrated database dumps to ~8 KB; a failed dump is under 1 KB.
