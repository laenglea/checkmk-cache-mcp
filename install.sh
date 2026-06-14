#!/bin/bash
# install.sh — deploys checkmk-cache-mcp onto a RHEL/CentOS/Rocky server.
# Run as root.
#
# Usage:
#   ./install.sh [site_name]
#
# site_name defaults to "mysite".
# The script is idempotent — safe to re-run for upgrades.

set -euo pipefail

SITE="${1:-mysite}"
INSTALL_DIR="/opt/checkmk-cache-mcp"
SERVICE_USER="cmkcache"
SYSCONFIG_DIR="/etc/sysconfig"
SYSTEMD_DIR="/etc/systemd/system"
LOG_DIR="/var/log/checkmk-cache-collector"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || error "Please run as root."

# ── Dependencies ─────────────────────────────────────────────────────────────
info "Checking dependencies..."
MISSING=()
for pkg in inotify-tools zstd python3; do
    command -v "${pkg/inotify-tools/inotifywait}" &>/dev/null \
        || rpm -q "$pkg" &>/dev/null \
        || MISSING+=("$pkg")
done
if [[ ${#MISSING[@]} -gt 0 ]]; then
    warn "Installing missing packages: ${MISSING[*]}"
    yum install -y "${MISSING[@]}" || error "Package install failed."
fi

# ── Service user ─────────────────────────────────────────────────────────────
if ! id "$SERVICE_USER" &>/dev/null; then
    info "Creating service user: $SERVICE_USER"
    useradd -r -s /sbin/nologin -d /var/lib/cmkcache -m "$SERVICE_USER"
fi

if getent group "$SITE" &>/dev/null; then
    info "Adding $SERVICE_USER to group '$SITE' (CheckMK cache access)..."
    usermod -aG "$SITE" "$SERVICE_USER"
else
    warn "Group '$SITE' not found — skipping group membership."
    warn "Once CheckMK is installed, run: usermod -aG $SITE $SERVICE_USER"
fi

# ── Install files ─────────────────────────────────────────────────────────────
info "Installing to $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR/collector" "$INSTALL_DIR/mcp"

install -m 755 collector/sync_checkmk_cache.sh "$INSTALL_DIR/collector/"
install -m 644 mcp/checkmk_cache_mcp.py        "$INSTALL_DIR/mcp/"
install -m 644 mcp/requirements.txt            "$INSTALL_DIR/mcp/"

# ── Python virtualenv ─────────────────────────────────────────────────────────
VENV="$INSTALL_DIR/mcp/venv"
if [[ ! -d "$VENV" ]]; then
    info "Creating Python virtualenv at $VENV ..."
    python3 -m venv "$VENV"
fi
info "Installing Python dependencies..."
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$INSTALL_DIR/mcp/requirements.txt"

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# ── Log directory ─────────────────────────────────────────────────────────────
mkdir -p "$LOG_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$LOG_DIR"

# ── Config files (don't overwrite existing) ───────────────────────────────────
COLLECTOR_ENV="$SYSCONFIG_DIR/sync_checkmk_cache_${SITE}"
MCP_ENV="$SYSCONFIG_DIR/checkmk-cache-mcp"

if [[ ! -f "$COLLECTOR_ENV" ]]; then
    install -m 640 collector/sync_checkmk_cache.env.example "$COLLECTOR_ENV"
    warn "Created $COLLECTOR_ENV — please edit before starting the service."
else
    info "Skipping $COLLECTOR_ENV (already exists)."
fi

if [[ ! -f "$MCP_ENV" ]]; then
    install -m 640 mcp/checkmk_cache_mcp.env.example "$MCP_ENV"
    warn "Created $MCP_ENV — please edit before starting the service."
else
    info "Skipping $MCP_ENV (already exists)."
fi

# ── systemd units ─────────────────────────────────────────────────────────────
info "Installing systemd units..."
install -m 644 systemd/checkmk-cache-collector@.service "$SYSTEMD_DIR/"
install -m 644 systemd/checkmk-cache-mcp.service        "$SYSTEMD_DIR/"
systemctl daemon-reload

info "Enabling services..."
systemctl enable "checkmk-cache-collector@${SITE}"
systemctl enable checkmk-cache-mcp

# ── inotify sysctl (persistent) ───────────────────────────────────────────────
SYSCTL_FILE="/etc/sysctl.d/60-checkmk-cache.conf"
if [[ ! -f "$SYSCTL_FILE" ]]; then
    info "Writing $SYSCTL_FILE ..."
    echo "fs.inotify.max_queued_events = 131072" > "$SYSCTL_FILE"
    sysctl -p "$SYSCTL_FILE"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
info "Installation complete."
echo ""
echo "  Next steps:"
echo "  1. Edit  $COLLECTOR_ENV"
echo "  2. Edit  $MCP_ENV"
echo "  3. Run:  systemctl start checkmk-cache-collector@${SITE}"
echo "           systemctl start checkmk-cache-mcp"
echo "  4. Check: journalctl -u checkmk-cache-collector@${SITE} -f"
echo "            journalctl -u checkmk-cache-mcp -f"
