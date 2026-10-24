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
    # Second token with the Dashboard-Operator role. Without it, control actions are disabled.
    operator_token_id: str = ""
    operator_token_secret: SecretStr = SecretStr("")
    # Third token with Dashboard-NodeAdmin (Sys.Modify, Sys.PowerMgmt): update list, apt update,
    # node reboot. Without it, node actions are disabled.
    nodeadmin_token_id: str = ""
    nodeadmin_token_secret: SecretStr = SecretStr("")
    # SSH address per node if it differs from the cluster IP: {"pve1": "10.0.0.11"}
    ssh_hosts: dict[str, str] = {}
    mock: bool = False

    @property
    def has_nodeadmin_token(self) -> bool:
        return self.mock or bool(self.nodeadmin_token_id and
                                 self.nodeadmin_token_secret.get_secret_value())

    @property
    def has_operator_token(self) -> bool:
        return self.mock or bool(self.operator_token_id and
                                 self.operator_token_secret.get_secret_value())

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
    # Global safety switch: no control actions at all.
    read_only: bool = True
    # Control actions are validated and written to the audit log, but not sent to Proxmox.
    dry_run: bool = False
    log_level: str = "INFO"
    # Directory containing the built SvelteKit SPA. Empty/nonexistent = API only.
    static_dir: str = "/app/static"
    # VictoriaMetrics base URL for history. Empty = no history (live data only).
    vm_url: str = ""
    # Mock mode: write 30 days of synthetic history into an empty TSDB on startup.
    mock_backfill: bool = True
    # Seconds between hardware/config inventory refreshes (disks, NICs, PCI, guest configs).
    inventory_interval: float = Field(default=900.0, ge=60.0, le=86400.0)

    # --- persistence & auth ---------------------------------------------------------------------
    # Users, sessions and the audit log (SQLite) live here; mount it as a volume.
    data_dir: str = "/data"
    # Encrypts TOTP secrets at rest. Empty = generated once and stored in <data_dir>/secret.key.
    app_secret_key: SecretStr = SecretStr("")
    # First start without users: create this admin. Empty password = random, printed to the log.
    admin_username: str = "admin"
    admin_password: SecretStr = SecretStr("")
    # Roles that must use TOTP (comma separated; empty = optional for everyone).
    totp_required_roles: str = "admin,operator"
    session_max_hours: float = Field(default=12.0, ge=0.25, le=720.0)
    session_idle_minutes: float = Field(default=120.0, ge=5.0, le=10080.0)
    # "auto" = Secure cookie when the request arrived via HTTPS (also behind a reverse proxy).
    cookie_secure: str = Field(default="auto", pattern="^(auto|true|false)$")
    # /metrics without authentication (only if the port is not reachable from untrusted nets).
    metrics_public: bool = False

    # --- node management (phase 4) ------------------------------------------------------------
    ssh_user: str = "pve-dashboard"
    ssh_port: int = Field(default=22, ge=1, le=65535)
    # Seconds between reading the list of available updates (Proxmox refreshes it daily).
    update_check_interval: float = Field(default=21600.0, ge=300.0)
    # Seconds between temperature reads via SSH (0 = off).
    sensors_interval: float = Field(default=60.0, ge=0.0)
    # Max. seconds to wait for a node to come back after a reboot.
    reboot_timeout: float = Field(default=900.0, ge=60.0)

    # --- alerting -------------------------------------------------------------------------------
    # Minimum seconds between two rule evaluations.
    alert_eval_interval: float = Field(default=10.0, ge=1.0, le=600.0)
    # Public URL of the dashboard, used for links in notifications (e.g. https://pve-dash.lan).
    dashboard_url: str = ""

    # --- OIDC single sign-on (optional, e.g. Authentik, Keycloak, Cloudflare Access) ------------
    # Issuer URL, e.g. https://auth.example.com/application/o/pve-dashboard/ - empty = disabled.
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: SecretStr = SecretStr("")
    oidc_scopes: str = "openid profile email groups"
    oidc_username_claim: str = "preferred_username"
    oidc_groups_claim: str = "groups"
    # Comma-separated IdP groups per dashboard role. Empty viewer list = every IdP user may view.
    oidc_admin_groups: str = ""
    oidc_operator_groups: str = ""
    oidc_viewer_groups: str = ""
    oidc_button_label: str = "Mit SSO anmelden"
    # Callback URL registered at the IdP. Empty = DASHBOARD_URL + /api/v1/auth/oidc/callback.
    oidc_redirect_url: str = ""
    # Allow an IdP login to take over an existing *local* user with the same name.
    oidc_link_existing: bool = False

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_id)

    # Package updates inside guests (QGA for VMs, pct exec for LXC). Debian/Ubuntu only.
    guest_updates: bool = False

    # --- backups (phase 5) ----------------------------------------------------------------------
    # A guest counts as "backed up" if its newest backup is younger than this.
    backup_max_age_hours: float = Field(default=48.0, ge=1.0)
    # Seconds between reading backup lists and snapshots.
    backup_refresh_interval: float = Field(default=900.0, ge=60.0)

    @property
    def totp_roles(self) -> set[str]:
        return {r.strip() for r in self.totp_required_roles.split(",") if r.strip()}


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
                operator_token_id=g.get("OPERATOR_TOKEN_ID", ""),
                operator_token_secret=SecretStr(g.get("OPERATOR_TOKEN_SECRET", "")),
                nodeadmin_token_id=g.get("NODEADMIN_TOKEN_ID", ""),
                nodeadmin_token_secret=SecretStr(g.get("NODEADMIN_TOKEN_SECRET", "")),
                ssh_hosts=_pairs(g.get("SSH_HOSTS", "")),
            )
        )

    names = [c.name for c in clusters]
    if len(names) != len(set(names)):
        raise ValueError(f"Duplicate cluster names in PVE_<n>_NAME: {names}")
    return clusters


def _pairs(value: str) -> dict[str, str]:
    """``pve1=10.0.0.11,pve2=10.0.0.12`` -> dict."""
    out: dict[str, str] = {}
    for part in value.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def mock_clusters() -> list[ClusterConfig]:
    """Two synthetic clusters: a 3-node cluster and a standalone host."""
    return [
        ClusterConfig(name="homelab", urls=["https://mock-homelab:8006"], mock=True),
        ClusterConfig(name="standalone", urls=["https://mock-standalone:8006"], mock=True),
    ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
