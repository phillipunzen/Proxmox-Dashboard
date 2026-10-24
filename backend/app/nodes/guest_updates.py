"""Optional: package updates *inside* guests (Debian/Ubuntu only). Off by default.

* VMs: via the QEMU guest agent (``agent/exec``) with the operator token. Proxmox requires
  ``VM.GuestAgent.Unrestricted`` for that - effectively root inside the guest.
* Containers: via the node helper (``pct exec``) over SSH; the helper only accepts a numeric
  VMID and a fixed apt command.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from app import audit
from app.collector.models import GuestView
from app.jobs import JobContext, JobFailed
from app.nodes.service import NodeError, NodeService
from app.pve import PveError, PveUnavailable

APT_ENV = "DEBIAN_FRONTEND=noninteractive LC_ALL=C"
CHECK_CMD = ["sh", "-c", f"{APT_ENV} apt-get update -qq >/dev/null 2>&1; apt list --upgradable"]
UPGRADE_CMD = ["sh", "-c", f"{APT_ENV} apt-get update && {APT_ENV} apt-get -y "
               "-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold "
               "dist-upgrade"]
APT_OS = ("debian", "ubuntu")


def count_upgradable(output: str) -> int:
    return sum(1 for line in output.splitlines() if "[upgradable from:" in line)


class GuestUpdateService:
    def __init__(self, nodes: NodeService, enabled: bool):
        self.nodes = nodes
        self.enabled = enabled
        self.results: dict[tuple[str, int], dict[str, Any]] = {}

    def _guest(self, cluster: str, vmid: int) -> GuestView:
        if not self.enabled:
            raise NodeError(409, "Gast-Updates sind deaktiviert (GUEST_UPDATES=true setzen)")
        if self.nodes.settings.read_only:
            raise NodeError(409, "READ_ONLY=true")
        snap = self.nodes._snap(cluster)
        g = next((x for x in snap.guests if x.vmid == vmid and not x.template), None)
        if g is None:
            raise NodeError(404, f"Gast {vmid} nicht gefunden")
        if g.status != "running":
            raise NodeError(409, f"{g.name} läuft nicht")
        return g

    async def _vm_os(self, cluster: str, g: GuestView) -> str:
        try:
            info = await self.nodes.read[cluster].get(
                f"/nodes/{g.node}/qemu/{g.vmid}/agent/get-osinfo")
        except (PveError, PveUnavailable) as exc:
            raise NodeError(409, f"Guest Agent von {g.name} nicht erreichbar: {exc}") from exc
        result = (info or {}).get("result", {})
        return str(result.get("id") or result.get("name") or "").lower()

    async def _vm_exec(self, cluster: str, g: GuestView, cmd: list[str], timeout: float,
                       ctx: JobContext | None = None) -> tuple[int, str]:
        op = self.nodes.operator.get(cluster)
        if op is None:
            raise NodeError(409, "Kein Operator-Token")
        base = f"/nodes/{g.node}/qemu/{g.vmid}/agent"
        try:
            started = await op.post(f"{base}/exec", {"command": cmd})
            pid = int(started["pid"])
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                await asyncio.sleep(self.nodes.poll_sleep)
                st = await op.get(f"{base}/exec-status", {"pid": pid})
                if st.get("exited"):
                    return int(st.get("exitcode", 1)), str(st.get("out-data", "")) + \
                        str(st.get("err-data", ""))
                if ctx:
                    ctx.log("   … läuft")
        except (PveError, PveUnavailable) as exc:
            msg = str(exc)
            if "403" in msg or "Permission" in msg:
                msg += " – braucht VM.GuestAgent.Unrestricted (pve-setup-roles.sh --guest-updates)"
            raise NodeError(502, msg) from exc
        raise NodeError(504, "Zeitüberschreitung")

    async def _lxc_os(self, cluster: str, g: GuestView) -> str:
        try:
            cfg = await self.nodes.read[cluster].get(f"/nodes/{g.node}/lxc/{g.vmid}/config")
        except (PveError, PveUnavailable) as exc:
            raise NodeError(502, str(exc)) from exc
        return str(cfg.get("ostype", "")).lower()

    async def check(self, cluster: str, vmid: int) -> dict[str, Any]:
        g = self._guest(cluster, vmid)
        if g.type == "qemu":
            os_id = await self._vm_os(cluster, g)
            if os_id not in APT_OS:
                raise NodeError(409, f"Nur Debian/Ubuntu unterstützt (erkannt: {os_id or '?'})")
            code, out = await self._vm_exec(cluster, g, CHECK_CMD, 300)
            count = count_upgradable(out)
        else:
            os_id = await self._lxc_os(cluster, g)
            if os_id not in APT_OS:
                raise NodeError(409, f"Nur Debian/Ubuntu unterstützt (ostype: {os_id or '?'})")
            out = await self.nodes._ssh(cluster, g.node, f"lxc-check {g.vmid}", timeout=300)
            m = re.search(r"(\d+)\s*$", out.strip())
            count = int(m.group(1)) if m else 0
        res = {"cluster": cluster, "vmid": vmid, "name": g.name, "os": os_id, "updates": count,
               "checked_at": time.time()}
        self.results[(cluster, vmid)] = res
        return res

    async def upgrade(self, actor: audit.Actor, cluster: str, vmid: int, confirm: str) -> int:
        g = self._guest(cluster, vmid)
        if confirm != g.name:
            raise NodeError(422, f"Zur Bestätigung den Gast-Namen '{g.name}' eingeben")

        async def run(ctx: JobContext) -> str:
            ctx.log(f"Paket-Upgrade in {g.vmid} {g.name} ({'VM' if g.type == 'qemu' else 'LXC'}"
                    f" auf {g.node})")
            try:
                if g.type == "qemu":
                    os_id = await self._vm_os(cluster, g)
                    if os_id not in APT_OS:
                        raise JobFailed(f"Nur Debian/Ubuntu unterstützt (erkannt: {os_id})")
                    ctx.log("Ausführung über den QEMU Guest Agent – Ausgabe erscheint am Ende.")
                    code, out = await self._vm_exec(cluster, g, UPGRADE_CMD, 3600, ctx)
                    for line in out.splitlines():
                        ctx.log(line)
                    if code != 0:
                        raise JobFailed(f"apt endete mit Exit-Code {code}")
                else:
                    if await self._lxc_os(cluster, g) not in APT_OS:
                        raise JobFailed("Nur Debian/Ubuntu-Container unterstützt")
                    await self.nodes._ssh(cluster, g.node, f"lxc-upgrade {g.vmid}", ctx, 3600)
            except NodeError as exc:
                raise JobFailed(exc.message) from exc
            self.results.pop((cluster, vmid), None)
            return f"{g.name} aktualisiert"

        job = await self.nodes.jobs.start("guest-upgrade", cluster, g.node, actor, run,
                                          params={"vmid": vmid, "name": g.name},
                                          exclusive=False)
        return job.id
