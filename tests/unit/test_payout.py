"""Payout capability specs (S79 Slice 2) — PayPal Payouts API v1.

RED-first oracle for the `PayoutProvider` contract on PayPal:
- `PayPalSDKAdapter.create_payout_batch` posts the correct Payouts API
  shape (`POST /v1/payments/payouts`, `sender_batch_id` = reference id
  for idempotency, major-unit amount, EMAIL recipient).
- `PayPalPlugin.create_payout` maps adapter responses to `PayoutResult`
  and every failure (provider rejection, network error) to the typed
  `PayoutError` — no provider-specific exception leaks to the caller.
"""
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
import requests as real_requests

from vbwd.plugins.payment_provider import PayoutError, PayoutProvider, PayoutResult
from vbwd.sdk.interface import SDKConfig, SDKResponse


REFERENCE_ID = "withdraw-req-123"


@pytest.fixture
def adapter(mock_paypal_api):
    from plugins.paypal.paypal.sdk_adapter import PayPalSDKAdapter

    config = SDKConfig(api_key="ATest123", api_secret="secret456", sandbox=True)
    return PayPalSDKAdapter(config)


def _token_response():
    token_resp = MagicMock()
    token_resp.status_code = 200
    token_resp.json.return_value = {"access_token": "tok", "expires_in": 3600}
    return token_resp


def _payout_batch_response(batch_status: str = "PENDING"):
    payout_resp = MagicMock()
    payout_resp.status_code = 201
    payout_resp.json.return_value = {
        "batch_header": {
            "payout_batch_id": "PB-9001",
            "batch_status": batch_status,
        }
    }
    return payout_resp


class TestCreatePayoutBatch:
    def test_posts_payouts_api_v1_shape(self, adapter, mock_paypal_api):
        mock_paypal_api.post.side_effect = [
            _token_response(),
            _payout_batch_response(),
        ]

        response = adapter.create_payout_batch(
            amount=Decimal("12.34"),
            currency="EUR",
            receiver_email="payee@example.com",
            reference_id=REFERENCE_ID,
        )

        assert response.success is True
        assert response.data["payout_batch_id"] == "PB-9001"
        assert response.data["batch_status"] == "PENDING"

        payout_call = mock_paypal_api.post.call_args_list[1]
        assert payout_call.args[0].endswith("/v1/payments/payouts")
        body = payout_call.kwargs["json"]
        assert body["sender_batch_header"]["sender_batch_id"] == REFERENCE_ID
        item = body["items"][0]
        assert item["recipient_type"] == "EMAIL"
        assert item["receiver"] == "payee@example.com"
        # PayPal uses major-unit amounts — '12.34', never cents.
        assert item["amount"] == {"value": "12.34", "currency": "EUR"}
        assert item["sender_item_id"] == REFERENCE_ID

    def test_error_response_maps_to_failed_sdk_response(self, adapter, mock_paypal_api):
        error_resp = MagicMock()
        error_resp.status_code = 422
        error_resp.text = "INSUFFICIENT_FUNDS"
        mock_paypal_api.post.side_effect = [_token_response(), error_resp]

        response = adapter.create_payout_batch(
            amount=Decimal("1.00"),
            currency="EUR",
            receiver_email="payee@example.com",
            reference_id=REFERENCE_ID,
        )

        assert response.success is False
        assert "INSUFFICIENT_FUNDS" in response.error

    def test_network_error_maps_to_failed_sdk_response(self, adapter, mock_paypal_api):
        mock_paypal_api.RequestException = real_requests.RequestException
        mock_paypal_api.post.side_effect = [
            _token_response(),
            real_requests.ConnectionError("connection refused"),
        ]

        response = adapter.create_payout_batch(
            amount=Decimal("1.00"),
            currency="EUR",
            receiver_email="payee@example.com",
            reference_id=REFERENCE_ID,
        )

        assert response.success is False
        assert "network" in response.error


class TestGetPayoutBatchStatus:
    def test_returns_batch_status(self, adapter, mock_paypal_api):
        mock_paypal_api.post.side_effect = [_token_response()]
        status_resp = MagicMock()
        status_resp.status_code = 200
        status_resp.json.return_value = {
            "batch_header": {"payout_batch_id": "PB-9001", "batch_status": "SUCCESS"}
        }
        mock_paypal_api.get.return_value = status_resp

        response = adapter.get_payout_status("PB-9001")

        assert response.success is True
        assert response.data["batch_status"] == "SUCCESS"
        status_call = mock_paypal_api.get.call_args
        assert status_call.args[0].endswith("/v1/payments/payouts/PB-9001")

    def test_error_response_maps_to_failed_sdk_response(self, adapter, mock_paypal_api):
        mock_paypal_api.post.side_effect = [_token_response()]
        error_resp = MagicMock()
        error_resp.status_code = 404
        error_resp.text = "BATCH_NOT_FOUND"
        mock_paypal_api.get.return_value = error_resp

        response = adapter.get_payout_status("PB-missing")

        assert response.success is False
        assert "BATCH_NOT_FOUND" in response.error


class TestPayPalPluginPayout:
    def _plugin_with_adapter(self, adapter_mock):
        from plugins.paypal import PayPalPlugin

        plugin = PayPalPlugin()
        plugin._get_adapter = lambda: adapter_mock
        return plugin

    def test_plugin_is_a_payout_provider(self):
        from plugins.paypal import PayPalPlugin

        assert issubclass(PayPalPlugin, PayoutProvider)

    def test_destination_schema(self):
        from plugins.paypal import PayPalPlugin

        assert PayPalPlugin().get_payout_destination_schema() == [
            {
                "name": "email",
                "type": "email",
                "label_key": "withdraw.destination.paypal_email",
            }
        ]

    def test_create_payout_returns_processing_result(self):
        adapter_mock = MagicMock()
        adapter_mock.create_payout_batch.return_value = SDKResponse(
            success=True,
            data={"payout_batch_id": "PB-9001", "batch_status": "PENDING"},
        )
        plugin = self._plugin_with_adapter(adapter_mock)

        result = plugin.create_payout(
            amount=Decimal("12.34"),
            currency="EUR",
            destination={"email": "payee@example.com"},
            reference_id=REFERENCE_ID,
        )

        assert isinstance(result, PayoutResult)
        assert result.provider_payout_id == "PB-9001"
        assert result.status == "processing"
        adapter_mock.create_payout_batch.assert_called_once_with(
            amount=Decimal("12.34"),
            currency="EUR",
            receiver_email="payee@example.com",
            reference_id=REFERENCE_ID,
        )

    def test_create_payout_provider_failure_raises_payout_error(self):
        adapter_mock = MagicMock()
        adapter_mock.create_payout_batch.return_value = SDKResponse(
            success=False, error="INSUFFICIENT_FUNDS"
        )
        plugin = self._plugin_with_adapter(adapter_mock)

        with pytest.raises(PayoutError):
            plugin.create_payout(
                amount=Decimal("12.34"),
                currency="EUR",
                destination={"email": "payee@example.com"},
                reference_id=REFERENCE_ID,
            )

    def test_create_payout_without_email_raises_payout_error(self):
        plugin = self._plugin_with_adapter(MagicMock())

        with pytest.raises(PayoutError):
            plugin.create_payout(
                amount=Decimal("12.34"),
                currency="EUR",
                destination={},
                reference_id=REFERENCE_ID,
            )

    @pytest.mark.parametrize(
        "batch_status,expected",
        [
            ("SUCCESS", "completed"),
            ("PENDING", "processing"),
            ("PROCESSING", "processing"),
            ("DENIED", "failed"),
            ("CANCELED", "failed"),
        ],
    )
    def test_get_payout_status_maps_batch_status(self, batch_status, expected):
        adapter_mock = MagicMock()
        adapter_mock.get_payout_status.return_value = SDKResponse(
            success=True, data={"batch_status": batch_status}
        )
        plugin = self._plugin_with_adapter(adapter_mock)

        assert plugin.get_payout_status("PB-9001") == expected

    def test_get_payout_status_failure_raises_payout_error(self):
        adapter_mock = MagicMock()
        adapter_mock.get_payout_status.return_value = SDKResponse(
            success=False, error="BATCH_NOT_FOUND"
        )
        plugin = self._plugin_with_adapter(adapter_mock)

        with pytest.raises(PayoutError):
            plugin.get_payout_status("PB-missing")
