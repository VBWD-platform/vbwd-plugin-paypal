"""PayPal payment provider plugin."""
from typing import Optional, Dict, Any, TYPE_CHECKING
from decimal import Decimal
from uuid import UUID
from src.plugins.base import BasePlugin, PluginMetadata
from src.plugins.payment_provider import PaymentProviderPlugin, PaymentResult, PaymentStatus

if TYPE_CHECKING:
    from flask import Blueprint


class PayPalPlugin(PaymentProviderPlugin):
    """PayPal payment provider — Orders API with webhooks.

    Class MUST be defined in __init__.py (not re-exported) due to
    discovery check obj.__module__ != full_module in manager.py.
    """

    @property
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="paypal",
            version="1.0.0",
            author="VBWD Team",
            description="PayPal payment provider — Orders API with webhooks",
            dependencies=[],
        )

    def get_blueprint(self) -> Optional["Blueprint"]:
        from plugins.paypal.routes import paypal_plugin_bp
        return paypal_plugin_bp

    def get_url_prefix(self) -> Optional[str]:
        return "/api/v1/plugins/paypal"

    def on_enable(self) -> None:
        pass  # Stateless — config read per-request from config_store

    def on_disable(self) -> None:
        pass

    def _get_adapter(self):
        """Instantiate PayPalSDKAdapter from config_store (per-request)."""
        from flask import current_app
        from plugins.paypal.sdk_adapter import PayPalSDKAdapter
        from src.sdk.interface import SDKConfig

        config_store = current_app.config_store
        config = config_store.get_config("paypal")
        prefix = "test_" if config.get("sandbox", True) else "live_"
        return PayPalSDKAdapter(SDKConfig(
            api_key=config.get(f"{prefix}client_id") or config.get("client_id", ""),
            api_secret=config.get(f"{prefix}client_secret") or config.get("client_secret", ""),
            sandbox=config.get("sandbox", True),
        ))

    def create_payment_intent(
        self,
        amount: Decimal,
        currency: str,
        subscription_id: UUID,
        user_id: UUID,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PaymentResult:
        adapter = self._get_adapter()
        meta = metadata or {}
        meta.update({"subscription_id": str(subscription_id), "user_id": str(user_id)})
        resp = adapter.create_payment_intent(amount, currency, meta)
        if resp.success:
            return PaymentResult(
                success=True,
                transaction_id=resp.data.get("session_id"),
                status=PaymentStatus.PENDING,
                metadata=resp.data,
            )
        return PaymentResult(success=False, error_message=resp.error)

    def process_payment(self, payment_intent_id: str, payment_method: str) -> PaymentResult:
        adapter = self._get_adapter()
        resp = adapter.capture_order(payment_intent_id)
        if resp.success:
            return PaymentResult(
                success=True,
                transaction_id=resp.data.get("capture_id"),
                status=PaymentStatus.COMPLETED,
            )
        return PaymentResult(success=False, error_message=resp.error)

    def refund_payment(self, transaction_id: str, amount: Optional[Decimal] = None) -> PaymentResult:
        """Refund a PayPal payment.

        transaction_id may be an order_id (from provider_session_id) or a capture_id.
        If it's an order_id, we first extract the capture_id from the order details.
        """
        adapter = self._get_adapter()

        # Try to extract capture_id from order details (transaction_id may be order_id)
        capture_id = self._resolve_capture_id(adapter, transaction_id)
        if not capture_id:
            return PaymentResult(success=False, error_message="Cannot find capture ID for refund")

        resp = adapter.refund_payment(capture_id, amount)
        if resp.success:
            return PaymentResult(
                success=True,
                transaction_id=resp.data.get("refund_id"),
                status=PaymentStatus.REFUNDED,
            )
        return PaymentResult(success=False, error_message=resp.error)

    @staticmethod
    def _resolve_capture_id(adapter, order_or_capture_id: str) -> Optional[str]:
        """Resolve a capture_id from an order_id or return as-is if already a capture_id."""
        import requests as req

        # Try fetching as an order to extract capture_id
        resp = req.get(
            f"{adapter._base_url}/v2/checkout/orders/{order_or_capture_id}",
            headers=adapter._headers(),
            timeout=adapter._config.timeout,
        )
        if resp.status_code == 200:
            data = resp.json()
            captures = (
                data.get("purchase_units", [{}])[0]
                .get("payments", {})
                .get("captures", [])
            )
            if captures:
                return captures[0].get("id")

        # If order lookup fails, assume it's already a capture_id
        return order_or_capture_id

    def verify_webhook(self, payload: bytes, signature: str) -> bool:
        adapter = self._get_adapter()
        from flask import current_app
        config = current_app.config_store.get_config("paypal")
        prefix = "test_" if config.get("sandbox", True) else "live_"
        webhook_id = config.get(f"{prefix}webhook_id") or config.get("webhook_id", "")
        try:
            adapter.verify_webhook_signature(
                payload, signature, webhook_id
            )
            return True
        except ValueError:
            return False

    def handle_webhook(self, payload: Dict[str, Any]) -> None:
        pass  # Webhook handling is done in routes.py (event-driven)
