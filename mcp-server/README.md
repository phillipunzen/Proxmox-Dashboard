# pve-dashboard-mcp

MCP server (Model Context Protocol, SDK 2.x) for the Proxmox Dashboard. It talks only to the
dashboard REST API with an API key - see the main README, section "MCP-Server für KI-Assistenten".

```bash
python3 -m venv .venv && .venv/bin/pip install .
DASHBOARD_URL=https://dash.lan DASHBOARD_API_KEY=pvd_… .venv/bin/pve-dashboard-mcp          # stdio
MCP_TRANSPORT=http MCP_AUTH_TOKEN=$(openssl rand -base64 32) … .venv/bin/pve-dashboard-mcp   # :8765/mcp
```

Tools with scope `read`: cluster_overview, list_guests, guest_details, top_consumers,
load_at_time, node_status, list_alerts, recent_tasks, backup_status, storage_forecast.
With scope `control` additionally: guest_power, create_snapshot, start_backup, acknowledge_alert.
