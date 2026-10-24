"""SSH runner for mock mode: simulates the node helper (apt upgrade, DMI, sensors)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

from app.pve.mock import MockPveClient
from app.ssh import HostKeyUnknown, SshError, SshTarget, valid_command

MOCK_HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockHostKeyForDevelopmentOnly000000000000"
MOCK_FP = "SHA256:mockmockmockmockmockmockmockmockmockmockmo"

BOARDS = {"pve1": ("ASUSTeK COMPUTER INC.", "ProArt B550-CREATOR", "3405"),
          "pve2": ("ASRock", "B550M Steel Legend", "P3.40"),
          "pve3": ("Fujitsu", "D3802-A1", "V5.0.0.14 R1.12.0"),
          "edge": ("Default string", "N100DC-ITX", "5.27")}
TEMPS = {"pve1": 54.0, "pve2": 61.0, "pve3": 83.0, "edge": 47.0}


class MockSshRunner:
    public_key = ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockDashboardKey000000000000000000000"
                  "0000 pve-dashboard")

    def __init__(self, mocks: dict[str, MockPveClient], line_delay: float = 0.25):
        self.mocks = mocks
        self.line_delay = line_delay

    async def scan_host_key(self, host: str, port: int) -> tuple[str, str]:
        return MOCK_HOST_KEY, MOCK_FP

    async def run(self, target: SshTarget, verb: str,
                  on_line: Callable[[str], None] | None = None,
                  timeout: float = 3600) -> tuple[int, str]:
        if not valid_command(verb):
            raise SshError(f"Unbekannter Befehl: {verb}")
        if not target.host_key:
            raise HostKeyUnknown(target.host, MOCK_FP, MOCK_HOST_KEY)
        mock = self.mocks.get(target.cluster)
        node = next((n for n in mock.spec.nodes if n.name == target.node), None) if mock else None
        if node is None or not node.online:
            raise SshError(f"SSH zu {target.host}: Connection refused")
        out: list[str] = []

        async def emit(line: str, delay: float | None = None) -> None:
            out.append(line)
            if on_line:
                on_line(line)
            await asyncio.sleep(self.line_delay if delay is None else delay)

        if verb == "dmi":
            vendor, board, bios = BOARDS.get(target.node, ("Unknown", "Unknown", "?"))
            await emit(json.dumps({"board_vendor": vendor, "board_name": board,
                                   "bios_version": bios, "sys_vendor": vendor}), 0)
        elif verb == "sensors":
            t = TEMPS.get(target.node, 50.0)
            await emit(json.dumps({"k10temp-pci-00c3": {"Tctl": {"temp1_input": t}},
                                   "nvme-pci-0100": {"Composite": {"temp1_input": t - 12}}}), 0)
        elif verb == "reboot-required":
            await emit("yes" if mock.actions.reboot_required.get(target.node) else "no", 0)
        elif verb == "version":
            await emit("pve-dashboard-helper 1", 0)
        elif verb.startswith("lxc-"):
            vmid = int(verb.split()[1])
            pending = mock.actions.guest_apt.get(vmid, 0)
            if verb.startswith("lxc-check"):
                await emit(str(pending), 0)
            else:
                await emit(f"pct exec {vmid} -- apt-get update && apt-get -y dist-upgrade")
                await emit("Reading package lists... Done")
                await emit(f"{pending} upgraded, 0 newly installed, 0 to remove")
                for i in range(pending):
                    await emit(f"Setting up libexample{i} (1.{i}.1) ...")
                mock.actions.guest_apt[vmid] = 0
        elif verb in ("update", "upgrade"):
            await emit("Hit:1 http://deb.debian.org/debian trixie InRelease")
            await emit("Hit:2 http://download.proxmox.com/debian/pve trixie InRelease")
            await emit("Reading package lists... Done")
            if verb == "upgrade":
                pkgs = mock.actions.apt.get(target.node, [])
                await emit("Calculating upgrade... Done")
                await emit(f"{len(pkgs)} upgraded, 0 newly installed, 0 to remove and 0 not "
                           "upgraded.")
                for i, (p, _old, new, _) in enumerate(pkgs, start=1):
                    await emit(f"Get:{i} http://download.proxmox.com/debian/pve trixie/"
                               f"pve-no-subscription amd64 {p} amd64 {new} [1.2 MB]")
                for p, old, new, _ in pkgs:
                    await emit(f"Preparing to unpack .../{p}_{new}_amd64.deb ...")
                    await emit(f"Unpacking {p} ({new}) over ({old}) ...")
                for p, _, new, _ in pkgs:
                    await emit(f"Setting up {p} ({new}) ...")
                if any(p.startswith("proxmox-kernel") for p, *_ in pkgs):
                    await emit("Running hook script 'zz-proxmox-boot'..")
                    await emit("Copying and configuring kernels on /dev/disk/by-uuid/1234-ABCD")
                    mock.actions.reboot_required[target.node] = True
                mock.actions.apt[target.node] = []
                await emit("Processing triggers for man-db (2.13.1-1) ...")
        return 0, "\n".join(out)
