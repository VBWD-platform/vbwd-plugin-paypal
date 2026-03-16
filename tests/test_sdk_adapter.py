"""Tests for PayPalSDKAdapter."""
import json
import pytest
from decimal import Decimal
from unittest.mock import MagicMock

from src.sdk.interface import SDKConfig, SDKResponse
from src.sdk.base import BaseSDKAdapter


@pytest.fixture
def adapter(mock_paypal_api):
    """Create PayPalSDKAdapter with mocked requests module."""
    from plugins.paypal.sdk_adapter import PayPalSDKAdapter

    config = SDKConfig(
        api_key="ATest123", api_secret="secret456", sandbox=True
    )
    return PayPalSDKAdapter(config)


class TestPayPalSDKAdapter:
    """Tests for PayPalSDKAdapter."""

    def test_provider_name(self, adapter):
        """provider_name should return 'paypal'."""
        assert adapter.provider_name == "paypal"

    def test_inherits_base_sdk_adapter(self, adapter):
        """PayPalSDKAdapter should inherit from BaseSDKAdapter."""
        assert isinstance(adapter, BaseSDKAdapter)

    def test_get_access_token(self, adapter, mock_paypal_api):
        """Should call OAuth2 endpoint and cache token."""
        token = adapter._get_access_token()
        assert token == "test-token"
        mock_paypal_api.post.assert_called_once()

    def test_get_access_token_caches(self, adapter, mock_paypal_api):
        """Should reuse cached token on second call."""
        adapter._get_access_token()
        adapter._get_access_token()
        # Only one call — token is cached
        assert mock_paypal_api.post.call_count == 1

    def test_create_payment_intent_success(self, adapter, mock_paypal_api):
        """create_payment_intent should return session_id and session_url."""
        order_resp = MagicMock()
        order_resp.status_code = 201
        order_resp.json.return_value = {
            "id": "ORDER-123",
            "links": [
                {"rel": "approve", "href": "https://paypal.com/approve/ORDER-123"},
                {"rel": "self", "href": "https://api.paypal.com/v2/orders/ORDER-123"},
            ],
        }
        # First call is token, second is create order
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"access_token": "tok", "expires_in": 3600}
        mock_paypal_api.post.side_effect = [token_resp, order_resp]

        result = adapter.create_payment_intent(
            amount=Decimal("29.99"),
            currency="USD",
            metadata={
                "invoice_id": "inv_123",
                "success_url": "https://example.com/ok",
                "cancel_url": "https://example.com/cancel",
            },
        )

        assert result.success is True
        assert result.data["session_id"] == "ORDER-123"
        assert "paypal.com/approve" in result.data["session_url"]

    def test_create_payment_intent_error(self, adapter, mock_paypal_api):
        """create_payment_intent should return error on failure."""
        error_resp = MagicMock()
        error_resp.status_code = 422
        error_resp.text = "UNPROCESSABLE_ENTITY"
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"access_token": "tok", "expires_in": 3600}
        mock_paypal_api.post.side_effect = [token_resp, error_resp]

        result = adapter.create_payment_intent(
            amount=Decimal("10.00"),
            currency="USD",
            metadata={"success_url": "", "cancel_url": ""},
        )

        assert result.success is False
        assert "UNPROCESSABLE_ENTITY" in result.error

    def test_create_payment_intent_amount_format(self, adapter, mock_paypal_api):
        """PayPal uses dollar amounts (not cents): Decimal('29.99') → '29.99'."""
        order_resp = MagicMock()
        order_resp.status_code = 201
        order_resp.json.return_value = {
            "id": "ORDER-AMT",
            "links": [{"rel": "approve", "href": "https://paypal.com/approve"}],
        }
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"access_token": "tok", "expires_in": 3600}
        mock_paypal_api.post.side_effect = [token_resp, order_resp]

        adapter.create_payment_intent(
            amount=Decimal("29.99"),
            currency="EUR",
            metadata={"success_url": "", "cancel_url": ""},
        )

        # Check the order data sent to PayPal
        call_args = mock_paypal_api.post.call_args_list[1]
        order_data = call_args.kwargs.get("json") or call_args[1].get("json")
        value = order_data["purchase_units"][0]["amount"]["value"]
        assert value == "29.99"  # Not 2999 (cents)

    def test_capture_order_success(self, adapter, mock_paypal_api):
        """capture_order should return capture_id and status."""
        capture_resp = MagicMock()
        capture_resp.status_code = 201
        capture_resp.json.return_value = {
            "id": "ORDER-CAP",
            "status": "COMPLETED",
            "purchase_units": [{
                "payments": {
                    "captures": [{
                        "id": "CAP-123",
                        "amount": {"value": "29.99", "currency_code": "USD"},
                    }]
                }
            }],
        }
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"access_token": "tok", "expires_in": 3600}
        mock_paypal_api.post.side_effect = [token_resp, capture_resp]

        result = adapter.capture_order("ORDER-CAP")

        assert result.success is True
        assert result.data["capture_id"] == "CAP-123"
        assert result.data["status"] == "COMPLETED"

    def test_capture_order_error(self, adapter, mock_paypal_api):
        """capture_order should return error on failure."""
        error_resp = MagicMock()
        error_resp.status_code = 422
        error_resp.text = "ORDER_NOT_APPROVED"
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"access_token": "tok", "expires_in": 3600}
        mock_paypal_api.post.side_effect = [token_resp, error_resp]

        result = adapter.capture_order("BAD-ORDER")

        assert result.success is False

    def test_create_subscription_success(self, adapter, mock_paypal_api):
        """create_subscription should return subscription_id and session_url."""
        sub_resp = MagicMock()
        sub_resp.status_code = 201
        sub_resp.json.return_value = {
            "id": "I-SUB-123",
            "links": [
                {"rel": "approve", "href": "https://paypal.com/approve/sub"},
            ],
        }
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"access_token": "tok", "expires_in": 3600}
        mock_paypal_api.post.side_effect = [token_resp, sub_resp]

        result = adapter.create_subscription(
            plan_id="P-PLAN-123",
            metadata={"invoice_id": "inv_1"},
            success_url="https://example.com/ok",
            cancel_url="https://example.com/cancel",
        )

        assert result.success is True
        assert result.data["subscription_id"] == "I-SUB-123"

    def test_get_payment_status(self, adapter, mock_paypal_api):
        """get_payment_status should return status, amount_total, currency."""
        status_resp = MagicMock()
        status_resp.status_code = 200
        status_resp.json.return_value = {
            "status": "COMPLETED",
            "purchase_units": [{
                "amount": {"value": "49.99", "currency_code": "EUR"},
                "custom_id": "inv_abc",
            }],
        }
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"access_token": "tok", "expires_in": 3600}
        mock_paypal_api.post.return_value = token_resp
        mock_paypal_api.get.return_value = status_resp

        result = adapter.get_payment_status("ORDER-STATUS")

        assert result.success is True
        assert result.data["status"] == "COMPLETED"
        assert result.data["amount_total"] == "49.99"
        assert result.data["currency"] == "EUR"

    def test_refund_full(self, adapter, mock_paypal_api):
        """refund_payment without amount should issue full refund."""
        refund_resp = MagicMock()
        refund_resp.status_code = 201
        refund_resp.json.return_value = {"id": "REF-FULL", "status": "COMPLETED"}
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"access_token": "tok", "expires_in": 3600}
        mock_paypal_api.post.side_effect = [token_resp, refund_resp]

        result = adapter.refund_payment("CAP-123")

        assert result.success is True
        assert result.data["refund_id"] == "REF-FULL"

    def test_refund_partial(self, adapter, mock_paypal_api):
        """refund_payment with amount should include amount in request."""
        refund_resp = MagicMock()
        refund_resp.status_code = 201
        refund_resp.json.return_value = {"id": "REF-PART", "status": "COMPLETED"}
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"access_token": "tok", "expires_in": 3600}
        mock_paypal_api.post.side_effect = [token_resp, refund_resp]

        result = adapter.refund_payment("CAP-123", amount=Decimal("15.00"))

        assert result.success is True
        call_args = mock_paypal_api.post.call_args_list[1]
        refund_data = call_args.kwargs.get("json") or call_args[1].get("json")
        assert refund_data["amount"]["value"] == "15.00"

    def test_verify_webhook_valid(self, adapter, mock_paypal_api):
        """verify_webhook_signature should return parsed event on success."""
        verify_resp = MagicMock()
        verify_resp.status_code = 200
        verify_resp.json.return_value = {"verification_status": "SUCCESS"}
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"access_token": "tok", "expires_in": 3600}
        mock_paypal_api.post.side_effect = [token_resp, verify_resp]

        event_payload = {"event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {}}
        result = adapter.verify_webhook_signature(
            json.dumps(event_payload).encode(),
            {"PAYPAL-TRANSMISSION-ID": "abc"},
            "WH-789",
        )

        assert result["event_type"] == "PAYMENT.CAPTURE.COMPLETED"

    def test_verify_webhook_invalid(self, adapter, mock_paypal_api):
        """verify_webhook_signature should raise ValueError on failure."""
        verify_resp = MagicMock()
        verify_resp.status_code = 200
        verify_resp.json.return_value = {"verification_status": "FAILURE"}
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"access_token": "tok", "expires_in": 3600}
        mock_paypal_api.post.side_effect = [token_resp, verify_resp]

        with pytest.raises(ValueError, match="Invalid PayPal webhook signature"):
            adapter.verify_webhook_signature(
                b'{"event_type": "test"}',
                {"PAYPAL-TRANSMISSION-ID": "abc"},
                "WH-789",
            )
