#!/bin/bash
# install.sh — deploys checkmk-cache-mcp onto a RHEL/CentOS/Rocky server.
# Run as root.
#
# Usage:
#   ./install.sh [--mode current|full] [site_name]
#
#   --mode current   MCP server only (no collector, no history tools)
#   --mode full      MCP server + collector + history tools
#   site_name        CheckMK site name (used to suggest default paths)
#
# Without --mode the script asks interactively.
# The script is idempotent — safe to re-run for upgrades.

set -euo pipefail

# ── Argument parsing ──────────────────────────────────────────────────────────
MODE=""
SITE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)
            MODE="${2:-}"
            shift 2
            ;;
        --mode=*)
            MODE="${1#--mode=}"
            shift
            ;;
        -*)
            echo "Unknown option: $1" >&2
            echo "Usage: $0 [--mode current|full] [site_name]" >&2
            exit 1
            ;;
        *)
            SITE="$1"
            shift
            ;;
    esac
done

# ── Constants ─────────────────────────────────────────────────────────────────
INSTALL_DIR="/opt/checkmk-cache-mcp"
ETC_DIR="$INSTALL_DIR/etc"
SERVICE_USER="cmkcache"
SYSTEMD_DIR="/etc/systemd/system"
HTTPD_CONF_DIR="/etc/httpd/conf.d"
LOG_DIR="/var/log/checkmk-cache-collector"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
step()    { echo -e "\n${CYAN}──── $* ────${NC}"; }
created() { echo -e "  ${GREEN}+${NC} $*"; }
skipped() { echo -e "  ${YELLOW}~${NC} $* (already exists, skipped)"; }
updated() { echo -e "  ${GREEN}↑${NC} $*"; }

# Prompt helper: ask question with a default value, return answer in $REPLY
ask() {
    local prompt="$1" default="$2"
    read -rp "$prompt [$default]: " REPLY
    if [[ -z "$REPLY" ]]; then REPLY="$default"; fi
}

[[ $EUID -eq 0 ]] || error "Please run as root."

echo ""
echo -e "${CYAN}CheckMK Cache MCP — Installer${NC}"
echo ""

# ── Interactive questions ─────────────────────────────────────────────────────

# Mode
if [[ "$MODE" != "current" && "$MODE" != "full" ]]; then
    if [[ -n "$MODE" ]]; then
        echo "Invalid --mode '$MODE'. Use 'current' or 'full'." >&2
        exit 1
    fi
    echo "Select installation mode:"
    echo "  [1] current  — MCP server only (reads live CheckMK cache, no history)"
    echo "  [2] full     — MCP server + collector + historical archive tools"
    echo ""
    while true; do
        read -rp "Mode [1/2]: " _choice
        case "$_choice" in
            1|current) MODE="current"; break ;;
            2|full)    MODE="full";    break ;;
            *) echo "Please enter 1 or 2." ;;
        esac
    done
    echo ""
fi

# Site name — auto-detect from /opt/omd/sites/ if not given via CLI
if [[ -z "$SITE" ]]; then
    mapfile -t _sites < <(find /opt/omd/sites -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | sort)
    case "${#_sites[@]}" in
        0)
            ask "CheckMK site name (no sites found under /opt/omd/sites)" "mysite"
            SITE="$REPLY"
            ;;
        1)
            SITE="${_sites[0]}"
            info "Auto-detected CheckMK site: $SITE"
            ;;
        *)
            echo "Found CheckMK sites:"
            for i in "${!_sites[@]}"; do
                echo "  [$((i+1))] ${_sites[$i]}"
            done
            echo ""
            while true; do
                read -rp "Select site [1-${#_sites[@]}]: " _choice
                if [[ "$_choice" =~ ^[0-9]+$ ]] && (( _choice >= 1 && _choice <= ${#_sites[@]} )); then
                    SITE="${_sites[$((_choice-1))]}"
                    break
                fi
                echo "Please enter a number between 1 and ${#_sites[@]}."
            done
            ;;
    esac
fi

# Cache dir (suggested from site name)
ask "CheckMK cache directory (CHECKMK_CACHE_DIR)" "/opt/omd/sites/${SITE}/tmp/check_mk/cache"
CACHE_DIR="$REPLY"

# History dir (full mode only)
HISTORY_DIR=""
if [[ "$MODE" == "full" ]]; then
    ask "History archive directory (CHECKMK_HISTORY_DIR)" "/cachehistory/${SITE}"
    HISTORY_DIR="$REPLY"
fi

# MCP port
ask "MCP server port (MCP_PORT)" "8765"
MCP_PORT="$REPLY"

# Apache proxy
APACHE_PROXY=false
while true; do
    read -rp "Configure Apache reverse proxy? [y/n]: " _choice
    case "$_choice" in
        y|Y|yes) APACHE_PROXY=true;  break ;;
        n|N|no)  APACHE_PROXY=false; break ;;
        *) echo "Please enter y or n." ;;
    esac
done

echo ""
echo -e "  Mode:      ${MODE}"
echo -e "  Site:      ${SITE}"
echo -e "  Cache dir: ${CACHE_DIR}"
[[ "$MODE" == "full" ]] && echo -e "  Hist dir:  ${HISTORY_DIR}"
echo -e "  Port:      ${MCP_PORT}"
echo -e "  Apache:    $( [[ "$APACHE_PROXY" == "true" ]] && echo "yes  →  /checkmk-cache-mcp/${SITE}" || echo "no" )"
echo -e "  Dest:      ${INSTALL_DIR}"
echo ""

# ── Dependencies ──────────────────────────────────────────────────────────────
step "Dependencies"
MISSING=()
PKGS=(python3)
[[ "$MODE" == "full" ]] && PKGS+=(inotify-tools zstd)

for pkg in "${PKGS[@]}"; do
    cmd="${pkg/inotify-tools/inotifywait}"
    if ! command -v "$cmd" &>/dev/null && ! rpm -q "$pkg" &>/dev/null 2>&1; then
        MISSING+=("$pkg")
    fi
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
    warn "Installing missing packages: ${MISSING[*]}"
    yum install -y "${MISSING[@]}" || error "Package install failed."
    for pkg in "${MISSING[@]}"; do created "installed package: $pkg"; done
else
    info "All required packages present."
fi

# ── Service user ──────────────────────────────────────────────────────────────
step "Service user"
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd -r -s /sbin/nologin -d /var/lib/cmkcache -m "$SERVICE_USER"
    created "user: $SERVICE_USER"
else
    info "User '$SERVICE_USER' already exists."
fi

if getent group "$SITE" &>/dev/null; then
    usermod -aG "$SITE" "$SERVICE_USER"
    info "Added $SERVICE_USER to group '$SITE' (CheckMK cache read access)."
else
    warn "Group '$SITE' not found — skipping group membership."
    warn "Once CheckMK is installed, run: usermod -aG $SITE $SERVICE_USER"
fi

# ── Install files ─────────────────────────────────────────────────────────────
step "Application files  →  $INSTALL_DIR"
mkdir -p "$INSTALL_DIR/mcp" "$ETC_DIR"
[[ "$MODE" == "full" ]] && mkdir -p "$INSTALL_DIR/collector"

install -m 755 mcp/checkmk_cache_mcp.py  "$INSTALL_DIR/mcp/"
updated "$INSTALL_DIR/mcp/checkmk_cache_mcp.py"
install -m 644 mcp/requirements.txt      "$INSTALL_DIR/mcp/"
updated "$INSTALL_DIR/mcp/requirements.txt"

if [[ "$MODE" == "full" ]]; then
    install -m 755 collector/sync_checkmk_cache.sh "$INSTALL_DIR/collector/"
    updated "$INSTALL_DIR/collector/sync_checkmk_cache.sh"
fi

# ── Python virtualenv ─────────────────────────────────────────────────────────
step "Python virtualenv  →  $INSTALL_DIR/mcp/venv"
VENV="$INSTALL_DIR/mcp/venv"
if [[ ! -d "$VENV" ]]; then
    python3 -m venv "$VENV"
    created "$VENV"
else
    info "Virtualenv already exists, updating packages."
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$INSTALL_DIR/mcp/requirements.txt"
info "Python dependencies up to date."

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# ── Config files ──────────────────────────────────────────────────────────────
step "Config files  →  $ETC_DIR"

# Always update example templates so they stay current after upgrades
install -m 640 etc/site.env.example "$ETC_DIR/site.env.example"
updated "$ETC_DIR/site.env.example"
if [[ "$MODE" == "full" ]]; then
    install -m 640 etc/collector.env.example "$ETC_DIR/collector.env.example"
    updated "$ETC_DIR/collector.env.example"
fi

# <site>.env — always (re)create so values stay in sync with install answers
SITE_ENV="$ETC_DIR/${SITE}.env"
install -m 640 etc/site.env.example "$SITE_ENV"
sed -i "s|^CHECKMK_CACHE_DIR=.*|CHECKMK_CACHE_DIR=${CACHE_DIR}|"       "$SITE_ENV"
sed -i "s|^CHECKMK_HISTORY_DIR=.*|CHECKMK_HISTORY_DIR=${HISTORY_DIR}|" "$SITE_ENV"
sed -i "s|^MCP_PORT=.*|MCP_PORT=${MCP_PORT}|"                           "$SITE_ENV"
chown "$SERVICE_USER:$SERVICE_USER" "$SITE_ENV"
updated "$SITE_ENV"

# collector-<site>.env — always (re)create
if [[ "$MODE" == "full" ]]; then
    COLLECTOR_ENV="$ETC_DIR/collector-${SITE}.env"
    install -m 640 etc/collector.env.example "$COLLECTOR_ENV"
    sed -i "s|mysite|${SITE}|g"                                          "$COLLECTOR_ENV"
    sed -i "s|^SOURCE_DIR=.*|SOURCE_DIR=${CACHE_DIR}|"                   "$COLLECTOR_ENV"
    sed -i "s|^DEST_DIR=.*|DEST_DIR=${HISTORY_DIR}|"                     "$COLLECTOR_ENV"
    sed -i "s|^LOCKDIR=.*|LOCKDIR=/run/checkmk-cache-collector/${SITE}|" "$COLLECTOR_ENV"
    chown "$SERVICE_USER:$SERVICE_USER" "$COLLECTOR_ENV"
    updated "$COLLECTOR_ENV"
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$ETC_DIR"

# ── Apache httpd config ───────────────────────────────────────────────────────
step "Apache httpd config"
if [[ "$APACHE_PROXY" == "true" ]]; then
    HTTPD_CONF="$HTTPD_CONF_DIR/checkmk-cache-mcp-${SITE}.conf"
    if [[ ! -d "$HTTPD_CONF_DIR" ]]; then
        warn "$HTTPD_CONF_DIR not found — is httpd installed?"
        warn "Copy etc/httpd-mcp.conf.example manually and adjust site/port."
    else
        # Generate config directly — avoids SELinux httpd_config_t issues with symlinks
        cat > "$HTTPD_CONF" <<EOF
# /etc/httpd/conf.d/checkmk-cache-mcp-${SITE}.conf
# Generated by install.sh — do not edit manually, re-run install.sh to update.

<Location /checkmk-cache-mcp/${SITE}>
    ProxyPass        http://127.0.0.1:${MCP_PORT}
    ProxyPassReverse http://127.0.0.1:${MCP_PORT}
    ProxyPreserveHost On
    SetEnv proxy-sendchunked 1
    SetEnv proxy-sendcl 0
    SetEnv proxy-initial-not-buffered 1
    SetEnv force-proxy-request-1.0 0
</Location>
EOF
        chmod 644 "$HTTPD_CONF"
        updated "$HTTPD_CONF  (Location: /checkmk-cache-mcp/${SITE}, Port: ${MCP_PORT})"
        if systemctl is-active httpd &>/dev/null; then
            if httpd -t 2>/dev/null; then
                systemctl reload httpd
                info "Apache reloaded."
            else
                warn "Apache config test failed — skipping reload. Check: httpd -t"
            fi
        else
            warn "Apache not running — start it with: systemctl start httpd"
        fi
    fi
else
    info "Skipped (not requested)."
fi

# ── History directory ─────────────────────────────────────────────────────────
if [[ "$MODE" == "full" ]]; then
    step "History directory  →  $HISTORY_DIR"
    if [[ ! -d "$HISTORY_DIR" ]]; then
        mkdir -p "$HISTORY_DIR"
        chown "$SERVICE_USER:$SERVICE_USER" "$HISTORY_DIR"
        created "$HISTORY_DIR"
    else
        info "$HISTORY_DIR already exists."
    fi
fi

# ── Log directory ─────────────────────────────────────────────────────────────
if [[ "$MODE" == "full" ]]; then
    step "Log directory  →  $LOG_DIR"
    if [[ ! -d "$LOG_DIR" ]]; then
        mkdir -p "$LOG_DIR"
        chown "$SERVICE_USER:$SERVICE_USER" "$LOG_DIR"
        created "$LOG_DIR"
    else
        info "$LOG_DIR already exists."
    fi
fi

# ── inotify sysctl ────────────────────────────────────────────────────────────
if [[ "$MODE" == "full" ]]; then
    step "inotify sysctl"
    SYSCTL_FILE="/etc/sysctl.d/60-checkmk-cache.conf"
    if [[ ! -f "$SYSCTL_FILE" ]]; then
        echo "fs.inotify.max_queued_events = 131072" > "$SYSCTL_FILE"
        sysctl -p "$SYSCTL_FILE" &>/dev/null
        created "$SYSCTL_FILE  (fs.inotify.max_queued_events = 131072)"
    else
        info "$SYSCTL_FILE already exists."
    fi
fi

# ── systemd units ─────────────────────────────────────────────────────────────
step "systemd units  →  $SYSTEMD_DIR"
install -m 644 systemd/checkmk-cache-mcp@.service "$SYSTEMD_DIR/"
updated "$SYSTEMD_DIR/checkmk-cache-mcp@.service"

if [[ "$MODE" == "full" ]]; then
    install -m 644 systemd/checkmk-cache-collector@.service "$SYSTEMD_DIR/"
    updated "$SYSTEMD_DIR/checkmk-cache-collector@.service"
fi

systemctl daemon-reload
info "systemd daemon reloaded."

step "Enable services"
systemctl enable "checkmk-cache-mcp@${SITE}"
info "Enabled: checkmk-cache-mcp@${SITE}"

if [[ "$MODE" == "full" ]]; then
    systemctl enable "checkmk-cache-collector@${SITE}"
    info "Enabled: checkmk-cache-collector@${SITE}"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}══════════════════════════════════════════${NC}"
echo -e "${GREEN}  Installation complete  (mode: ${MODE}, site: ${SITE})${NC}"
echo -e "${CYAN}══════════════════════════════════════════${NC}"
echo ""
echo "  Config files (verify before starting):"
echo "    $ETC_DIR/${SITE}.env"
[[ "$MODE" == "full" ]] && echo "    $ETC_DIR/collector-${SITE}.env"
[[ "$APACHE_PROXY" == "true" ]] && echo "    $HTTPD_CONF_DIR/checkmk-cache-mcp-${SITE}.conf"
echo ""
echo "  Start services:"
echo "    systemctl start checkmk-cache-mcp@${SITE}"
[[ "$MODE" == "full" ]] && echo "    systemctl start checkmk-cache-collector@${SITE}"
echo ""
echo "  Check logs:"
echo "    journalctl -u checkmk-cache-mcp@${SITE} -f"
[[ "$MODE" == "full" ]] && echo "    journalctl -u checkmk-cache-collector@${SITE} -f"
echo ""
echo "  Health check:"
echo "    curl http://127.0.0.1:${MCP_PORT}/health"
[[ "$APACHE_PROXY" == "true" ]] && echo "    curl http://localhost/checkmk-cache-mcp/${SITE}/health"
echo ""
