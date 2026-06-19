#!/usr/bin/env bash
#
# One-shot bootstrap for a fresh Amazon Linux 2023 EC2 instance.
#
# Assumes:
#   - You've SSM-sessioned (or SSH'd) into the instance as ec2-user.
#   - An EBS volume is attached at /dev/sdf (it shows up as /dev/nvme1n1
#     on Nitro instances).
#   - The instance has an IAM role with secretsmanager:GetSecretValue on
#     ${SNOWTUNER_SECRET_ID}.
#
# Run interactively (don't pipe from curl); you want to see each step:
#
#   sudo SNOWTUNER_SECRET_ID=snowtuner/snowflake \
#        SNOWTUNER_REPO_URL=https://github.com/austinkjensen/snowtuner.git \
#        SNOWTUNER_REPO_REF=main \
#        bash /opt/snowtuner/deploy/bootstrap.sh
#
# Idempotent — re-running is safe and is how you upgrade.
set -euo pipefail

readonly REPO_URL="${SNOWTUNER_REPO_URL:-https://github.com/austinkjensen/snowtuner.git}"
readonly REPO_REF="${SNOWTUNER_REPO_REF:-main}"
readonly REPO_DIR="/opt/snowtuner"
readonly DATA_DIR="/var/lib/snowtuner"
readonly SECRET_ID="${SNOWTUNER_SECRET_ID:-snowtuner/snowflake}"
readonly REGION="${AWS_REGION:-us-west-2}"

log() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }

require_root() {
    if [[ "$(id -u)" -ne 0 ]]; then
        echo "Run with sudo." >&2
        exit 1
    fi
}

# ── 0. Pre-flight: API port must be free ─────────────────────────
# Refuse to run if something foreign already holds :8770.  A previous
# snowtuner.service holding it is fine (systemctl restart reclaims it);
# anything else is a squatter we'd otherwise install over and then
# smoke-test against by mistake.  Checked first so we fail before the
# multi-minute package install + SPA build.
preflight_port() {
    local port=8770
    log "Checking API port ${port} is available"
    if ! command -v ss >/dev/null 2>&1; then
        echo "ss not available; skipping port pre-flight." >&2
        return
    fi
    if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE ":${port}$"; then
        if systemctl is-active --quiet snowtuner 2>/dev/null; then
            echo "Port ${port} is held by the running snowtuner.service — restart will reclaim it."
        else
            echo "✗ Port ${port} is already in use by a non-snowtuner process." >&2
            echo "  Identify and stop it, then re-run bootstrap:" >&2
            echo "    sudo ss -ltnp 'sport = :${port}'" >&2
            exit 1
        fi
    else
        echo "Port ${port} is free."
    fi
}

# ── 1. Prepare the data volume ───────────────────────────────────
mount_data_volume() {
    log "Preparing data volume at ${DATA_DIR}"
    mkdir -p "${DATA_DIR}"

    if mountpoint -q "${DATA_DIR}"; then
        echo "Already mounted — skipping format."
        return
    fi

    # Find a separate EBS volume.  On modern Nitro instances the OS sees them
    # as /dev/nvme1n1, /dev/nvme2n1, etc.  Pick the first that exists.
    local candidate=""
    for dev in /dev/nvme1n1 /dev/nvme2n1 /dev/xvdf /dev/sdf; do
        if [[ -b "${dev}" ]]; then
            candidate="${dev}"
            break
        fi
    done

    if [[ -z "${candidate}" ]]; then
        # No separate data volume present — the CloudFormation flow puts
        # ${DATA_DIR} on the root volume (sized via the VolumeSize template
        # parameter), and that's fine.  Just make sure the directory exists.
        echo "No separate EBS volume found; using root volume for ${DATA_DIR}."
        return
    fi

    if ! blkid -p "${candidate}" >/dev/null 2>&1; then
        echo "Formatting ${candidate} as ext4..."
        mkfs.ext4 -L snowtuner "${candidate}"
    fi

    # Persist the mount across reboots.
    local uuid
    uuid=$(blkid -s UUID -o value "${candidate}")
    if ! grep -q "${uuid}" /etc/fstab; then
        echo "UUID=${uuid} ${DATA_DIR} ext4 defaults,nofail 0 2" >> /etc/fstab
    fi
    mount "${DATA_DIR}"
    echo "Mounted ${candidate} (UUID=${uuid}) at ${DATA_DIR}"
}

# ── 2. Install system packages ───────────────────────────────────
install_system_packages() {
    log "Installing system packages"

    dnf -y update
    dnf -y install \
        git \
        jq \
        gcc \
        gcc-c++ \
        make \
        python3.11 \
        python3.11-pip \
        python3.11-devel \
        openssl-devel \
        libffi-devel

    # NodeSource for Node 22 (default repo ships Node 18).
    if ! command -v node >/dev/null || [[ "$(node -v 2>/dev/null)" != v22.* ]]; then
        curl -fsSL https://rpm.nodesource.com/setup_22.x | bash -
        dnf -y install nodejs
    fi
    echo "node $(node -v), npm $(npm -v)"

    # uv for fast Python venv + pip.
    if ! command -v uv >/dev/null; then
        curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
    fi
    echo "uv $(uv --version)"
}

# ── 3. Create the snowtuner system user ──────────────────────────
create_user() {
    log "Creating snowtuner system user"
    if ! id snowtuner >/dev/null 2>&1; then
        # Interactive shell (NOT /sbin/nologin): this is a single-tenant box
        # running snowtuner and nothing else, so the security cost of a login
        # shell is negligible, and operators need `sudo -u snowtuner -i` to
        # run `snowtuner sync` / `demo seed` / `verify` by hand.
        useradd --system --home-dir "${DATA_DIR}" --shell /bin/bash snowtuner
    else
        # Upgrade path: an older bootstrap created this user with
        # /sbin/nologin, which makes `sudo -u snowtuner -i` fail with "This
        # account is currently not available".  Repair it in place.
        usermod --shell /bin/bash snowtuner
    fi
    chown -R snowtuner:snowtuner "${DATA_DIR}"
    chmod 0750 "${DATA_DIR}"
    write_user_profile
}

# Interactive-shell profile for the snowtuner user: put the venv on PATH and
# export the Snowflake creds (written by fetch-secrets.sh as a systemd
# EnvironmentFile) so plain `snowtuner ...` works inside `sudo -u snowtuner
# -i`.  Without this an interactive shell has neither the CLI on PATH nor the
# env-var credential tier, so `snowtuner sync` fails with "no credentials".
# The env file may not exist yet at create_user time (fetch_secrets runs
# later) — the [[ -r ]] guard defers that to shell-open time.
write_user_profile() {
    local bashrc="${DATA_DIR}/.bashrc"
    cat > "${bashrc}" <<'PROFILE'
# Managed by snowtuner bootstrap.sh — regenerated on every run; edits are lost.
export PATH="/opt/snowtuner/.venv/bin:${PATH}"
if [[ -r /var/lib/snowtuner/env ]]; then
    set -a; . /var/lib/snowtuner/env; set +a
fi
PROFILE
    chown snowtuner:snowtuner "${bashrc}"
    chmod 0644 "${bashrc}"
}

# ── 4. Clone (or update) the repo ────────────────────────────────
clone_repo() {
    log "Syncing repo at ${REPO_DIR}"
    if [[ -d "${REPO_DIR}/.git" ]]; then
        cd "${REPO_DIR}"
        git fetch --depth=1 origin "${REPO_REF}"
        git reset --hard "FETCH_HEAD"
    else
        git clone --depth=1 --branch "${REPO_REF}" "${REPO_URL}" "${REPO_DIR}"
    fi
    chown -R snowtuner:snowtuner "${REPO_DIR}"
}

# ── 5. Install Python deps into a venv ───────────────────────────
install_python() {
    log "Installing snowtuner Python package"
    cd "${REPO_DIR}"
    sudo -u snowtuner uv venv .venv --python python3.11
    sudo -u snowtuner .venv/bin/python -m ensurepip --upgrade
    sudo -u snowtuner uv pip install --python .venv/bin/python -e '.[snowflake]'
    echo "snowtuner $( .venv/bin/snowtuner --version 2>/dev/null || echo 'installed' )"
}

# ── 5b. Operator CLI wrapper ─────────────────────────────────────
# /usr/local/bin/snowtuner lets you run `sudo snowtuner <cmd>` from any SSM
# session: it drops to the snowtuner service user and loads the Snowflake
# creds (the systemd EnvironmentFile) so sync / demo / verify Just Work
# without switching users or sourcing env by hand.
install_cli_wrapper() {
    log "Installing /usr/local/bin/snowtuner wrapper"
    cat > /usr/local/bin/snowtuner <<'WRAPPER'
#!/usr/bin/env bash
# Operator convenience wrapper — managed by snowtuner bootstrap.sh.
# Runs the snowtuner CLI as the 'snowtuner' service user with the Snowflake
# credentials loaded.  Usage:  sudo snowtuner <command> [args...]
set -euo pipefail
ENV_FILE=/var/lib/snowtuner/env
SNOWTUNER_BIN=/opt/snowtuner/.venv/bin/snowtuner

if [[ "$(id -un)" == "snowtuner" ]]; then
    if [[ -r "${ENV_FILE}" ]]; then set -a; . "${ENV_FILE}"; set +a; fi
    exec "${SNOWTUNER_BIN}" "$@"
fi

# Not the service user yet — re-exec self as snowtuner (password-less from
# root or a sudo-capable operator); the branch above then loads creds.
exec sudo -u snowtuner -- "$0" "$@"
WRAPPER
    chmod 0755 /usr/local/bin/snowtuner
}

# ── 6. Build the SPA ────────────────────────────────────────────
build_spa() {
    log "Building web/dist"
    cd "${REPO_DIR}/web"
    sudo -u snowtuner npm ci
    sudo -u snowtuner npm run build
    echo "Built $(find dist -type f | wc -l) static files."
}

# ── 7. Fetch secrets and write env file ──────────────────────────
fetch_secrets() {
    log "Fetching secrets from ${SECRET_ID}"
    chmod +x "${REPO_DIR}/deploy/fetch-secrets.sh"
    SNOWTUNER_SECRET_ID="${SECRET_ID}" \
    AWS_REGION="${REGION}" \
    SNOWTUNER_DATA_DIR="${DATA_DIR}" \
        bash "${REPO_DIR}/deploy/fetch-secrets.sh"
}

# ── 8. Install + start the systemd unit ──────────────────────────
install_service() {
    log "Installing systemd unit"
    install -m 0644 "${REPO_DIR}/deploy/snowtuner.service" /etc/systemd/system/snowtuner.service
    systemctl daemon-reload
    systemctl enable snowtuner
    systemctl restart snowtuner
    sleep 2
    systemctl status snowtuner --no-pager --lines=10 || true
}

# ── 9. Smoke test ────────────────────────────────────────────────
smoke_test() {
    log "Smoke test"
    sleep 3
    if curl --silent --show-error --max-time 5 http://127.0.0.1:8770/health; then
        echo
        echo "✓ snowtuner API responding on :8770"
    else
        echo
        echo "✗ snowtuner API not responding — check 'journalctl -u snowtuner -n 50'" >&2
        exit 1
    fi
}

main() {
    require_root
    preflight_port
    mount_data_volume
    install_system_packages
    create_user
    clone_repo
    install_python
    install_cli_wrapper
    build_spa
    fetch_secrets
    install_service
    smoke_test

    log "Done"
    echo "Run snowtuner CLI commands on the box with:  sudo snowtuner <cmd>"
    echo "  e.g.  sudo snowtuner verify   /   sudo snowtuner demo seed"
    echo
    echo "Next: from your laptop,"
    echo "  aws ssm start-session --target <instance-id> \\"
    echo "    --document-name AWS-StartPortForwardingSession \\"
    echo "    --parameters '{\"portNumber\":[\"8770\"],\"localPortNumber\":[\"8770\"]}'"
    echo "  open http://localhost:8770"
}

main "$@"
