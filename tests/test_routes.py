"""Tests for PayPal plugin API routes."""
import json
import pytest
from decimal import Decimal
from uuid import uuid4, UUID
from unittest.mock import MagicMock, patch

from flask import Flask

from src.plugins.config_store import PluginConfigEntry
from src.models.enums import InvoiceStatus


@pytest.fixture
def mock_container(mocker):
    """Mock DI container with repositories and event dispatcher."""
    container = mocker.MagicMock()
    container.invoice_repository.return_value = mocker.MagicMock()
    container.subscription_repository.return_value = mocker.MagicMock()
    container.user_repository.return_value = mocker.MagicMock()
    container.addon_subscription_repository.return_value = mocker.MagicMock()
    container.event_dispatcher.return_value = mocker.MagicMock()
    return container


@pytest.fixture
def app(mock_paypal_api, mock_config_store, mock_container, mocker):
    """Create Flask app with PayPal blueprint registered."""
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

    mock_db = MagicMock()
    mocker.patch("src.middleware.auth.db", mock_db)

    from plugins.paypal.routes import paypal_plugin_bp

    flask_app.register_blueprint(
        paypal_plugin_bp, url_prefix="/api/v1/plugins/paypal"
    )

    flask_app.config_store = mock_config_store
    flask_app.container = mock_container

    return flask_app


@pytest.fixture
def client(app):
    """Flask test client."""
    return app.test_client()


@pytest.fixture
def auth_headers():
    """Auth headers with a dummy bearer token."""
    return {"Authorization": "Bearer test_token_123"}


# ---- create-order tests ----


class TestCreateOrder:
    """Tests for POST /api/v1/plugins/paypal/create-order."""

    def test_create_order_requires_auth(self, client):
        """Should return 401 without Authorization header."""
        resp = client.post(
            "/api/v1/plugins/paypal/create-order",
            json={"invoice_id": str(uuid4())},
        )
        assert resp.status_code == 401

    def test_create_order_plugin_disabled(
        self, client, app, mock_config_store_disabled, auth_headers
    ):
        """Should return 404 when plugin is disabled."""
        app.config_store = mock_config_store_disabled
        resp = client.post(
            "/api/v1/plugins/paypal/create-order",
            json={"invoice_id": str(uuid4())},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_create_order_missing_invoice_id(self, client, auth_headers, mock_container):
        """Should return 400 when invoice_id is missing."""
        mock_container.invoice_repository.return_value.find_by_id.return_value = None
        resp = client.post(
            "/api/v1/plugins/paypal/create-order",
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_create_order_invoice_not_found(self, client, auth_headers, mock_container):
        """Should return 404 when invoice does not exist."""
        mock_container.invoice_repository.return_value.find_by_id.return_value = None
        resp = client.post(
            "/api/v1/plugins/paypal/create-order",
            json={"invoice_id": str(uuid4())},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_create_order_invoice_not_pending(self, client, auth_headers, mock_container):
        """Should return 400 when invoice status is not pending."""
        invoice = MagicMock()
        invoice.id = uuid4()
        invoice.status = InvoiceStatus.PAID
        invoice.user_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        mock_container.invoice_repository.return_value.find_by_id.return_value = invoice

        resp = client.post(
            "/api/v1/plugins/paypal/create-order",
            json={"invoice_id": str(invoice.id)},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_create_order_wrong_user(self, client, auth_headers, mock_container):
        """Should return 403 when invoice belongs to a different user."""
        invoice = MagicMock()
        invoice.id = uuid4()
        invoice.status = InvoiceStatus.PENDING
        invoice.user_id = uuid4()
        mock_container.invoice_repository.return_value.find_by_id.return_value = invoice

        resp = client.post(
            "/api/v1/plugins/paypal/create-order",
            json={"invoice_id": str(invoice.id)},
            headers=auth_headers,
        )
        assert resp.status_code == 403

    def test_create_order_success(
        self, client, auth_headers, mock_container, mock_paypal_api
    ):
        """Should return 200 with session_id and session_url on success."""
        user_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        invoice = MagicMock()
        invoice.id = uuid4()
        invoice.status = InvoiceStatus.PENDING
        invoice.user_id = user_id
        invoice.total_amount = Decimal("29.99")
        invoice.amount = Decimal("29.99")
        invoice.currency = "USD"
        invoice.line_items = []
        mock_container.invoice_repository.return_value.find_by_id.return_value = invoice

        # Mock PayPal Order creation
        order_resp = MagicMock()
        order_resp.status_code = 201
        order_resp.json.return_value = {
            "id": "ORDER-PP-123",
            "links": [
                {"rel": "approve", "href": "https://paypal.com/approve/ORDER-PP-123"},
            ],
        }
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"access_token": "tok", "expires_in": 3600}
        mock_paypal_api.post.side_effect = [token_resp, order_resp]

        resp = client.post(
            "/api/v1/plugins/paypal/create-order",
            json={"invoice_id": str(invoice.id)},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["session_id"] == "ORDER-PP-123"
        assert "session_url" in data


# ---- capture-order tests ----


class TestCaptureOrder:
    """Tests for POST /api/v1/plugins/paypal/capture-order."""

    def test_capture_order_requires_auth(self, client):
        """Should return 401 without Authorization header."""
        resp = client.post(
            "/api/v1/plugins/paypal/capture-order",
            json={"order_id": "ORDER-123"},
        )
        assert resp.status_code == 401

    def test_capture_order_missing_order_id(self, client, auth_headers):
        """Should return 400 when order_id is missing."""
        resp = client.post(
            "/api/v1/plugins/paypal/capture-order",
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    def test_capture_order_success(
        self, client, auth_headers, mock_container, mock_paypal_api
    ):
        """Should return 200 and emit PaymentCapturedEvent on success."""
        capture_resp = MagicMock()
        capture_resp.status_code = 201
        capture_resp.json.return_value = {
            "id": "ORDER-CAP",
            "status": "COMPLETED",
            "purchase_units": [{
                "payments": {
                    "captures": [{
                        "id": "CAP-789",
                        "amount": {"value": "29.99", "currency_code": "USD"},
                    }]
                }
            }],
        }
        status_resp = MagicMock()
        status_resp.status_code = 200
        status_resp.json.return_value = {
            "status": "COMPLETED",
            "purchase_units": [{
                "amount": {"value": "29.99", "currency_code": "USD"},
                "custom_id": str(uuid4()),
            }],
        }
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"access_token": "tok", "expires_in": 3600}
        mock_paypal_api.post.side_effect = [token_resp, capture_resp]
        mock_paypal_api.get.return_value = status_resp

        resp = client.post(
            "/api/v1/plugins/paypal/capture-order",
            json={"order_id": "ORDER-CAP"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "COMPLETED"
        assert data["capture_id"] == "CAP-789"
        mock_container.event_dispatcher.return_value.emit.assert_called_once()


# ---- webhook tests ----


class TestWebhook:
    """Tests for POST /api/v1/plugins/paypal/webhook."""

    def test_webhook_invalid_signature(self, client, mock_paypal_api):
        """Should return 400 when PayPal signature verification fails."""
        verify_resp = MagicMock()
        verify_resp.status_code = 200
        verify_resp.json.return_value = {"verification_status": "FAILURE"}
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"access_token": "tok", "expires_in": 3600}
        mock_paypal_api.post.side_effect = [token_resp, verify_resp]

        resp = client.post(
            "/api/v1/plugins/paypal/webhook",
            data=json.dumps({"event_type": "test"}),
            headers={
                "PAYPAL-TRANSMISSION-ID": "abc",
                "PAYPAL-TRANSMISSION-SIG": "sig",
            },
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_webhook_plugin_disabled(self, client, app, mock_config_store_disabled):
        """Should return 404 when plugin is disabled."""
        app.config_store = mock_config_store_disabled
        resp = client.post(
            "/api/v1/plugins/paypal/webhook",
            data=b"raw_body",
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_webhook_capture_completed(self, client, mock_paypal_api, mock_container):
        """Should return 200 and emit event for PAYMENT.CAPTURE.COMPLETED."""
        invoice_id = str(uuid4())
        event_payload = {
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {
                "id": "CAP-WH-123",
                "custom_id": invoice_id,
                "amount": {"value": "29.99", "currency_code": "USD"},
            },
        }

        # Mock invoice lookup
        mock_invoice = MagicMock()
        mock_invoice.id = UUID(invoice_id)
        mock_invoice.status.value = "PENDING"
        mock_container.invoice_repository.return_value.find_by_id.return_value = mock_invoice

        verify_resp = MagicMock()
        verify_resp.status_code = 200
        verify_resp.json.return_value = {"verification_status": "SUCCESS"}
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"access_token": "tok", "expires_in": 3600}
        mock_paypal_api.post.side_effect = [token_resp, verify_resp]

        resp = client.post(
            "/api/v1/plugins/paypal/webhook",
            data=json.dumps(event_payload),
            headers={"PAYPAL-TRANSMISSION-ID": "abc"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["received"] is True
        mock_container.event_dispatcher.return_value.emit.assert_called_once()

    def test_webhook_ignores_unknown_events(
        self, client, mock_paypal_api, mock_container
    ):
        """Should return 200 but emit no event for unknown event types."""
        event_payload = {
            "event_type": "SOME.UNKNOWN.EVENT",
            "resource": {},
        }
        verify_resp = MagicMock()
        verify_resp.status_code = 200
        verify_resp.json.return_value = {"verification_status": "SUCCESS"}
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"access_token": "tok", "expires_in": 3600}
        mock_paypal_api.post.side_effect = [token_resp, verify_resp]

        resp = client.post(
            "/api/v1/plugins/paypal/webhook",
            data=json.dumps(event_payload),
            headers={"PAYPAL-TRANSMISSION-ID": "abc"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        mock_container.event_dispatcher.return_value.emit.assert_not_called()


# ---- session-status tests ----


class TestSessionStatus:
    """Tests for GET /api/v1/plugins/paypal/session-status/<order_id>."""

    def test_session_status_requires_auth(self, client):
        """Should return 401 without Authorization header."""
        resp = client.get("/api/v1/plugins/paypal/session-status/ORDER-123")
        assert resp.status_code == 401

    def test_session_status_success(self, client, auth_headers, mock_paypal_api):
        """Should return 200 with mapped status data."""
        status_resp = MagicMock()
        status_resp.status_code = 200
        status_resp.json.return_value = {
            "status": "COMPLETED",
            "purchase_units": [{
                "amount": {"value": "29.99", "currency_code": "USD"},
                "custom_id": "",
            }],
        }
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"access_token": "tok", "expires_in": 3600}
        mock_paypal_api.post.return_value = token_resp
        mock_paypal_api.get.return_value = status_resp

        resp = client.get(
            "/api/v1/plugins/paypal/session-status/ORDER-123",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "paid"  # COMPLETED mapped to "paid"
        assert data["amount_total"] == "29.99"
