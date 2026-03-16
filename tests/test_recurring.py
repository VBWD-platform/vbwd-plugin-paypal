"""Tests for PayPal recurring billing (subscription) flows."""
import json
import pytest
from decimal import Decimal
from uuid import uuid4, UUID
from unittest.mock import MagicMock, PropertyMock

from flask import Flask

from src.plugins.config_store import PluginConfigEntry
from src.models.enums import InvoiceStatus, LineItemType
from src.events.payment_events import (
    PaymentCapturedEvent,
    PaymentFailedEvent,
    SubscriptionCancelledEvent,
)


@pytest.fixture
def mock_container(mocker):
    """Mock DI container."""
    container = mocker.MagicMock()
    container.invoice_repository.return_value = mocker.MagicMock()
    container.subscription_repository.return_value = mocker.MagicMock()
    container.user_repository.return_value = mocker.MagicMock()
    container.event_dispatcher.return_value = mocker.MagicMock()
    return container


@pytest.fixture
def app(mock_paypal_api, mock_config_store, mock_container, mocker):
    """Flask app with PayPal blueprint."""
    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True

    user_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    mock_auth_service = MagicMock()
    mock_auth_service.return_value.verify_token.return_value = str(user_id)
    mocker.patch("src.middleware.auth.AuthService", mock_auth_service)

    mock_user = MagicMock()
    mock_user.id = user_id
    mock_user.status.value = "ACTIVE"
    mock_user_repo = MagicMock()
    mock_user_repo.return_value.find_by_id.return_value = mock_user
    mocker.patch("src.middleware.auth.UserRepository", mock_user_repo)
    mocker.patch("src.middleware.auth.db", MagicMock())

    from plugins.paypal.routes import paypal_plugin_bp

    flask_app.register_blueprint(paypal_plugin_bp, url_prefix="/api/v1/plugins/paypal")
    flask_app.config_store = mock_config_store
    flask_app.container = mock_container
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer test_token_123"}


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


class TestDetermineSessionMode:
    """Test determine_session_mode via shared helpers."""

    def test_determine_mode_one_time(self, app):
        """Should return 'payment' for non-recurring invoice."""
        with app.app_context():
            from src.plugins.payment_route_helpers import determine_session_mode

            invoice = MagicMock()
            invoice.line_items = []
            assert determine_session_mode(invoice) == "payment"


class TestSubscriptionActivated:
    """Test BILLING.SUBSCRIPTION.ACTIVATED webhook handling."""

    def test_webhook_subscription_activated_links(
        self, client, mock_paypal_api, mock_container
    ):
        """Should store provider_subscription_id on our Subscription model."""
        invoice_id = str(uuid4())
        sub_id = uuid4()

        mock_invoice = MagicMock()
        mock_invoice.id = UUID(invoice_id)
        mock_li = MagicMock()
        mock_li.item_type = LineItemType.SUBSCRIPTION
        mock_li.item_id = sub_id
        mock_invoice.line_items = [mock_li]
        mock_container.invoice_repository.return_value.find_by_id.return_value = (
            mock_invoice
        )

        mock_subscription = MagicMock()
        mock_subscription.id = sub_id
        mock_container.subscription_repository.return_value.find_by_id.return_value = (
            mock_subscription
        )

        event_payload = {
            "event_type": "BILLING.SUBSCRIPTION.ACTIVATED",
            "resource": {
                "id": "I-SUB-ACTIVATED",
                "custom_id": invoice_id,
                "billing_info": {
                    "last_payment": {
                        "amount": {"value": "9.99", "currency_code": "USD"}
                    }
                },
            },
        }
        resp = _make_webhook_call(client, mock_paypal_api, event_payload)
        assert resp.status_code == 200

        # Verify provider_subscription_id was set
        assert mock_subscription.provider_subscription_id == "I-SUB-ACTIVATED"
        mock_container.subscription_repository.return_value.save.assert_called_once()

    def test_webhook_subscription_activated_emits(
        self, client, mock_paypal_api, mock_container
    ):
        """Should emit PaymentCapturedEvent on subscription activation."""
        invoice_id = str(uuid4())
        mock_invoice = MagicMock()
        mock_invoice.id = UUID(invoice_id)
        mock_invoice.line_items = []
        mock_container.invoice_repository.return_value.find_by_id.return_value = (
            mock_invoice
        )

        event_payload = {
            "event_type": "BILLING.SUBSCRIPTION.ACTIVATED",
            "resource": {
                "id": "I-SUB-EMT",
                "custom_id": invoice_id,
                "billing_info": {
                    "last_payment": {
                        "amount": {"value": "19.99", "currency_code": "EUR"}
                    }
                },
            },
        }
        resp = _make_webhook_call(client, mock_paypal_api, event_payload)
        assert resp.status_code == 200

        emit_call = mock_container.event_dispatcher.return_value.emit
        emit_call.assert_called_once()
        event = emit_call.call_args[0][0]
        assert isinstance(event, PaymentCapturedEvent)
        assert event.provider == "paypal"


class TestSaleCompleted:
    """Test PAYMENT.SALE.COMPLETED webhook (subscription renewal)."""

    def test_webhook_sale_completed_creates_renewal(
        self, client, mock_paypal_api, mock_container
    ):
        """Should create renewal invoice for subscription payment."""
        sub_id = uuid4()
        mock_subscription = MagicMock()
        mock_subscription.id = sub_id
        mock_subscription.user_id = uuid4()
        mock_plan = MagicMock()
        mock_plan.id = uuid4()
        mock_plan.name = "Pro Plan"
        mock_subscription.tarif_plan = mock_plan
        mock_container.subscription_repository.return_value.find_by_provider_subscription_id.return_value = (
            mock_subscription
        )
        mock_container.invoice_repository.return_value.find_by_provider_session_id.return_value = (
            None
        )

        event_payload = {
            "event_type": "PAYMENT.SALE.COMPLETED",
            "resource": {
                "id": "SALE-REN-1",
                "billing_agreement_id": "I-SUB-RENEW",
                "amount": {"total": "9.99", "currency": "USD"},
            },
        }
        resp = _make_webhook_call(client, mock_paypal_api, event_payload)
        assert resp.status_code == 200

        # Verify renewal invoice was created
        mock_container.invoice_repository.return_value.save.assert_called_once()
        mock_container.event_dispatcher.return_value.emit.assert_called_once()

    def test_webhook_sale_completed_deduplication(
        self, client, mock_paypal_api, mock_container
    ):
        """Should not create duplicate invoice for same sale_id."""
        mock_subscription = MagicMock()
        mock_subscription.id = uuid4()
        mock_container.subscription_repository.return_value.find_by_provider_subscription_id.return_value = (
            mock_subscription
        )

        existing_invoice = MagicMock()
        existing_invoice.id = uuid4()
        mock_container.invoice_repository.return_value.find_by_provider_session_id.return_value = (
            existing_invoice
        )

        event_payload = {
            "event_type": "PAYMENT.SALE.COMPLETED",
            "resource": {
                "id": "SALE-DUP-1",
                "billing_agreement_id": "I-SUB-DUP",
                "amount": {"total": "9.99", "currency": "USD"},
            },
        }
        resp = _make_webhook_call(client, mock_paypal_api, event_payload)
        assert resp.status_code == 200

        # Invoice repo save NOT called (deduplication)
        mock_container.invoice_repository.return_value.save.assert_not_called()


class TestSubscriptionCancelled:
    """Test BILLING.SUBSCRIPTION.CANCELLED webhook."""

    def test_webhook_subscription_cancelled(
        self, client, mock_paypal_api, mock_container
    ):
        """Should emit SubscriptionCancelledEvent."""
        sub_id = uuid4()
        user_id = uuid4()
        mock_subscription = MagicMock()
        mock_subscription.id = sub_id
        mock_subscription.user_id = user_id
        mock_container.subscription_repository.return_value.find_by_provider_subscription_id.return_value = (
            mock_subscription
        )

        event_payload = {
            "event_type": "BILLING.SUBSCRIPTION.CANCELLED",
            "resource": {"id": "I-SUB-CANCEL"},
        }
        resp = _make_webhook_call(client, mock_paypal_api, event_payload)
        assert resp.status_code == 200

        emit_call = mock_container.event_dispatcher.return_value.emit
        emit_call.assert_called_once()
        event = emit_call.call_args[0][0]
        assert isinstance(event, SubscriptionCancelledEvent)
        assert event.reason == "paypal_subscription_cancelled"


class TestPaymentFailed:
    """Test BILLING.SUBSCRIPTION.PAYMENT.FAILED webhook."""

    def test_webhook_payment_failed(self, client, mock_paypal_api, mock_container):
        """Should emit PaymentFailedEvent."""
        mock_subscription = MagicMock()
        mock_subscription.id = uuid4()
        mock_subscription.user_id = uuid4()
        mock_container.subscription_repository.return_value.find_by_provider_subscription_id.return_value = (
            mock_subscription
        )

        event_payload = {
            "event_type": "BILLING.SUBSCRIPTION.PAYMENT.FAILED",
            "resource": {"id": "I-SUB-FAIL"},
        }
        resp = _make_webhook_call(client, mock_paypal_api, event_payload)
        assert resp.status_code == 200

        emit_call = mock_container.event_dispatcher.return_value.emit
        emit_call.assert_called_once()
        event = emit_call.call_args[0][0]
        assert isinstance(event, PaymentFailedEvent)
        assert event.provider == "paypal"


class TestCaptureOrderEmitsEvent:
    """Test that capture-order route emits PaymentCapturedEvent."""

    def test_capture_order_emits_event(
        self, client, auth_headers, mock_paypal_api, mock_container
    ):
        """POST /capture-order should emit PaymentCapturedEvent on success."""
        invoice_id = str(uuid4())
        capture_resp = MagicMock()
        capture_resp.status_code = 201
        capture_resp.json.return_value = {
            "id": "ORDER-CAP-REC",
            "status": "COMPLETED",
            "purchase_units": [
                {
                    "payments": {
                        "captures": [
                            {
                                "id": "CAP-REC-1",
                                "amount": {"value": "49.99", "currency_code": "USD"},
                            }
                        ]
                    }
                }
            ],
        }
        status_resp = MagicMock()
        status_resp.status_code = 200
        status_resp.json.return_value = {
            "status": "COMPLETED",
            "purchase_units": [
                {
                    "amount": {"value": "49.99", "currency_code": "USD"},
                    "custom_id": invoice_id,
                }
            ],
        }
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"access_token": "tok", "expires_in": 3600}
        mock_paypal_api.post.side_effect = [token_resp, capture_resp]
        mock_paypal_api.get.return_value = status_resp

        resp = client.post(
            "/api/v1/plugins/paypal/capture-order",
            json={"order_id": "ORDER-CAP-REC"},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        emit_call = mock_container.event_dispatcher.return_value.emit
        emit_call.assert_called_once()
        event = emit_call.call_args[0][0]
        assert isinstance(event, PaymentCapturedEvent)
        assert str(event.invoice_id) == invoice_id


class TestBillingPeriodToPaypalDaily:
    """Tests that daily billing period maps to DAY interval in PayPal."""

    def test_daily_period_in_mapping(self):
        from plugins.paypal.routes import BILLING_PERIOD_TO_PAYPAL

        assert "daily" in BILLING_PERIOD_TO_PAYPAL
        assert BILLING_PERIOD_TO_PAYPAL["daily"]["interval_unit"] == "DAY"
        assert BILLING_PERIOD_TO_PAYPAL["daily"]["interval_count"] == 1
