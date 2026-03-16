"""Shared fixtures for PayPal plugin tests."""
import pytest
from unittest.mock import MagicMock

from src.sdk.interface import SDKConfig
from src.plugins.config_store import PluginConfigEntry


@pytest.fixture
def paypal_config():
    """PayPal plugin configuration dict."""
    return {
        "test_client_id": "ATest123",
        "test_client_secret": "secret456",
        "test_webhook_id": "WH-789",
        "sandbox": True,
    }


@pytest.fixture
def sdk_config(paypal_config):
    """SDKConfig instance built from paypal_config."""
    return SDKConfig(
        api_key=paypal_config["test_client_id"],
        api_secret=paypal_config["test_client_secret"],
        sandbox=paypal_config["sandbox"],
    )


@pytest.fixture
def mock_paypal_api(mocker):
    """Mock requests module for PayPal API calls.

    Returns the mock so tests can configure specific responses.
    """
    mock = mocker.patch("plugins.paypal.sdk_adapter.requests")
    # Default: successful OAuth token
    token_resp = mocker.MagicMock()
    token_resp.status_code = 200
    token_resp.json.return_value = {
        "access_token": "test-token",
        "expires_in": 3600,
    }
    mock.post.return_value = token_resp
    mock.get.return_value = token_resp
    return mock


@pytest.fixture
def mock_config_store(mocker, paypal_config):
    """Mock PluginConfigStore with enabled PayPal entry."""
    store = mocker.MagicMock()
    store.get_by_name.return_value = PluginConfigEntry(
        plugin_name="paypal",
        status="enabled",
        config=paypal_config,
    )
    store.get_config.return_value = paypal_config
    return store


@pytest.fixture
def mock_config_store_disabled(mocker):
    """Config store returning disabled PayPal plugin."""
    store = mocker.MagicMock()
    store.get_by_name.return_value = PluginConfigEntry(
        plugin_name="paypal", status="disabled"
    )
    return store
