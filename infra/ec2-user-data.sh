#!/bin/bash
# infra/ec2-user-data.sh — first-boot bootstrap for the AWS EC2 host.
#
# Paste the whole file into the launch wizard's "Advanced details → User data"
# field. It runs once, as root, on first boot. See infra/DEPLOY-AWS.md §2.
#
# Targets Ubuntu Server 24.04 LTS (arm64) on a t4g.medium. Ubuntu rather than
# Amazon Linux because infra/DEPLOY.md §0 is already written against ufw and
# apt, and one set of instructions is better than two.
#
# ─── WHAT IT DOES NOT DO, ON PURPOSE ─────────────────────────────────────────
#
# It does not clone the repository and does not start the stack. Cloning a
# private repository needs a credential, and user-data is readable by anything
# on the instance that can reach the instance metadata service
# (curl http://169.254.169.254/latest/user-data). A deploy key pasted here is a
# deploy key published to every process on the box, forever — the field
# survives reboots and is visible in the console. Create the credential on the
# instance, after first SSH (DEPLOY-AWS.md §5).
#
# Likewise no .env.prod values. Nothing secret belongs in this file.
#
# Progress:  sudo tail -f /var/log/user-data.log
# Finished:  test -f /var/lib/cloud/bootstrap-complete

set -euxo pipefail
exec > >(tee -a /var/log/user-data.log) 2>&1

export DEBIAN_FRONTEND=noninteractive

# ─── 1. Base packages ────────────────────────────────────────────────────────
# gettext-base is NOT optional. It provides `envsubst`, which DEPLOY.md step 2
# needs to render nginx/orkest.conf.template. Ubuntu server images do not ship
# it, and discovering that at step 2 means discovering it with a half-configured
# box.
apt-get update
apt-get install -y ca-certificates curl git gnupg ufw gettext-base unattended-upgrades unzip

# ─── 2. Docker Engine + Compose v2, from Docker's own repository ─────────────
# Not Ubuntu's `docker.io`: that package tracks its own release cadence and
# does not reliably carry the Compose v2 plugin, and every command in
# DEPLOY.md is `docker compose` (plugin), never `docker-compose` (v1 binary).
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  # shellcheck disable=SC1091 # /etc/os-release exists on every Ubuntu image
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker

# The default login user on Ubuntu AMIs. Takes effect at the next login, which
# is the first SSH — user-data finishes before anyone can connect.
usermod -aG docker ubuntu

# ─── 2b. AWS CLI v2 ──────────────────────────────────────────────────────────
# For infra/backup.sh (nightly pg_dump to S3). Not in Ubuntu's apt archive for
# 24.04, so from AWS's own installer, picking the build that matches the CPU (x86_64 or aarch64).
# Credentials come from the instance's IAM role; nothing is configured here.
if ! command -v aws >/dev/null 2>&1; then
  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscliv2.zip
  unzip -q /tmp/awscliv2.zip -d /tmp
  /tmp/aws/install
  rm -rf /tmp/aws /tmp/awscliv2.zip
fi

# ─── 3. Swap ─────────────────────────────────────────────────────────────────
# 4 GB of swap on a 4 GB box. The steady-state stack fits in RAM with
# docker-compose.aws.yml applied; this exists for ONE step — `npm ci` followed
# by `next build` inside the web image, which is the largest memory spike in the
# whole deployment and gets OOM-killed partway through without it.
#
# swappiness=10 keeps swap as a spill-over for that spike rather than somewhere
# the kernel proactively pages Postgres to.
if ! swapon --show | grep -q '^/swapfile'; then
  fallocate -l 4G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
fi
grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab

cat > /etc/sysctl.d/99-orkest.conf <<'EOF'
vm.swappiness=10
vm.vfs_cache_pressure=50
EOF
sysctl --system

# ─── 4. Host firewall ────────────────────────────────────────────────────────
# Mirrors DEPLOY.md §0. It covers HOST-level listeners only.
#
# It does NOT cover container ports. Docker inserts its own iptables rules
# (the DOCKER / DOCKER-USER chains) ahead of ufw's, so a port published by
# docker-compose is reachable from the internet regardless of what `ufw status`
# says. The layer that stops a Postgres port someone publishes "just for a
# minute" is the EC2 security group (DEPLOY-AWS.md §2), and only that.
#
# `--force` because `ufw enable` otherwise prompts, and there is no terminal.
# Port 22 is allowed BEFORE enabling — the reverse order locks out SSH.
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# ─── 5. Security updates ─────────────────────────────────────────────────────
# Unattended security patches for the host OS only. Container images are not
# touched by this — they update when you rebuild (DEPLOY.md §7).
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

# ─── 6. Tell the first SSH session what comes next ───────────────────────────
cat > /etc/motd <<'EOF'

  Orkest host — bootstrapped by infra/ec2-user-data.sh

  Next: infra/DEPLOY-AWS.md §5 (verify this bootstrap, clone the repo),
        then infra/DEPLOY.md from step 1 with the AWS `dc` alias:

    alias dc='docker compose -f docker-compose.prod.yml -f docker-compose.aws.yml --env-file .env.prod'

EOF

touch /var/lib/cloud/bootstrap-complete
echo "bootstrap complete"
