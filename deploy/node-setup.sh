#!/usr/bin/env bash
# Prepares ONE Proxmox node for the dashboard's SSH functions
# (package upgrades, mainboard info, temperatures, reboot-required check).
# Run as root on every node:
#
#   bash node-setup.sh "ssh-ed25519 AAAA... pve-dashboard"     # public key from Admin → SSH
#   bash node-setup.sh --uninstall
#
# What it does (and why it is reasonably safe):
#   * creates the system user "pve-dashboard" (no password, key-only)
#   * authorized_keys: restrict + forced command -> the key can ONLY start the dispatcher
#     /usr/local/sbin/pve-dashboard-ssh, no shell, no port/agent/X11 forwarding
#   * the dispatcher accepts a fixed list of verbs and calls the root helper via ONE sudo rule
#     (lxc-check/lxc-upgrade additionally take a numeric container ID - optional guest updates)
#
# Honest caveat: "upgrade" runs apt as root, and package maintainer scripts run as root;
# lxc-upgrade does the same inside a container.
# Whoever controls the dashboard's private key can therefore install updates from your
# configured repositories - but cannot run arbitrary commands.
set -euo pipefail

USER_NAME="pve-dashboard"
HOME_DIR="/var/lib/pve-dashboard"
DISPATCH="/usr/local/sbin/pve-dashboard-ssh"
HELPER="/usr/local/sbin/pve-dashboard-helper"
SUDOERS="/etc/sudoers.d/pve-dashboard"

[ "$(id -u)" = 0 ] || { echo "Bitte als root ausführen" >&2; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
  rm -f "$DISPATCH" "$HELPER" "$SUDOERS"
  userdel -r "$USER_NAME" 2>/dev/null || true
  echo "Entfernt."
  exit 0
fi

PUBKEY="${1:-}"
case "$PUBKEY" in
  ssh-ed25519\ *|ssh-rsa\ *|ecdsa-sha2-*) ;;
  *) echo "Usage: $0 \"<public key from Admin → SSH>\"" >&2; exit 1 ;;
esac

command -v sudo >/dev/null || apt-get install -y sudo
# Optional, for temperatures; ignore if offline.
command -v sensors >/dev/null || apt-get install -y lm-sensors >/dev/null 2>&1 || true

if ! id "$USER_NAME" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$HOME_DIR" --shell /bin/sh "$USER_NAME"
fi
passwd -l "$USER_NAME" >/dev/null

install -d -m 700 -o "$USER_NAME" -g "$USER_NAME" "$HOME_DIR/.ssh"
printf 'restrict,command="%s" %s\n' "$DISPATCH" "$PUBKEY" > "$HOME_DIR/.ssh/authorized_keys"
chown "$USER_NAME:$USER_NAME" "$HOME_DIR/.ssh/authorized_keys"
chmod 600 "$HOME_DIR/.ssh/authorized_keys"

# --- dispatcher (runs as pve-dashboard) ----------------------------------------------------------
cat > "$DISPATCH" <<'EOF'
#!/bin/sh
# Forced command for the dashboard key: only the verbs below are allowed.
set -f  # no globbing of the client-supplied string
case "$SSH_ORIGINAL_COMMAND" in
  upgrade|update|dmi|sensors|reboot-required|version)
    exec sudo -n /usr/local/sbin/pve-dashboard-helper "$SSH_ORIGINAL_COMMAND" ;;
  "lxc-check "*|"lxc-upgrade "*)
    verb=${SSH_ORIGINAL_COMMAND%% *}
    vmid=${SSH_ORIGINAL_COMMAND#* }
    case "$vmid" in
      ""|*[!0-9]*) echo "pve-dashboard: invalid vmid" >&2; exit 126 ;;
    esac
    exec sudo -n /usr/local/sbin/pve-dashboard-helper "$verb" "$vmid" ;;
  *)
    echo "pve-dashboard: command not allowed: $SSH_ORIGINAL_COMMAND" >&2
    exit 126 ;;
esac
EOF
chown root:root "$DISPATCH"; chmod 755 "$DISPATCH"

# --- helper (runs as root via sudo) ------------------------------------------------------------
cat > "$HELPER" <<'EOF'
#!/bin/sh
# pve-dashboard-helper: fixed actions for the Proxmox dashboard. Root-owned, not writable by others.
set -eu
export LC_ALL=C DEBIAN_FRONTEND=noninteractive
APT_OPTS="-y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"
case "${1:-}" in
  update)
    apt-get update ;;
  upgrade)
    # Proxmox: always dist-upgrade (full-upgrade), never plain "upgrade".
    apt-get update
    apt-get $APT_OPTS dist-upgrade
    apt-get $APT_OPTS autoremove --purge ;;
  dmi)
    d=/sys/class/dmi/id
    r() { tr -d '"\\' < "$d/$1" 2>/dev/null || true; }
    printf '{"board_vendor":"%s","board_name":"%s","bios_version":"%s","sys_vendor":"%s","product_name":"%s"}\n' \
      "$(r board_vendor)" "$(r board_name)" "$(r bios_version)" "$(r sys_vendor)" "$(r product_name)" ;;
  sensors)
    if command -v sensors >/dev/null; then sensors -j 2>/dev/null || echo '{}'; else echo '{}'; fi ;;
  reboot-required)
    if [ -f /var/run/reboot-required ]; then echo yes; exit 0; fi
    # Proxmox kernels don't always create the flag file: compare with the newest installed kernel.
    running=$(uname -r)
    newest=$(ls -1 /boot/vmlinuz-*-pve 2>/dev/null | sed 's|/boot/vmlinuz-||' | sort -V | tail -n1)
    if [ -n "$newest" ] && [ "$newest" != "$running" ]; then echo yes; else echo no; fi ;;
  lxc-check|lxc-upgrade)
    # Optional guest updates (GUEST_UPDATES=true): Debian/Ubuntu containers only.
    vmid="${2:-}"
    case "$vmid" in ""|*[!0-9]*) echo "invalid vmid" >&2; exit 2 ;; esac
    ostype=$(pct config "$vmid" 2>/dev/null | sed -n 's/^ostype: //p')
    case "$ostype" in debian|ubuntu) ;; *) echo "unsupported ostype: $ostype" >&2; exit 3 ;; esac
    if [ "$1" = lxc-check ]; then
      pct exec "$vmid" -- sh -c 'apt-get update -qq >/dev/null 2>&1; apt list --upgradable 2>/dev/null | grep -c "upgradable from" || true'
    else
      pct exec "$vmid" -- env DEBIAN_FRONTEND=noninteractive sh -c \
        'apt-get update && apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold dist-upgrade'
    fi ;;
  version)
    echo "pve-dashboard-helper 2" ;;
  *)
    echo "unknown action" >&2; exit 2 ;;
esac
EOF
chown root:root "$HELPER"; chmod 755 "$HELPER"

# --- sudo rule (validated before it is activated) -------------------------------------------------
TMP=$(mktemp)
printf '%s ALL=(root) NOPASSWD: %s\nDefaults:%s !requiretty\n' "$USER_NAME" "$HELPER" "$USER_NAME" > "$TMP"
visudo -cf "$TMP" >/dev/null
install -m 440 -o root -g root "$TMP" "$SUDOERS"
rm -f "$TMP"

echo "Fertig auf $(hostname). Host-Key-Fingerprint zum Abgleich im Dashboard:"
for f in /etc/ssh/ssh_host_ed25519_key.pub /etc/ssh/ssh_host_ecdsa_key.pub; do
  [ -f "$f" ] && ssh-keygen -lf "$f"
done
