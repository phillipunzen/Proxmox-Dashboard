import pytest
from pydantic import ValidationError

from app.config import load_clusters


def test_load_numbered_clusters_in_order():
    env = {
        "PVE_2_NAME": "edge",
        "PVE_2_URL": "https://edge:8006/",
        "PVE_2_TOKEN_ID": "dash@pve!ro",
        "PVE_2_TOKEN_SECRET": "s2",
        "PVE_1_NAME": "homelab",
        "PVE_1_URLS": "https://pve1:8006, https://pve2:8006",
        "PVE_1_TOKEN_ID": "dash@pve!ro",
        "PVE_1_TOKEN_SECRET": "s1",
        "PVE_1_VERIFY_TLS": "false",
        "UNRELATED": "x",
    }
    clusters = load_clusters(env)
    assert [c.name for c in clusters] == ["homelab", "edge"]
    assert clusters[0].urls == ["https://pve1:8006", "https://pve2:8006"]
    assert clusters[0].verify_tls is False
    assert clusters[1].verify_tls is True  # default: verification on
    assert clusters[1].urls == ["https://edge:8006"]  # trailing slash stripped
    assert clusters[0].token_secret.get_secret_value() == "s1"
    assert "s1" not in repr(clusters[0])  # secret never leaks into logs


def test_no_clusters_configured():
    assert load_clusters({}) == []


def test_invalid_url_rejected():
    with pytest.raises(ValidationError):
        load_clusters({"PVE_1_NAME": "x", "PVE_1_URLS": "pve1:8006"})


def test_duplicate_names_rejected():
    env = {"PVE_1_NAME": "a", "PVE_1_URLS": "https://a:8006",
           "PVE_2_NAME": "a", "PVE_2_URLS": "https://b:8006"}
    with pytest.raises(ValueError, match="Duplicate"):
        load_clusters(env)


def test_ui_url_defaults_to_first_api_url():
    c = load_clusters({"PVE_1_NAME": "a", "PVE_1_URLS": "https://a:8006,https://b:8006"})[0]
    assert c.ui_url == "https://a:8006"
