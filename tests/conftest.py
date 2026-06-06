"""Shared fixtures for PayPal plugin tests."""
import pytest

from vbwd.sdk.interface import SDKConfig
from vbwd.plugins.config_store import PluginConfigEntry


@pytest.fixture
def published_events():
    """Capture domain-neutral recurring-billing facts published to the bus.

    S50.4: PayPal webhooks no longer call a subscription write port — they
    *publish* generic facts (payment.provider_linked / recurring_charge /
    provider_cancelled / recurring_failed / invoice_failed) and the subscription
    plugin (if enabled) subscribes. The test subscribes a spy and asserts the
    right event name + domain-neutral payload, never a subscription symbol.
    """
    from vbwd.events.bus import event_bus
    from vbwd.plugins.payment_route_helpers import (
        EVENT_PROVIDER_LINKED,
        EVENT_RECURRING_CHARGE,
        EVENT_PROVIDER_CANCELLED,
        EVENT_RECURRING_FAILED,
        EVENT_INVOICE_FAILED,
    )

    captured: list[tuple[str, dict]] = []

    def _spy(event_name, data):
        captured.append((event_name, data))

    names = [
        EVENT_PROVIDER_LINKED,
        EVENT_RECURRING_CHARGE,
        EVENT_PROVIDER_CANCELLED,
        EVENT_RECURRING_FAILED,
        EVENT_INVOICE_FAILED,
    ]
    for name in names:
        event_bus.subscribe(name, _spy)
    yield captured
    for name in names:
        event_bus.unsubscribe(name, _spy)


@pytest.fixture
def recurring_registry():
    """Line-item registry carrying a fake handler that reports a line item as
    recurring iff the test attached a ``_recurring_spec`` to it (the seam
    ``determine_session_mode`` / ``_get_or_create_paypal_plan`` now use). Saves
    and restores the singleton's handlers so global state is untouched."""
    from vbwd.events.line_item_registry import (
        line_item_registry,
        ILineItemHandler,
        LineItemResult,
    )

    class _FakeRecurringHandler(ILineItemHandler):
        def can_handle_line_item(self, line_item, context):
            return True

        def activate_line_item(self, line_item, context):
            return LineItemResult.skip()

        def reverse_line_item(self, line_item, context):
            return LineItemResult.skip()

        def restore_line_item(self, line_item, context):
            return LineItemResult.skip()

        def is_recurring_line_item(self, line_item):
            return getattr(line_item, "_recurring_spec", None) is not None

        def recurring_billing_spec(self, line_item):
            return getattr(line_item, "_recurring_spec", None)

    saved = line_item_registry.handlers
    line_item_registry.clear()
    line_item_registry.register(_FakeRecurringHandler())
    yield line_item_registry
    line_item_registry.clear()
    for handler in saved:
        line_item_registry.register(handler)


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
    mock = mocker.patch("plugins.paypal.paypal.sdk_adapter.requests")
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
