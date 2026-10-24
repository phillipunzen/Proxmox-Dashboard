"""MCP server for the Proxmox Dashboard.

Read tools are always available. Control tools are only registered when the configured API
key has the ``control`` scope - the dashboard enforces this again on every call, and every
action lands in the dashboard's audit log as ``apikey:<name>``.

Environment:
  DASHBOARD_URL        e.g. http://proxmox-dashboard:8000 (default http://localhost:8080)
  DASHBOARD_API_KEY    pvd_… (scope read, or control for actions)
  DASHBOARD_VERIFY_TLS true|false (default true)
  MCP_TRANSPORT        stdio (default) | http
  MCP_HOST / MCP_PORT  for http (default 0.0.0.0:8765)
  MCP_AUTH_TOKEN       required for http: clients send "Authorization: Bearer <token>"
  MCP_ALLOWED_HOSTS    optional comma list -> enables DNS-rebinding protection
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from pve_dashboard_mcp.client import DashboardClient, DashboardError

log = logging.getLogger("pve-dashboard-mcp")

INSTRUCTIONS = """Zugriff auf ein Proxmox-VE-Dashboard (Cluster, Nodes, VMs/LXC-Container).
Einheiten: CPU-Werte in Prozent; "host_share" = Anteil an der Host-CPU-Kapazität;
"cores" = belegte Host-Threads; Raten in MB/s; Speicher in GiB.
Regeln für Aktionen: Frage den Benutzer IMMER, bevor du einen Gast startest, stoppst,
neu startest oder ein Backup/Snapshot auslöst. Für einen harten Stopp (action="stop") musst
du confirm_name auf den exakten Gast-Namen setzen - nur nach ausdrücklicher Bestätigung.
Bevorzuge "shutdown" (sauber) gegenüber "stop" (hart)."""

GiB = 1024**3
MB = 1000**2


def _pct(v: float | None) -> float | None:
    return None if v is None else round(v * 100, 1)


def _gib(v: float | None) -> float | None:
    return None if v is None else round(v / GiB, 2)


def _mbs(v: float | None) -> float | None:
    return None if v is None else round(v / MB, 2)


def parse_time(value: str, now: datetime | None = None) -> float:
    """ISO 8601 (with offset; naive = local time) or relative like -2h, -30m, -1d."""
    now = now or datetime.now(UTC)
    m = re.fullmatch(r"\s*-(\d+(?:\.\d+)?)\s*([mhd])\s*", value)
    if m:
        amount = float(m.group(1))
        delta = {"m": timedelta(minutes=amount), "h": timedelta(hours=amount),
                 "d": timedelta(days=amount)}[m.group(2)]
        return (now - delta).timestamp()
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ToolError("Zeit als ISO 8601 (2026-10-24T14:30:00+02:00) oder relativ (-2h)") \
            from exc
    if dt.tzinfo is None:
        dt = dt.astimezone()  # local time of the MCP server
    return dt.timestamp()


def _guest_row(g: dict[str, Any]) -> dict[str, Any]:
    running = g["status"] == "running"
    row: dict[str, Any] = {"vmid": g["vmid"], "name": g["name"], "type": g["type"],
                           "node": g["node"], "cluster": g["cluster"], "status": g["status"]}
    if running:
        row.update(cpu_pct=_pct(g["cpu"]), host_share_pct=_pct(g["cpu_host"]),
                   vcpus=g["maxcpu"], mem_gib=_gib(g["mem"]), mem_max_gib=_gib(g["maxmem"]),
                   disk_read_mbs=_mbs(g.get("diskread_bps")),
                   disk_write_mbs=_mbs(g.get("diskwrite_bps")),
                   net_in_mbs=_mbs(g.get("netin_bps")), net_out_mbs=_mbs(g.get("netout_bps")))
    else:
        row.update(vcpus=g["maxcpu"], mem_max_gib=_gib(g["maxmem"]))
    if g.get("tags"):
        row["tags"] = g["tags"]
    return row


async def resolve_guest(client: DashboardClient, guest: str,
                        cluster: str | None) -> dict[str, Any]:
    """Find a guest by VMID or (unique) name."""
    guests = await client.get("/api/v1/guests", cluster=cluster, include_templates=False)
    key = guest.strip()
    hits = ([g for g in guests if str(g["vmid"]) == key] if key.isdigit()
            else [g for g in guests if g["name"].lower() == key.lower()])
    if not hits and not key.isdigit():
        hits = [g for g in guests if key.lower() in g["name"].lower()]
    if not hits:
        raise ToolError(f"Kein Gast '{guest}' gefunden")
    if len(hits) > 1:
        names = ", ".join(f"{g['vmid']} {g['name']} ({g['cluster']})" for g in hits[:10])
        raise ToolError(f"'{guest}' ist nicht eindeutig: {names} – bitte VMID/Cluster angeben")
    return hits[0]


def build_server(client: DashboardClient, scopes: list[str]) -> MCPServer:
    mcp = MCPServer("proxmox-dashboard", title="Proxmox Dashboard",
                    instructions=INSTRUCTIONS, version="1.0.1")
    ro = ToolAnnotations(read_only_hint=True, open_world_hint=False)

    # --- read -----------------------------------------------------------------------------

    @mcp.tool(annotations=ro)
    async def cluster_overview(cluster: str | None = None) -> dict[str, Any]:
        """Status aller Cluster: Nodes (online, CPU, RAM, Load), Summen, Gästezahlen,
        Warnungen und aktive Alarme. Guter Einstieg für 'Wie geht es dem Cluster?'."""
        clusters = await client.get("/api/v1/clusters")
        alerts = await client.get("/api/v1/alerts", state="firing")
        out = []
        for c in clusters:
            if cluster and c["name"] != cluster:
                continue
            t = c["totals"]
            out.append({
                "cluster": c["name"], "reachable": c["reachable"], "quorate": c["quorate"],
                "pve_version": c["pve_version"],
                "cpu_pct": _pct(t["cpu"]), "mem_pct": _pct(t["mem"] / t["maxmem"])
                if t["maxmem"] else None, "storage_pct": _pct(t["storage_used"] /
                                                              t["storage_total"])
                if t["storage_total"] else None,
                "guests_running": t["vms_running"] + t["lxc_running"],
                "guests_stopped": t["vms_stopped"] + t["lxc_stopped"],
                "nodes": [{"node": n["node"], "status": n["status"], "cpu_pct": _pct(n["cpu"]),
                           "mem_pct": _pct(n["mem"] / n["maxmem"]) if n["maxmem"] else None,
                           "load1": (n.get("loadavg") or [None])[0],
                           "iowait_pct": _pct(n.get("iowait")), "uptime_h":
                           round(n["uptime"] / 3600, 1)} for n in c["nodes"]],
                "warnings": [w["message"] for w in c["warnings"]],
            })
        return {"clusters": out, "firing_alerts": [
            {"id": a["id"], "severity": a["severity"], "message": a["message"]} for a in alerts]}

    @mcp.tool(annotations=ro)
    async def list_guests(cluster: str | None = None, node: str | None = None,
                          type: Literal["qemu", "lxc"] | None = None,  # noqa: A002
                          status: Literal["running", "stopped", "paused"] | None = None,
                          tag: str | None = None,
                          sort_by: Literal["cpu_host", "mem", "netin_bps", "diskwrite_bps",
                                           "vmid", "name"] = "cpu_host",
                          limit: int = 25) -> list[dict[str, Any]]:
        """VMs und Container mit aktuellen Werten, filter- und sortierbar
        (z. B. größte CPU-Verbraucher: sort_by=cpu_host)."""
        rows = await client.get("/api/v1/guests", cluster=cluster, node=node, type=type,
                                status=status, tag=tag, sort=sort_by,
                                order="asc" if sort_by in ("vmid", "name") else "desc",
                                limit=max(1, min(limit, 200)))
        return [_guest_row(g) for g in rows]

    @mcp.tool(annotations=ro)
    async def guest_details(guest: str, cluster: str | None = None) -> dict[str, Any]:
        """Alles zu einem Gast (VMID oder Name): Live-Werte, Konfiguration (vCPU, RAM,
        Disks, Netzwerk, Passthrough, OS, IPs), Snapshots und letztes Backup."""
        g = await resolve_guest(client, guest, cluster)
        base = f"/api/v1/clusters/{g['cluster']}/guests/{g['vmid']}"
        out = {"live": _guest_row(g), "console_url": g.get("console_url")}
        try:
            inv = await client.get(f"/api/v1/inventory/clusters/{g['cluster']}/guests/{g['vmid']}")
            out["config"] = {k: inv.get(k) for k in (
                "vcpus", "cpu_type", "bios", "machine", "ostype", "os_name", "agent_running",
                "ip_addresses", "onboot", "notes")}
            out["config"]["memory_gib"] = _gib(inv.get("memory"))
            out["config"]["disks"] = [f"{d['key']}: {d.get('storage') or 'bind'} "
                                      f"{_gib(d.get('size'))} GiB" for d in inv["disks"]
                                      if d.get("media") != "cdrom"]
            out["config"]["nics"] = [f"{n['key']}: {n.get('bridge')}"
                                     f"{'.' + str(n['vlan']) if n.get('vlan') else ''}"
                                     for n in inv["nics"]]
            out["config"]["passthrough"] = [f"{p['key']}: {', '.join(p['devices'])}"
                                            for p in inv["hostpci"]]
        except DashboardError:
            out["config"] = None
        out["snapshots"] = [{"name": s["name"], "description": s.get("description"),
                             "time": datetime.fromtimestamp(s["snaptime"], UTC).isoformat()
                             if s.get("snaptime") else None}
                            for s in await client.get(f"{base}/snapshots")]
        backups = await client.get("/api/v1/backups", cluster=g["cluster"])
        b = next((x for x in backups["guests"] if x["vmid"] == g["vmid"]), None)
        if b:
            out["backup"] = {"state": b["state"], "last": (b["last_backup"] or {}).get("ctime"),
                             "age_hours": round(b["age_hours"], 1) if b["age_hours"] else None,
                             "covered_by_jobs": b["covered_by"], "count": b["backup_count"]}
        return out

    @mcp.tool(annotations=ro)
    async def top_consumers(metric: Literal["cpu", "mem", "diskread", "diskwrite", "netin",
                                            "netout"] = "cpu",
                            range: Literal["1h", "6h", "24h", "7d", "30d"] = "24h",  # noqa: A002
                            node: str | None = None, cluster: str | None = None,
                            limit: int = 10) -> dict[str, Any]:
        """Wer verbraucht im Zeitraum am meisten? Durchschnitt, p95 und Maximum je Gast.
        CPU in belegten Host-Threads, RAM in GiB, IO/Netz in MB/s."""
        rows = await client.get("/api/v1/analysis/top", metric=metric, range=range, node=node,
                                cluster=cluster, limit=max(1, min(limit, 50)))
        conv = (lambda v: round(v, 2)) if metric == "cpu" else (
            _gib if metric == "mem" else _mbs)
        unit = {"cpu": "cores", "mem": "GiB"}.get(metric, "MB/s")
        return {"metric": metric, "range": range, "unit": unit, "guests": [
            {"vmid": r["vmid"], "name": r["name"], "node": r["node"], "avg": conv(r["avg"]),
             "p95": conv(r["p95"]), "max": conv(r["max"])} for r in rows]}

    @mcp.tool(annotations=ro)
    async def load_at_time(time: str, node: str | None = None, cluster: str | None = None,
                           metric: Literal["cpu", "mem", "diskread", "diskwrite", "netin",
                                           "netout"] = "cpu",
                           window_minutes: int = 5) -> dict[str, Any]:
        """Drill-down: Welche Gäste haben zu einem Zeitpunkt die Last verursacht?
        time als ISO 8601 (2026-10-24T14:30:00+02:00) oder relativ (-2h, -30m)."""
        if cluster is None:
            clusters = await client.get("/api/v1/clusters")
            if node:
                cluster = next((c["name"] for c in clusters
                                if any(n["node"] == node for n in c["nodes"])), None)
            cluster = cluster or clusters[0]["name"]
        at = parse_time(time)
        r = await client.get("/api/v1/analysis/at", cluster=cluster, node=node, metric=metric,
                             at=int(at), window=max(60, window_minutes * 60))
        conv = (lambda v: round(v, 2)) if metric == "cpu" else (
            _gib if metric == "mem" else _mbs)
        return {"time": datetime.fromtimestamp(at, UTC).isoformat(), "cluster": cluster,
                "node": node, "total": conv(r["total"]) if r["total"] is not None else None,
                "capacity": conv(r["capacity"]) if r["capacity"] is not None else None,
                "guests": [{"vmid": g["vmid"], "name": g["name"], "node": g["node"],
                            "value": conv(g["value"]),
                            "share_pct": _pct(g["share_of_total"])}
                           for g in r["guests"][:10] if g["value"] > 0]}

    @mcp.tool(annotations=ro)
    async def node_status(cluster: str | None = None) -> list[dict[str, Any]]:
        """Pflegezustand der Nodes: Wartungsmodus, verfügbare Updates, Neustart nötig,
        Temperatur, laufender Job."""
        rows = await client.get("/api/v1/node-status", cluster=cluster)
        return [{k: r[k] for k in ("cluster", "node", "online", "maintenance", "updates",
                                   "reboot_required", "temperature", "running_job")}
                for r in rows]

    @mcp.tool(annotations=ro)
    async def list_alerts(state: Literal["firing", "resolved", "all"] = "firing",
                          limit: int = 30) -> list[dict[str, Any]]:
        """Alarme des Dashboards (aktiv oder Verlauf)."""
        rows = await client.get("/api/v1/alerts", state=state, limit=max(1, min(limit, 200)))
        return [{k: a[k] for k in ("id", "severity", "rule_name", "message", "state",
                                   "started_at", "resolved_at", "acknowledged_by")} for a in rows]

    @mcp.tool(annotations=ro)
    async def recent_tasks(errors_only: bool = False, guest: str | None = None,
                           cluster: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Letzte Proxmox-Aufgaben (Backups, Migrationen, Start/Stopp …), optional nur Fehler
        oder nur für einen Gast."""
        vmid = (await resolve_guest(client, guest, cluster))["vmid"] if guest else None
        rows = await client.get("/api/v1/tasks", cluster=cluster, vmid=vmid,
                                errors_only=errors_only, limit=max(1, min(limit, 100)))
        return [{"type": t["type"], "target": t["id"], "node": t["node"], "user": t["user"],
                 "start": datetime.fromtimestamp(t["starttime"], UTC).isoformat(),
                 "status": t["status"] or "running"} for t in rows]

    @mcp.tool(annotations=ro)
    async def backup_status(only_problems: bool = True,
                            cluster: str | None = None) -> dict[str, Any]:
        """Backup-Lage je Gast: letztes Backup, Alter, abdeckende Jobs, Snapshots.
        state: ok | old | missing | not_covered | failed_verify."""
        data = await client.get("/api/v1/backups", cluster=cluster)
        rows = [g for g in data["guests"] if not only_problems or g["state"] != "ok"
                or (g["oldest_snapshot_days"] or 0) > 30]
        return {"counts": data["counts"], "max_age_hours": data["max_age_hours"], "guests": [
            {"vmid": g["vmid"], "name": g["name"], "state": g["state"],
             "last_backup": (g["last_backup"] or {}).get("ctime"),
             "age_hours": round(g["age_hours"], 1) if g["age_hours"] is not None else None,
             "jobs": g["covered_by"], "snapshots": g["snapshots"],
             "oldest_snapshot_days": round(g["oldest_snapshot_days"])
             if g["oldest_snapshot_days"] else None} for g in rows]}

    @mcp.tool(annotations=ro)
    async def storage_forecast(cluster: str | None = None) -> list[dict[str, Any]]:
        """Storage-Belegung mit Trend der letzten 14 Tage und Prognose 'voll in N Tagen'."""
        rows = await client.get("/api/v1/storage/forecast", cluster=cluster)
        return [{"storage": f["storage"], "node": f["node"], "cluster": f["cluster"],
                 "used_pct": _pct(f["used"] / f["total"]) if f["total"] else None,
                 "free_gib": _gib(f["total"] - f["used"]),
                 "growth_gib_per_day": _gib(f["growth_per_day"]),
                 "days_until_full": round(f["days_until_full"]) if f["days_until_full"]
                 is not None else None} for f in rows]

    # --- control (scope "control") --------------------------------------------------------

    if "control" not in scopes:
        return mcp

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=False,
                                          open_world_hint=False))
    async def guest_power(guest: str, action: Literal["start", "shutdown", "reboot", "stop",
                                                      "suspend", "resume"],
                          confirm_name: str | None = None,
                          cluster: str | None = None) -> dict[str, Any]:
        """Gast starten/herunterfahren/neu starten/pausieren/fortsetzen/hart stoppen.
        NUR nach ausdrücklicher Zustimmung des Benutzers. action="stop" ist ein harter
        Stopp und verlangt confirm_name = exakter Gast-Name."""
        g = await resolve_guest(client, guest, cluster)
        r = await client.post(f"/api/v1/clusters/{g['cluster']}/guests/{g['vmid']}/power",
                              {"action": action, "confirm": confirm_name})
        return {"guest": f"{g['vmid']} {g['name']}", "action": action, "task": r.get("upid"),
                "dry_run": r.get("dry_run"), "note": r.get("message") or
                "Aufgabe gestartet – Ergebnis mit recent_tasks prüfen"}

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=False, open_world_hint=False))
    async def create_snapshot(guest: str, name: str, description: str = "",
                              cluster: str | None = None) -> dict[str, Any]:
        """Snapshot eines Gasts anlegen (Name: Buchstabe zuerst, A-Z 0-9 _ -).
        Nur nach Zustimmung des Benutzers."""
        g = await resolve_guest(client, guest, cluster)
        r = await client.post(f"/api/v1/clusters/{g['cluster']}/guests/{g['vmid']}/snapshots",
                              {"name": name, "description": description})
        return {"guest": f"{g['vmid']} {g['name']}", "snapshot": name, "task": r.get("upid"),
                "dry_run": r.get("dry_run")}

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=False, open_world_hint=False))
    async def start_backup(guest: str, storage: str | None = None,
                           cluster: str | None = None) -> dict[str, Any]:
        """Sofort-Backup eines Gasts (Snapshot-Modus). Ohne storage wird ein PBS-Storage
        bevorzugt. Nur nach Zustimmung des Benutzers."""
        g = await resolve_guest(client, guest, cluster)
        base = f"/api/v1/clusters/{g['cluster']}/guests/{g['vmid']}"
        options = await client.get(f"{base}/backup-storages")
        if storage is None:
            storage = "pbs" if "pbs" in options else (options[0] if options else None)
        if storage not in options:
            raise ToolError(f"Backup-Storage muss einer von {options} sein")
        r = await client.post(f"{base}/backup", {"storage": storage, "mode": "snapshot"})
        return {"guest": f"{g['vmid']} {g['name']}", "storage": storage, "task": r.get("upid"),
                "dry_run": r.get("dry_run")}

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=True,
                                          open_world_hint=False))
    async def acknowledge_alert(alert_id: int) -> dict[str, Any]:
        """Alarm bestätigen (stoppt Wiederholungs-Benachrichtigungen)."""
        r = await client.post(f"/api/v1/alerts/{alert_id}/ack")
        return {"id": r["id"], "acknowledged_by": r["acknowledged_by"]}

    return mcp


class BearerAuth:
    """ASGI middleware for the HTTP transport: require a shared bearer token."""

    def __init__(self, app: Any, token: str):
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"")
            if not (auth.startswith(b"Bearer ") and hmac.compare_digest(auth[7:], self.token)):
                await send({"type": "http.response.start", "status": 401, "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


async def _create() -> tuple[MCPServer, DashboardClient]:
    client = DashboardClient(os.environ.get("DASHBOARD_URL", "http://localhost:8080"),
                             os.environ.get("DASHBOARD_API_KEY", ""),
                             os.environ.get("DASHBOARD_VERIFY_TLS", "true").lower() != "false")
    who = await client.get("/api/v1/auth/whoami")
    log.info("Dashboard identity: %s (%s), scopes %s", who["name"], who["role"], who["scopes"])
    return build_server(client, who["scopes"]), client


async def amain() -> None:
    # Client, identity check and server share one event loop (httpx pools are loop-bound).
    server, client = await _create()
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    try:
        if transport == "stdio":
            await server.run_stdio_async()
            return
        import uvicorn
        from mcp.server.transport_security import TransportSecuritySettings

        token = os.environ.get("MCP_AUTH_TOKEN", "")
        if len(token) < 24:
            sys.exit("MCP_AUTH_TOKEN (min. 24 Zeichen) ist für MCP_TRANSPORT=http Pflicht")
        hosts = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",")
                 if h.strip()]
        security = TransportSecuritySettings(enable_dns_rebinding_protection=bool(hosts),
                                             allowed_hosts=hosts)
        app = server.streamable_http_app(transport_security=security)
        config = uvicorn.Config(BearerAuth(app, token), host=os.environ.get("MCP_HOST", "0.0.0.0"),
                                port=int(os.environ.get("MCP_PORT", "8765")), log_level="info")
        await uvicorn.Server(config).serve()
    finally:
        await client.aclose()


def main() -> None:
    # stdout belongs to the MCP protocol in stdio mode - log to stderr only.
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(amain())


if __name__ == "__main__":
    main()
