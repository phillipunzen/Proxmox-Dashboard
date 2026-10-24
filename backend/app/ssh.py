"""SSH access to the nodes for what the PVE API cannot do (apt upgrade, DMI, sensors).

Security model (see deploy/node-setup.sh):
* dedicated user ``pve-dashboard`` on each node, key-only login,
* ``authorized_keys`` with ``restrict,command="/usr/local/sbin/pve-dashboard-ssh"``: the key
  can only run that dispatcher, which accepts a fixed list of verbs,
* the dispatcher calls a root-owned helper via a single sudoers rule,
* the dashboard only connects to hosts whose host key an admin has confirmed.

The key pair is generated on first start in ``<data_dir>/ssh/`` and shown in the admin UI.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import asyncssh

log = logging.getLogger(__name__)

VERBS = ("upgrade", "update", "dmi", "sensors", "reboot-required", "version")
# Verbs that take exactly one numeric argument (a container VMID).
VMID_VERBS = ("lxc-check", "lxc-upgrade")


def valid_command(command: str) -> bool:
    parts = command.split(" ")
    if len(parts) == 1:
        return parts[0] in VERBS
    return (len(parts) == 2 and parts[0] in VMID_VERBS and parts[1].isdigit()
            and 100 <= int(parts[1]) <= 999_999_999)
CONNECT_TIMEOUT = 10


class SshError(Exception):
    pass


class HostKeyUnknown(SshError):
    def __init__(self, host: str, fingerprint: str, key: str):
        super().__init__(f"Host-Key von {host} ist noch nicht bestätigt ({fingerprint})")
        self.host = host
        self.fingerprint = fingerprint
        self.key = key


@dataclass
class SshTarget:
    cluster: str
    node: str
    host: str
    port: int
    host_key: str | None  # trusted OpenSSH public key, or None


class SshRunner(Protocol):
    public_key: str

    async def run(self, target: SshTarget, verb: str,
                  on_line: Callable[[str], None] | None = None,
                  timeout: float = 3600) -> tuple[int, str]: ...

    async def scan_host_key(self, host: str, port: int) -> tuple[str, str]: ...


def load_or_create_key(data_dir: str) -> asyncssh.SSHKey:
    path = Path(data_dir) / "ssh" / "id_ed25519"
    if path.exists():
        return asyncssh.read_private_key(str(path))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = asyncssh.generate_private_key("ssh-ed25519", comment="pve-dashboard")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key.export_private_key())
    log.info("Generated SSH key %s", path)
    return key


class AsyncSshRunner:
    def __init__(self, data_dir: str, user: str):
        self.key = load_or_create_key(data_dir)
        self.user = user
        self.public_key = self.key.export_public_key().decode().strip()

    async def scan_host_key(self, host: str, port: int) -> tuple[str, str]:
        """Fetch the server's host key (for the admin to confirm). Returns (key, fingerprint)."""
        try:
            key = await asyncio.wait_for(asyncssh.get_server_host_key(host, port),
                                         CONNECT_TIMEOUT)
        except (OSError, asyncssh.Error, TimeoutError) as exc:
            raise SshError(f"{host}:{port} nicht erreichbar: {exc}") from exc
        if key is None:
            raise SshError(f"{host}: kein Host-Key erhalten")
        return key.export_public_key().decode().strip(), key.get_fingerprint()

    async def run(self, target: SshTarget, verb: str,
                  on_line: Callable[[str], None] | None = None,
                  timeout: float = 3600) -> tuple[int, str]:
        """Run one allowed verb; stream merged stdout/stderr lines. Returns (exit, output)."""
        if not valid_command(verb):
            raise SshError(f"Unbekannter Befehl: {verb}")
        if not target.host_key:
            key, fp = await self.scan_host_key(target.host, target.port)
            raise HostKeyUnknown(target.host, fp, key)
        known = asyncssh.import_known_hosts(
            f"[{target.host}]:{target.port} {target.host_key}\n" if target.port != 22
            else f"{target.host} {target.host_key}\n")
        output: list[str] = []
        try:
            async with asyncio.timeout(timeout):
                async with asyncssh.connect(
                        target.host, port=target.port, username=self.user,
                        client_keys=[self.key], known_hosts=known, agent_path=None,
                        connect_timeout=CONNECT_TIMEOUT) as conn:
                    proc = await conn.create_process(verb, stderr=asyncssh.STDOUT)
                    async for line in proc.stdout:
                        line = line.rstrip("\n")
                        output.append(line)
                        if on_line:
                            on_line(line)
                    await proc.wait()
                    return proc.exit_status or 0, "\n".join(output)
        except asyncssh.HostKeyNotVerifiable as exc:
            raise SshError(f"Host-Key von {target.host} passt NICHT zum bestätigten Schlüssel – "
                           "möglicher Angriff oder Neuinstallation. Bitte prüfen.") from exc
        except asyncssh.PermissionDenied as exc:
            raise SshError(f"Anmeldung an {target.host} abgelehnt – Public Key mit "
                           "deploy/node-setup.sh hinterlegt?") from exc
        except TimeoutError as exc:
            raise SshError(f"Zeitüberschreitung nach {timeout:.0f} s") from exc
        except (OSError, asyncssh.Error) as exc:
            raise SshError(f"SSH zu {target.host}: {exc}") from exc
