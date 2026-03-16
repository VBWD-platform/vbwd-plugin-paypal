"""Tests for PayPal webhook event-driven verification."""
import json
import pytest
from uuid import uuid4, UUID
from unittest.mock import MagicMock

from flask import Flask

from src.plugins.config_store import PluginConfigEntry
from src.events.payment_events import PaymentCapturedEvent


@pytest.fixture
def mock_container(mocker):
    """Mock DI container."""
    container = mocker.MagicMock()
    container.invoice_repository.return_value = mocker.MagicMock()
    container.subscription_repository.return_value = mocker.MagicMock()
    container.event_dispatcher.return_value = mocker.MagicMock()
    return container


@pytest.fixture
def app(mock_paypal_api, mock_config_store, mock_container, mocker):
    """Flask app with PayPal blueprint."""
    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True

    mocker.patch("src.middleware.auth.AuthService", MagicMock())
    mocker.patch("src.middleware.auth.UserRepository", MagicMock())
    mocker.patch("src.middleware.auth.db", MagicMock())

    from plugins.paypal.routes import paypal_plugin_bp
    flask_app.register_blueprint(
        paypal_plugin_bp, url_prefix="/api/v1/plugins/paypal"
    )
    flask_app.config_store = mock_config_store
    flask_app.container = mock_container
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _make_webhook_call(client, mock_paypal_api, event_payload):
    """Helper to make a verified webhook call."""
    verify_resp = MagicMock()
    verify_resp.status_code = 200
    verify_resp.json.return_value = {"verification_status": "SUCCESS"}
    token_resp = MagicMock()
    token_resp.status_code = 200
    token_resp.json.return_value = {"access_token": "tok", "expires_in": 3600}
    mock_paypal_api.post.side_effect = [token_resp, verify_resp]

    return client.post(
        "/api/v1/plugins/paypal/webhook",
        data=json.dumps(event_payload),
        headers={"PAYPAL-TRANSMISSION-ID": "abc"},
        content_type="application/json",
    )


class TestWebhookEventEmission:
    """Verify that webhook handlers emit correct domain events."""

    def test_emits_payment_captured_event(
        self, client, mock_paypal_api, mock_container
    ):
        """PAYMENT.CAPTURE.COMPLETED should emit PaymentCapturedEvent."""
        invoice_id = str(uuid4())
        mock_invoice = MagicMock()
        mock_invoice.id = UUID(invoice_id)
        mock_invoice.status.value = "PENDING"
        mock_container.invoice_repository.return_value.find_by_id.return_value = (
            mock_invoice
        )

        event_payload = {
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {
                "id": "CAP-EVT-1",
                "custom_id": invoice_id,
                "amount": {"value": "50.00", "currency_code": "EUR"},
            },
        }
        resp = _make_webhook_call(client, mock_paypal_api, event_payload)
        assert resp.status_code == 200

        emit_call = mock_container.event_dispatcher.return_value.emit
        emit_call.assert_called_once()
        event = emit_call.call_args[0][0]
        assert isinstance(event, PaymentCapturedEvent)

    def test_event_correct_invoice_id(
        self, client, mock_paypal_api, mock_container
    ):
        """Emitted event should have correct invoice_id."""
        invoice_id = str(uuid4())
        mock_invoice = MagicMock()
        mock_invoice.id = UUID(invoice_id)
        mock_invoice.status.value = "PENDING"
        mock_container.invoice_repository.return_value.find_by_id.return_value = (
            mock_invoice
        )

        event_payload = {
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {
                "id": "CAP-EVT-2",
                "custom_id": invoice_id,
                "amount": {"value": "25.00", "currency_code": "USD"},
            },
        }
        _make_webhook_call(client, mock_paypal_api, event_payload)

        event = mock_container.event_dispatcher.return_value.emit.call_args[0][0]
        assert str(event.invoice_id) == invoice_id

    def test_event_correct_amount(
        self, client, mock_paypal_api, mock_container
    ):
        """Emitted event should have correct amount."""
        invoice_id = str(uuid4())
        mock_invoice = MagicMock()
        mock_invoice.id = UUID(invoice_id)
        mock_invoice.status.value = "PENDING"
        mock_container.invoice_repository.return_value.find_by_id.return_value = (
            mock_invoice
        )

        event_payload = {
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {
                "id": "CAP-EVT-3",
                "custom_id": invoice_id,
                "amount": {"value": "99.95", "currency_code": "GBP"},
            },
        }
        _make_webhook_call(client, mock_paypal_api, event_payload)

        event = mock_container.event_dispatcher.return_value.emit.call_args[0][0]
        assert event.amount == "99.95"

    def test_event_correct_provider(
        self, client, mock_paypal_api, mock_container
    ):
        """Emitted event should have provider='paypal'."""
        invoice_id = str(uuid4())
        mock_invoice = MagicMock()
        mock_invoice.id = UUID(invoice_id)
        mock_invoice.status.value = "PENDING"
        mock_container.invoice_repository.return_value.find_by_id.return_value = (
            mock_invoice
        )

        event_payload = {
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {
                "id": "CAP-EVT-4",
                "custom_id": invoice_id,
                "amount": {"value": "10.00", "currency_code": "USD"},
            },
        }
        _make_webhook_call(client, mock_paypal_api, event_payload)

        event = mock_container.event_dispatcher.return_value.emit.call_args[0][0]
        assert event.provider == "paypal"

    def test_event_correct_transaction_id(
        self, client, mock_paypal_api, mock_container
    ):
        """Emitted event should have correct transaction_id (capture_id)."""
        invoice_id = str(uuid4())
        mock_invoice = MagicMock()
        mock_invoice.id = UUID(invoice_id)
        mock_invoice.status.value = "PENDING"
        mock_container.invoice_repository.return_value.find_by_id.return_value = (
            mock_invoice
        )

        event_payload = {
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {
                "id": "CAP-TX-99",
                "custom_id": invoice_id,
                "amount": {"value": "5.00", "currency_code": "USD"},
            },
        }
        _make_webhook_call(client, mock_paypal_api, event_payload)

        event = mock_container.event_dispatcher.return_value.emit.call_args[0][0]
        assert event.transaction_id == "CAP-TX-99"

    def test_webhook_no_direct_domain_mutations(
        self, client, mock_paypal_api, mock_container
    ):
        """Webhook handlers should NOT call save on repositories directly."""
        invoice_id = str(uuid4())
        mock_invoice = MagicMock()
        mock_invoice.id = UUID(invoice_id)
        mock_invoice.status.value = "PENDING"
        mock_container.invoice_repository.return_value.find_by_id.return_value = (
            mock_invoice
        )

        event_payload = {
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {
                "id": "CAP-NOSAVE",
                "custom_id": invoice_id,
                "amount": {"value": "10.00", "currency_code": "USD"},
            },
        }
        _make_webhook_call(client, mock_paypal_api, event_payload)

        # No direct saves on invoice or subscription repos
        mock_container.invoice_repository.return_value.save.assert_not_called()
        mock_container.subscription_repository.return_value.save.assert_not_called()
