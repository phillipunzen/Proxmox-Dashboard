from app.config import ClusterConfig
from app.pve.client import PveApi, PveClient, PveError, PveUnavailable
from app.pve.mock import MockPveClient

__all__ = ["PveApi", "PveClient", "PveError", "PveUnavailable", "MockPveClient", "make_client",
           "make_operator_client", "make_nodeadmin_client"]


def make_client(cfg: ClusterConfig) -> PveApi:
    """Return a mock or real client depending on the cluster config."""
    if cfg.mock:
        return MockPveClient(cfg.name)
    return PveClient(cfg)


def make_operator_client(cfg: ClusterConfig, read_client: PveApi) -> PveApi | None:
    """Client with the operator token for write calls; None if no operator token is set.

    The mock shares its state, so the read client is reused there.
    """
    if isinstance(read_client, MockPveClient):
        return read_client
    if not cfg.has_operator_token:
        return None
    return PveClient(cfg.model_copy(update={"token_id": cfg.operator_token_id,
                                            "token_secret": cfg.operator_token_secret}))


def make_nodeadmin_client(cfg: ClusterConfig, read_client: PveApi) -> PveApi | None:
    """Client with the NodeAdmin token (apt, node reboot); None if not configured."""
    if isinstance(read_client, MockPveClient):
        return read_client
    if not cfg.has_nodeadmin_token:
        return None
    return PveClient(cfg.model_copy(update={"token_id": cfg.nodeadmin_token_id,
                                            "token_secret": cfg.nodeadmin_token_secret}))
