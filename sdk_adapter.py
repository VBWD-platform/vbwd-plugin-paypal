"""PayPal SDK adapter implementing ISDKAdapter."""
import json
import time
from decimal import Decimal
from typing import Dict, Any, Optional

import requests

from src.sdk.base import BaseSDKAdapter
from src.sdk.interface import SDKConfig, SDKResponse


class PayPalSDKAdapter(BaseSDKAdapter):
    """PayPal SDK adapter implementing ISDKAdapter.

    Uses PayPal Orders API v2 (redirect-based, PCI-compliant).
    Liskov Substitution: interchangeable with any ISDKAdapter.
    """

    def __init__(self, config: SDKConfig, idempotency_service=None):
        super().__init__(config, idempotency_service)
        self._client_id = config.api_key
        self._client_secret = config.api_secret or ""
        self._base_url = (
            "https://api-m.sandbox.paypal.com"
            if config.sandbox
            else "https://api-m.paypal.com"
        )
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0

    @property
    def provider_name(self) -> str:
        return "paypal"

    def _get_access_token(self) -> str:
        """Get or refresh PayPal OAuth2 access token."""
        if self._access_token and time.time() < self._token_expires_at:
            return self._access_token
        response = requests.post(
            f"{self._base_url}/v1/oauth2/token",
            auth=(self._client_id, self._client_secret),
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json"},
            timeout=self._config.timeout,
        )
        response.raise_for_status()
        data = response.json()
        self._access_token = data["access_token"]
        self._token_expires_at = time.time() + data["expires_in"] - 60
        return self._access_token

    def _headers(self, idempotency_key: Optional[str] = None) -> Dict[str, str]:
        """Build authorization headers."""
        token = self._get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        if idempotency_key:
            headers["PayPal-Request-Id"] = idempotency_key
        return headers

    def create_payment_intent(
        self,
        amount: Decimal,
        currency: str,
        metadata: Dict[str, Any],
        idempotency_key: Optional[str] = None,
    ) -> SDKResponse:
        """Create PayPal Order for one-time payment."""

        def _create():
            order_data = {
                "intent": "CAPTURE",
                "purchase_units": [
                    {
                        "amount": {
                            "currency_code": currency.upper(),
                            "value": str(amount),
                        },
                        "custom_id": metadata.get("invoice_id", ""),
                    }
                ],
                "application_context": {
                    "return_url": metadata.get("success_url", ""),
                    "cancel_url": metadata.get("cancel_url", ""),
                    "user_action": "PAY_NOW",
                },
            }
            resp = requests.post(
                f"{self._base_url}/v2/checkout/orders",
                json=order_data,
                headers=self._headers(idempotency_key),
                timeout=self._config.timeout,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                approve_url = next(
                    (l["href"] for l in data.get("links", []) if l["rel"] == "approve"),
                    None,
                )
                return SDKResponse(
                    success=True,
                    data={
                        "session_id": data["id"],
                        "session_url": approve_url,
                    },
                )
            return SDKResponse(
                success=False, error=resp.text, error_code=str(resp.status_code)
            )

        return self._with_idempotency(idempotency_key, _create)

    def capture_order(self, order_id: str) -> SDKResponse:
        """Capture a previously approved PayPal Order."""
        resp = requests.post(
            f"{self._base_url}/v2/checkout/orders/{order_id}/capture",
            headers=self._headers(),
            json={},
            timeout=self._config.timeout,
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            captures = (
                data.get("purchase_units", [{}])[0]
                .get("payments", {})
                .get("captures", [{}])
            )
            capture = captures[0] if captures else {}
            return SDKResponse(
                success=True,
                data={
                    "order_id": data["id"],
                    "capture_id": capture.get("id", ""),
                    "status": data.get("status", ""),
                    "amount": capture.get("amount", {}).get("value", "0"),
                    "currency": capture.get("amount", {}).get("currency_code", "USD"),
                },
            )
        return SDKResponse(
            success=False, error=resp.text, error_code=str(resp.status_code)
        )

    def capture_payment(
        self, payment_intent_id: str, idempotency_key: Optional[str] = None
    ) -> SDKResponse:
        """Capture payment (ISDKAdapter interface). Delegates to capture_order."""

        def _capture():
            return self.capture_order(payment_intent_id)

        return self._with_idempotency(idempotency_key, _capture)

    def create_subscription(
        self,
        plan_id: str,
        metadata: Dict[str, Any],
        success_url: str,
        cancel_url: str,
    ) -> SDKResponse:
        """Create PayPal Billing Subscription for recurring payments."""
        sub_data = {
            "plan_id": plan_id,
            "custom_id": metadata.get("invoice_id", ""),
            "application_context": {
                "return_url": success_url,
                "cancel_url": cancel_url,
                "user_action": "SUBSCRIBE_NOW",
            },
        }
        resp = requests.post(
            f"{self._base_url}/v1/billing/subscriptions",
            json=sub_data,
            headers=self._headers(),
            timeout=self._config.timeout,
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            approve_url = next(
                (l["href"] for l in data.get("links", []) if l["rel"] == "approve"),
                None,
            )
            return SDKResponse(
                success=True,
                data={
                    "subscription_id": data["id"],
                    "session_url": approve_url,
                },
            )
        return SDKResponse(
            success=False, error=resp.text, error_code=str(resp.status_code)
        )

    def create_billing_plan(
        self,
        product_id: str,
        name: str,
        amount: str,
        currency: str,
        interval: str,
        interval_count: int = 1,
    ) -> SDKResponse:
        """Create a PayPal Billing Plan for recurring subscriptions."""
        plan_data = {
            "product_id": product_id,
            "name": name,
            "billing_cycles": [
                {
                    "frequency": {
                        "interval_unit": interval.upper(),
                        "interval_count": interval_count,
                    },
                    "tenure_type": "REGULAR",
                    "sequence": 1,
                    "total_cycles": 0,
                    "pricing_scheme": {
                        "fixed_price": {
                            "value": amount,
                            "currency_code": currency.upper(),
                        }
                    },
                }
            ],
            "payment_preferences": {
                "auto_bill_outstanding": True,
                "payment_failure_threshold": 3,
            },
        }
        resp = requests.post(
            f"{self._base_url}/v1/billing/plans",
            json=plan_data,
            headers=self._headers(),
            timeout=self._config.timeout,
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            return SDKResponse(success=True, data={"plan_id": data["id"]})
        return SDKResponse(
            success=False, error=resp.text, error_code=str(resp.status_code)
        )

    def create_product(self, name: str) -> SDKResponse:
        """Create a PayPal Catalog Product (required for billing plans)."""
        product_data = {
            "name": name,
            "type": "SERVICE",
        }
        resp = requests.post(
            f"{self._base_url}/v1/catalogs/products",
            json=product_data,
            headers=self._headers(),
            timeout=self._config.timeout,
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            return SDKResponse(success=True, data={"product_id": data["id"]})
        return SDKResponse(
            success=False, error=resp.text, error_code=str(resp.status_code)
        )

    def get_payment_status(self, order_id: str) -> SDKResponse:
        """Get PayPal Order status."""
        resp = requests.get(
            f"{self._base_url}/v2/checkout/orders/{order_id}",
            headers=self._headers(),
            timeout=self._config.timeout,
        )
        if resp.status_code == 200:
            data = resp.json()
            purchase_unit = data.get("purchase_units", [{}])[0]
            return SDKResponse(
                success=True,
                data={
                    "status": data.get("status", ""),
                    "amount_total": purchase_unit.get("amount", {}).get("value", "0"),
                    "currency": purchase_unit.get("amount", {}).get(
                        "currency_code", "USD"
                    ),
                    "custom_id": purchase_unit.get("custom_id", ""),
                },
            )
        return SDKResponse(
            success=False, error=resp.text, error_code=str(resp.status_code)
        )

    def get_subscription_status(self, subscription_id: str) -> SDKResponse:
        """Get PayPal Subscription status."""
        resp = requests.get(
            f"{self._base_url}/v1/billing/subscriptions/{subscription_id}",
            headers=self._headers(),
            timeout=self._config.timeout,
        )
        if resp.status_code == 200:
            data = resp.json()
            billing_info = data.get("billing_info", {})
            last_payment = billing_info.get("last_payment", {})
            return SDKResponse(
                success=True,
                data={
                    "status": data.get("status", ""),
                    "custom_id": data.get("custom_id", ""),
                    "amount": last_payment.get("amount", {}).get("value", "0"),
                    "currency": last_payment.get("amount", {}).get(
                        "currency_code", "USD"
                    ),
                },
            )
        return SDKResponse(
            success=False, error=resp.text, error_code=str(resp.status_code)
        )

    def verify_webhook_signature(
        self, payload: bytes, headers: Dict[str, str], webhook_id: str
    ) -> dict:
        """Verify PayPal webhook signature via PayPal API."""
        token = self._get_access_token()
        verify_data = {
            "auth_algo": headers.get("PAYPAL-AUTH-ALGO", ""),
            "cert_url": headers.get("PAYPAL-CERT-URL", ""),
            "transmission_id": headers.get("PAYPAL-TRANSMISSION-ID", ""),
            "transmission_sig": headers.get("PAYPAL-TRANSMISSION-SIG", ""),
            "transmission_time": headers.get("PAYPAL-TRANSMISSION-TIME", ""),
            "webhook_id": webhook_id,
            "webhook_event": json.loads(payload),
        }
        resp = requests.post(
            f"{self._base_url}/v1/notifications/verify-webhook-signature",
            json=verify_data,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=self._config.timeout,
        )
        if resp.status_code == 200:
            result = resp.json()
            if result.get("verification_status") == "SUCCESS":
                return json.loads(payload)
        raise ValueError("Invalid PayPal webhook signature")

    def refund_payment(
        self,
        capture_id: str,
        amount: Optional[Decimal] = None,
        idempotency_key: Optional[str] = None,
    ) -> SDKResponse:
        """Refund a captured PayPal payment."""

        def _refund():
            refund_data: Dict[str, Any] = {}
            if amount is not None:
                refund_data["amount"] = {
                    "value": str(amount),
                    "currency_code": "USD",
                }
            resp = requests.post(
                f"{self._base_url}/v2/payments/captures/{capture_id}/refund",
                json=refund_data,
                headers=self._headers(idempotency_key),
                timeout=self._config.timeout,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                return SDKResponse(
                    success=True,
                    data={"refund_id": data["id"], "status": data["status"]},
                )
            return SDKResponse(
                success=False, error=resp.text, error_code=str(resp.status_code)
            )

        return self._with_idempotency(idempotency_key, _refund)
