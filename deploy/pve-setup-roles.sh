#!/usr/bin/env bash
# Creates the Proxmox roles, user and API tokens for the dashboard (least privilege).
# Run once as root on ANY node of the cluster (config is cluster-wide). Idempotent.
#
#   bash pve-setup-roles.sh            # read-only token only (phase 1/2)
#   bash pve-setup-roles.sh --operator # additionally the operator token (phase 3+)
#   bash pve-setup-roles.sh --node-admin # additionally node-level token (updates, reboot)
#   bash pve-setup-roles.sh --operator --guest-updates
#                                      # operator may run apt in VMs via the guest agent
#                                      # (VM.GuestAgent.Unrestricted = root inside the guest!)
#
# Token secrets are printed ONCE by Proxmox - copy them into the dashboard's .env.
set -euo pipefail

USER_ID="dashboard@pve"

# Proxmox VE 9 privilege names (VM.Monitor was replaced by VM.GuestAgent.*).
# Mapping.Audit: resolve PCI resource mappings (hostpciN: mapping=...) for the inventory.
RO_PRIVS="Sys.Audit,VM.Audit,Datastore.Audit,Pool.Audit,SDN.Audit,Mapping.Audit,VM.GuestAgent.Audit"
OP_PRIVS="${RO_PRIVS},VM.PowerMgmt,VM.Snapshot,VM.Snapshot.Rollback,VM.Backup,VM.Migrate,Datastore.AllocateSpace"
NODE_PRIVS="${OP_PRIVS},Sys.Modify,Sys.PowerMgmt"

want_operator=false
want_node_admin=false
want_guest_updates=false
for arg in "$@"; do
  case "$arg" in
    --operator) want_operator=true ;;
    --guest-updates) want_operator=true; want_guest_updates=true ;;
    --node-admin) want_operator=true; want_node_admin=true ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

ensure_role() { # name privs
  if pveum role list --output-format json | grep -q "\"roleid\":\"$1\""; then
    pveum role modify "$1" --privs "$2"
    echo "Role $1 updated"
  else
    pveum role add "$1" --privs "$2"
    echo "Role $1 created"
  fi
}

ensure_token() { # tokenid role
  local full="${USER_ID}!$1"
  if pveum user token list "$USER_ID" --output-format json | grep -q "\"tokenid\":\"$1\""; then
    echo "Token $full exists (secret unchanged; delete it to regenerate)"
  else
    echo "Creating token $full - SAVE THE SECRET BELOW:"
    # privsep=1: the token only gets the ACLs assigned to the token itself.
    pveum user token add "$USER_ID" "$1" --privsep 1 --comment "Proxmox Dashboard ($2)"
  fi
  pveum acl modify / --tokens "$full" --roles "$2"
}

$want_guest_updates && OP_PRIVS="${OP_PRIVS},VM.GuestAgent.Unrestricted" \
  && NODE_PRIVS="${NODE_PRIVS},VM.GuestAgent.Unrestricted"
ensure_role Dashboard-ReadOnly "$RO_PRIVS"
$want_operator && ensure_role Dashboard-Operator "$OP_PRIVS"
$want_node_admin && ensure_role Dashboard-NodeAdmin "$NODE_PRIVS"

if ! pveum user list --output-format json | grep -q "\"userid\":\"${USER_ID}\""; then
  # Realm "pve" user without password: it can only be used through its API tokens.
  pveum user add "$USER_ID" --comment "Proxmox Dashboard service user"
fi

# With privsep the effective rights are (user ACL) AND (token ACL),
# so the user carries the widest role that any of its tokens needs.
user_role=Dashboard-ReadOnly
$want_operator && user_role=Dashboard-Operator
$want_node_admin && user_role=Dashboard-NodeAdmin
pveum acl modify / --users "$USER_ID" --roles "$user_role"

ensure_token readonly Dashboard-ReadOnly
$want_operator && ensure_token operator Dashboard-Operator
$want_node_admin && ensure_token nodeadmin Dashboard-NodeAdmin

echo
echo "Done. Effective permissions can be checked with:"
echo "  pveum user permissions '${USER_ID}!readonly' --path /"
