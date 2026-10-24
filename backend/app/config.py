"""Application configuration.

All settings come from environment variables (usually via ``.env`` and docker-compose).
Secrets never live in code.

Clusters are configured as numbered groups so that several clusters/standalone hosts
can be listed without JSON-in-env gymnastics::

    PVE_1_NAME=homelab
    PVE_1_URLS=https://pve1:8006,https://pve2:8006   # several URLs = failover
    PVE_1_TOKEN_ID=dashboard@pve!readonly
    PVE_1_TOKEN_SECRET=xxxxxxxx-xxxx-...
    PVE_1_VERIFY_TLS=true
    PVE_1_CA_FILE=/certs/pve-root-ca.pem               # optional

Numbering may have gaps; groups are processed in ascending order.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from functools import lru_cache

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_CLUSTER_KEY = re.compile(r"^PVE_(\d+)_([A-Z_]+)$")
_TRUE = {"1", "true", "yes", "on"}


class ClusterConfig(BaseModel):
    """Connection settings for one Proxmox VE cluster or standalone host."""

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
    urls: list[str] = Field(min_length=1)
    token_id: str = ""
    token_secret: SecretStr = SecretStr("")
    verify_tls: bool = True
    ca_file: str | None = None
    timeout: float = 10.0
    # Base URL of the Proxmox web UI used for console deep links. Defaults to urls[0].
    web_url: str | None = None
    mock: bool = False

    @field_validator("urls")
    @classmethod
    def _strip_urls(cls, urls: list[str]) -> list[str]:
        cleaned = [u.strip().rstrip("/") for u in urls if u.strip()]
        for u in cleaned:
            if not u.startswith(("https://", "http://")):
                raise ValueError(f"URL must start with https:// or http://: {u}")
        return cleaned

    @property
    def ui_url(self) -> str:
        return (self.web_url or self.urls[0]).rstrip("/")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Use synthetic data instead of a real cluster (frontend development, demos, tests).
    pve_mock: bool = False
    # Fast poll: /cluster/resources + /cluster/status (seconds).
    poll_interval: float = Field(default=10.0, ge=2.0, le=300.0)
    # Slow poll: per-node /nodes/{node}/status for kernel, load average, swap ... (seconds).
    node_status_interval: float = Field(default=30.0, ge=5.0, le=3600.0)
    # Global safety switch. Phase 1 is read-only regardless; kept for later phases.
    read_only: bool = True
    log_level: str = "INFO"
    # Directory containing the built SvelteKit SPA. Empty/nonexistent = API only.
    static_dir: str = "/app/static"
    # VictoriaMetrics base URL for history. Empty = no history (live data only).
    vm_url: str = ""
    # Mock mode: write 30 days of synthetic history into an empty TSDB on startup.
    mock_backfill: bool = True
    # Seconds between hardware/config inventory refreshes (disks, NICs, PCI, guest configs).
    inventory_interval: float = Field(default=900.0, ge=60.0, le=86400.0)


def load_clusters(environ: Mapping[str, str] | None = None) -> list[ClusterConfig]:
    """Parse ``PVE_<n>_<FIELD>`` variables into cluster configs."""
    env = os.environ if environ is None else environ
    groups: dict[int, dict[str, str]] = {}
    for key, value in env.items():
        m = _CLUSTER_KEY.match(key)
        if m:
            groups.setdefault(int(m.group(1)), {})[m.group(2)] = value

    clusters: list[ClusterConfig] = []
    for idx in sorted(groups):
        g = groups[idx]
        urls = g.get("URLS") or g.get("URL") or ""
        clusters.append(
            ClusterConfig(
                name=g.get("NAME") or f"cluster{idx}",
                urls=urls.split(","),
                token_id=g.get("TOKEN_ID", ""),
                token_secret=SecretStr(g.get("TOKEN_SECRET", "")),
                verify_tls=g.get("VERIFY_TLS", "true").strip().lower() in _TRUE,
                ca_file=g.get("CA_FILE") or None,
                timeout=float(g.get("TIMEOUT", "10")),
                web_url=g.get("WEB_URL") or None,
            )
        )

    names = [c.name for c in clusters]
    if len(names) != len(set(names)):
        raise ValueError(f"Duplicate cluster names in PVE_<n>_NAME: {names}")
    return clusters


def mock_clusters() -> list[ClusterConfig]:
    """Two synthetic clusters: a 3-node cluster and a standalone host."""
    return [
        ClusterConfig(name="homelab", urls=["https://mock-homelab:8006"], mock=True),
        ClusterConfig(name="standalone", urls=["https://mock-standalone:8006"], mock=True),
    ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
