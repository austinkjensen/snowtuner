#!/usr/bin/env bash
#
# Install Cloudflare Tunnel and bring up a public URL for snowtuner.
#
# This is a guide-style script, NOT a fully-automated one — `cloudflared
# tunnel login` requires you to authenticate in a browser, so we walk
# through it interactively.
#
# Prerequisites:
#   - You've already signed up for Cloudflare (https://dash.cloudflare.com/sign-up)
#   - snowtuner is running locally on 127.0.0.1:8770 (`systemctl is-active snowtuner`)
#
# Run as the ec2-user (sudo where indicated):
#
#   bash /opt/snowtuner/deploy/install-cloudflared.sh
set -euo pipefail

log() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
pause() {
    printf "\n\033[1;33m▸ %s\033[0m\n" "$*"
    read -r -p "Press ENTER when ready..." _
}

# ── 1. Install cloudflared ───────────────────────────────────────
if ! command -v cloudflared >/dev/null; then
    log "Installing cloudflared"
    curl -fsSL https://pkg.cloudflare.com/cloudflared-ascii.repo \
        | sudo tee /etc/yum.repos.d/cloudflared.repo
    sudo dnf -y install cloudflared
fi
cloudflared --version

# ── 2. Decide: trycloudflare (quick) or named tunnel (durable URL) ──
log "Choose your tunnel mode"
cat <<'EOF'

  [1] Quick try-it tunnel (trycloudflare.com)
      - Random URL like https://something-random.trycloudflare.com
      - No Cloudflare account needed
      - URL changes every time cloudflared restarts
      - Good for "does the deploy work?" smoke testing

  [2] Named tunnel (stable URL)
      - Authenticated against your Cloudflare account
      - Stable URL on a domain you own (e.g. snowtuner.example.com)
      - Survives restarts
      - Requires a domain hosted on Cloudflare DNS

EOF
read -r -p "Choice [1/2]: " choice

case "${choice}" in
    1)
        log "Starting a quick tunnel"
        echo "This runs in the foreground. Copy the trycloudflare URL it prints."
        echo "Stop with Ctrl-C when you're done testing."
        echo
        echo "Note: the URL will be different each time you run this."
        echo
        cloudflared tunnel --url http://127.0.0.1:8770
        ;;
    2)
        log "Authenticating with Cloudflare"
        echo "A login URL will be printed below. Open it in your laptop browser"
        echo "(NOT in the EC2 instance — copy the URL out) and approve."
        echo
        cloudflared tunnel login
        pause "Once browser auth succeeded, continuing..."

        log "Creating named tunnel 'snowtuner'"
        if cloudflared tunnel list | grep -q '\bsnowtuner\b'; then
            echo "Tunnel 'snowtuner' already exists — reusing it."
        else
            cloudflared tunnel create snowtuner
        fi
        tunnel_id=$(cloudflared tunnel list | awk '$2 == "snowtuner" { print $1 }')

        read -r -p "Domain hostname for the tunnel (e.g. snowtuner.example.com): " hostname
        log "Mapping ${hostname} -> tunnel ${tunnel_id}"
        cloudflared tunnel route dns "${tunnel_id}" "${hostname}"

        # Write the ingress config.
        sudo mkdir -p /etc/cloudflared
        sudo tee /etc/cloudflared/config.yml >/dev/null <<EOF
tunnel: ${tunnel_id}
credentials-file: /home/ec2-user/.cloudflared/${tunnel_id}.json
ingress:
  - hostname: ${hostname}
    service: http://127.0.0.1:8770
    originRequest:
      noTLSVerify: true
  - service: http_status:404
EOF
        # Move the credentials file to root-readable location for the service.
        sudo cp "/home/ec2-user/.cloudflared/${tunnel_id}.json" /etc/cloudflared/
        sudo chmod 0600 "/etc/cloudflared/${tunnel_id}.json"
        sudo sed -i "s|/home/ec2-user/.cloudflared/|/etc/cloudflared/|" /etc/cloudflared/config.yml

        log "Installing cloudflared as a systemd service"
        sudo cloudflared --config /etc/cloudflared/config.yml service install
        sudo systemctl enable --now cloudflared
        sudo systemctl status cloudflared --no-pager --lines=8

        log "Done"
        echo "Your snowtuner instance should be reachable at:"
        echo "  https://${hostname}"
        echo
        echo "Don't forget to grab the API token:"
        echo "  sudo cat /var/lib/snowtuner/api_token"
        echo "and paste it into the UI's Settings page."
        ;;
    *)
        echo "Unrecognized choice; aborting." >&2
        exit 1
        ;;
esac
