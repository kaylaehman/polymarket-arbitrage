"""
Conftest for directional tests.

Sets minimal env vars so load_config("config.yaml") passes validation
in the dev venv (where live credentials are not present).
The values are test-only dummies — they are never used to call any API.
"""
import os
import pytest


@pytest.fixture(autouse=True, scope="session")
def _patch_kalshi_env():
    """Inject dummy Kalshi creds so config validation passes in dev/CI."""
    overrides = {
        "KALSHI_API_KEY_ID": "00000000-test-0000-0000-000000000000",
        "KALSHI_PRIVATE_KEY": (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEpAIBAAKCAQEA0000000000000000000000000000000000000000000000\n"
            "-----END RSA PRIVATE KEY-----\n"
        ),
    }
    originals = {k: os.environ.get(k) for k in overrides}
    for k, v in overrides.items():
        if not os.environ.get(k):  # don't override real creds
            os.environ[k] = v
    yield
    for k, orig in originals.items():
        if orig is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = orig
