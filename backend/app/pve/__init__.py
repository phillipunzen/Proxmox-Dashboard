from app.config import ClusterConfig
from app.pve.client import PveApi, PveClient, PveError, PveUnavailable
from app.pve.mock import MockPveClient

__all__ = ["PveApi", "PveClient", "PveError", "PveUnavailable", "MockPveClient", "make_client"]


def make_client(cfg: ClusterConfig) -> PveApi:
    """Return a mock or real client depending on the cluster config."""
    if cfg.mock:
        return MockPveClient(cfg.name)
    return PveClient(cfg)
