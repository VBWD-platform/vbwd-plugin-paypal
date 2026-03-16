"""PayPal plugin API routes."""
import logging
from decimal import Decimal
from uuid import UUID

from flask import Blueprint, jsonify, request, current_app, g

from src.middleware.auth import require_auth
from src.plugins.payment_route_helpers import (
    check_plugin_enabled,
    validate_invoice_for_payment,
    emit_payment_captured,
    determine_session_mode,
)
from src.sdk.interface import SDKConfig
from src.models.enums import LineItemType, InvoiceStatus
from src.models.invoice_line_item import InvoiceLineItem
from src.models.invoice import UserInvoice
from src.events.payment_events import (
    SubscriptionCancelledEvent,
    PaymentFailedEvent,
)

logger = logging.getLogger(__name__)

paypal_plugin_bp = Blueprint("paypal_plugin", __name__)

# Billing period to PayPal interval mapping
BILLING_PERIOD_TO_PAYPAL = {
    "daily": {"interval_unit": "DAY", "interval_count": 1},
    "weekly": {"interval_unit": "WEEK", "interval_count": 1},
    "monthly": {"interval_unit": "MONTH", "interval_count": 1},
    "quarterly": {"interval_unit": "MONTH", "interval_count": 3},
    "yearly": {"interval_unit": "YEAR", "interval_count": 1},
}


def _get_adapter(config):
    """Instantiate PayPalSDKAdapter from plugin config."""
    from plugins.paypal.sdk_adapter import PayPalSDKAdapter

    prefix = "test_" if config.get("sandbox", True) else "live_"
    return PayPalSDKAdapter(
        SDKConfig(
            api_key=config.get(f"{prefix}client_id") or config.get("client_id", ""),
            api_secret=config.get(f"{prefix}client_secret")
            or config.get("client_secret", ""),
            sandbox=config.get("sandbox", True),
        )
    )


@paypal_plugin_bp.route("/create-session", methods=["POST"])
@paypal_plugin_bp.route("/create-order", methods=["POST"])
@require_auth
def create_order():
    """Create a PayPal Order or Subscription for a PENDING invoice."""
    config, err = check_plugin_enabled("paypal")
    if err:
        return err

    data = request.get_json() or {}
    invoice, err = validate_invoice_for_payment(data.get("invoice_id", ""), g.user_id)
    if err:
        return err

    adapter = _get_adapter(config)
    mode = determine_session_mode(invoice)
    base_meta = {"invoice_id": str(invoice.id), "user_id": str(g.user_id)}

    frontend_base = (
        request.headers.get("Origin")
        or request.headers.get("Referer", "").rstrip("/").rsplit("/pay", 1)[0]
        or request.host_url.rstrip("/")
    )
    success_url = f"{frontend_base}/pay/paypal/success"
    cancel_url = f"{frontend_base}/pay/paypal/cancel"

    if mode == "subscription":
        plan_resp = _get_or_create_paypal_plan(adapter, invoice, config)
        if not plan_resp.success:
            return jsonify({"error": plan_resp.error}), 500
        response = adapter.create_subscription(
            plan_id=plan_resp.data["plan_id"],
            metadata=base_meta,
            success_url=success_url,
            cancel_url=cancel_url,
        )
        if response.success:
            # Store PayPal subscription ID on invoice for reliable mapping
            paypal_id = response.data.get("subscription_id", "")
            if paypal_id:
                invoice.provider_session_id = paypal_id
                current_app.container.invoice_repository().save(invoice)
            return (
                jsonify(
                    {
                        "session_id": paypal_id,
                        "session_url": response.data.get("session_url"),
                    }
                ),
                200,
            )
    else:
        meta = {
            **base_meta,
            "success_url": success_url,
            "cancel_url": cancel_url,
        }
        response = adapter.create_payment_intent(
            amount=Decimal(str(invoice.total_amount or invoice.amount)),
            currency=invoice.currency or "USD",
            metadata=meta,
        )
        if response.success:
            # Store PayPal order ID on invoice for reliable mapping
            paypal_id = response.data.get("session_id", "")
            if paypal_id:
                invoice.provider_session_id = paypal_id
                current_app.container.invoice_repository().save(invoice)
            return jsonify(response.data), 200

    return jsonify({"error": response.error}), 500


@paypal_plugin_bp.route("/capture-order", methods=["POST"])
@require_auth
def capture_order():
    """Capture a PayPal Order after buyer approval."""
    config, err = check_plugin_enabled("paypal")
    if err:
        return err

    data = request.get_json() or {}
    order_id = data.get("order_id")
    if not order_id:
        return jsonify({"error": "order_id required"}), 400

    adapter = _get_adapter(config)
    response = adapter.capture_order(order_id)
    if not response.success:
        return jsonify({"error": response.error}), 500

    resp_data = response.data

    # Get order details to find invoice_id from custom_id
    custom_id = ""
    order_detail = adapter.get_payment_status(order_id)
    if order_detail.success:
        custom_id = order_detail.data.get("custom_id", "")

    # Fallback: look up invoice by provider_session_id if custom_id is empty
    if not custom_id:
        logger.warning(
            "PayPal custom_id empty for order %s, using provider_session_id fallback",
            order_id,
        )
        invoice_repo = current_app.container.invoice_repository()
        invoice = invoice_repo.find_by_provider_session_id(order_id)
        if invoice:
            custom_id = str(invoice.id)

    if custom_id:
        logger.info(
            "Emitting PaymentCapturedEvent for invoice %s (order %s)",
            custom_id,
            order_id,
        )
        result = emit_payment_captured(
            invoice_id=UUID(custom_id),
            payment_reference=order_id,
            amount=resp_data.get("amount", "0"),
            currency=resp_data.get("currency", "USD"),
            provider="paypal",
            transaction_id=resp_data.get("capture_id", ""),
        )
        if not result.success:
            logger.error("PaymentCapturedEvent handler failed: %s", result.error)
    else:
        logger.error(
            "Cannot find invoice for PayPal order %s — no custom_id and no provider_session_id match",
            order_id,
        )

    return (
        jsonify(
            {
                "status": resp_data.get("status"),
                "order_id": order_id,
                "capture_id": resp_data.get("capture_id"),
            }
        ),
        200,
    )


@paypal_plugin_bp.route("/webhook", methods=["POST"])
def paypal_webhook():
    """Handle PayPal webhook events."""
    config, err = check_plugin_enabled("paypal")
    if err:
        return err

    payload = request.get_data()
    headers = {
        "PAYPAL-AUTH-ALGO": request.headers.get("PAYPAL-AUTH-ALGO", ""),
        "PAYPAL-CERT-URL": request.headers.get("PAYPAL-CERT-URL", ""),
        "PAYPAL-TRANSMISSION-ID": request.headers.get("PAYPAL-TRANSMISSION-ID", ""),
        "PAYPAL-TRANSMISSION-SIG": request.headers.get("PAYPAL-TRANSMISSION-SIG", ""),
        "PAYPAL-TRANSMISSION-TIME": request.headers.get("PAYPAL-TRANSMISSION-TIME", ""),
    }

    adapter = _get_adapter(config)
    prefix = "test_" if config.get("sandbox", True) else "live_"
    webhook_id = config.get(f"{prefix}webhook_id") or config.get("webhook_id", "")
    try:
        event = adapter.verify_webhook_signature(payload, headers, webhook_id)
    except ValueError:
        return jsonify({"error": "Invalid signature"}), 400

    event_type = event.get("event_type", "")
    resource = event.get("resource", {})

    if event_type == "PAYMENT.CAPTURE.COMPLETED":
        _handle_capture_completed(resource)
    elif event_type == "BILLING.SUBSCRIPTION.ACTIVATED":
        _handle_subscription_activated(resource)
    elif event_type == "PAYMENT.SALE.COMPLETED":
        _handle_sale_completed(resource)
    elif event_type == "BILLING.SUBSCRIPTION.CANCELLED":
        _handle_subscription_cancelled(resource)
    elif event_type == "BILLING.SUBSCRIPTION.PAYMENT.FAILED":
        _handle_payment_failed(resource)

    return jsonify({"received": True}), 200


@paypal_plugin_bp.route("/session-status/<order_id>", methods=["GET"])
@require_auth
def session_status(order_id):
    """Poll PayPal Order or Subscription status."""
    config, err = check_plugin_enabled("paypal")
    if err:
        return err

    adapter = _get_adapter(config)

    # Try order status first, fall back to subscription status
    response = adapter.get_payment_status(order_id)
    if not response.success:
        response = adapter.get_subscription_status(order_id)
    if not response.success:
        return jsonify({"error": response.error}), 500

    status = response.data.get("status", "")
    # Map PayPal statuses to our standard status format
    if status in ("COMPLETED", "ACTIVE"):
        mapped_status = "paid"
    else:
        mapped_status = status.lower()

    # Reconciliation: if PayPal says paid, ensure our invoice is updated
    if mapped_status == "paid":
        custom_id = response.data.get("custom_id", "")
        # Fallback: look up invoice by provider_session_id
        if not custom_id:
            invoice_repo = current_app.container.invoice_repository()
            invoice = invoice_repo.find_by_provider_session_id(order_id)
            if invoice:
                custom_id = str(invoice.id)
        if custom_id:
            _reconcile_payment(custom_id, order_id, response.data)

    return (
        jsonify(
            {
                "status": mapped_status,
                "amount_total": response.data.get(
                    "amount_total", response.data.get("amount")
                ),
                "currency": response.data.get("currency"),
            }
        ),
        200,
    )


# ---- Webhook Handlers ----


def _handle_capture_completed(resource):
    """PayPal capture completed — one-time payment confirmed via webhook."""
    custom_id = resource.get("custom_id")
    if not custom_id:
        return

    container = current_app.container
    invoice_repo = container.invoice_repository()
    invoice = invoice_repo.find_by_id(UUID(custom_id))
    if not invoice or invoice.status.value != "PENDING":
        return

    capture_id = resource.get("id", "")
    amount = resource.get("amount", {}).get("value", "0")
    currency = resource.get("amount", {}).get("currency_code", "USD")
    emit_payment_captured(
        invoice_id=UUID(custom_id),
        payment_reference=capture_id,
        amount=amount,
        currency=currency,
        provider="paypal",
        transaction_id=capture_id,
    )


def _handle_subscription_activated(resource):
    """PayPal subscription activated — link subscription_id to our model."""
    paypal_sub_id = resource.get("id")
    custom_id = resource.get("custom_id")
    if not paypal_sub_id or not custom_id:
        return

    _link_paypal_subscription(UUID(custom_id), paypal_sub_id)

    billing_info = resource.get("billing_info", {})
    last_payment = billing_info.get("last_payment", {})
    emit_payment_captured(
        invoice_id=UUID(custom_id),
        payment_reference=paypal_sub_id,
        amount=last_payment.get("amount", {}).get("value", "0"),
        currency=last_payment.get("amount", {}).get("currency_code", "USD"),
        provider="paypal",
        transaction_id=paypal_sub_id,
    )


def _handle_sale_completed(resource):
    """PayPal subscription renewal payment completed."""
    billing_agreement_id = resource.get("billing_agreement_id")
    if not billing_agreement_id:
        return

    container = current_app.container
    sub_repo = container.subscription_repository()
    subscription = sub_repo.find_by_provider_subscription_id(billing_agreement_id)
    if not subscription:
        return

    renewal_invoice = _create_renewal_invoice(subscription, resource)
    emit_payment_captured(
        invoice_id=renewal_invoice.id,
        payment_reference=resource.get("id", ""),
        amount=resource.get("amount", {}).get("total", "0"),
        currency=resource.get("amount", {}).get("currency", "USD"),
        provider="paypal",
        transaction_id=resource.get("id", ""),
    )


def _handle_subscription_cancelled(resource):
    """PayPal subscription cancelled."""
    paypal_sub_id = resource.get("id")
    if not paypal_sub_id:
        return

    container = current_app.container
    sub_repo = container.subscription_repository()
    subscription = sub_repo.find_by_provider_subscription_id(paypal_sub_id)
    if not subscription:
        return

    event = SubscriptionCancelledEvent(
        subscription_id=subscription.id,
        user_id=subscription.user_id,
        reason="paypal_subscription_cancelled",
        provider="paypal",
    )
    container.event_dispatcher().emit(event)


def _handle_payment_failed(resource):
    """PayPal subscription payment failed."""
    paypal_sub_id = resource.get("id")
    if not paypal_sub_id:
        return

    container = current_app.container
    sub_repo = container.subscription_repository()
    subscription = sub_repo.find_by_provider_subscription_id(paypal_sub_id)
    if not subscription:
        return

    event = PaymentFailedEvent(
        subscription_id=subscription.id,
        user_id=subscription.user_id,
        error_code="payment_failed",
        error_message="PayPal subscription payment failed",
        provider="paypal",
    )
    container.event_dispatcher().emit(event)


# ---- Helpers ----


def _link_paypal_subscription(invoice_id, provider_subscription_id):
    """Store provider_subscription_id on our Subscription after activation."""
    container = current_app.container
    invoice_repo = container.invoice_repository()
    sub_repo = container.subscription_repository()

    invoice = invoice_repo.find_by_id(invoice_id)
    if not invoice:
        return

    for li in invoice.line_items:
        if li.item_type == LineItemType.SUBSCRIPTION:
            subscription = sub_repo.find_by_id(li.item_id)
            if subscription:
                subscription.provider_subscription_id = provider_subscription_id
                sub_repo.save(subscription)
                break


def _create_renewal_invoice(subscription, paypal_sale):
    """Create a renewal invoice from PayPal subscription sale event."""
    container = current_app.container
    invoice_repo = container.invoice_repository()

    # Deduplication
    sale_id = paypal_sale.get("id", "")
    existing = invoice_repo.find_by_provider_session_id(sale_id)
    if existing:
        return existing

    plan = subscription.tarif_plan
    amount = Decimal(paypal_sale.get("amount", {}).get("total", "0"))
    currency = paypal_sale.get("amount", {}).get("currency", "USD").upper()

    renewal_invoice = UserInvoice(
        user_id=subscription.user_id,
        tarif_plan_id=plan.id if plan else None,
        subscription_id=subscription.id,
        invoice_number=UserInvoice.generate_invoice_number(),
        amount=amount,
        total_amount=amount,
        currency=currency,
        status=InvoiceStatus.PENDING,
        payment_method="paypal",
        provider_session_id=sale_id,
    )
    renewal_invoice.line_items.append(
        InvoiceLineItem(
            item_type=LineItemType.SUBSCRIPTION,
            item_id=subscription.id,
            description=f"Renewal: {plan.name}" if plan else "Subscription renewal",
            quantity=1,
            unit_price=amount,
            total_price=amount,
        )
    )
    invoice_repo.save(renewal_invoice)
    return renewal_invoice


def _get_or_create_paypal_plan(adapter, invoice, config):
    """Get or create a PayPal Billing Plan for subscription items."""
    from src.extensions import db
    from src.models.subscription import Subscription

    for li in invoice.line_items:
        if li.item_type == LineItemType.SUBSCRIPTION:
            sub = db.session.get(Subscription, li.item_id)
            if sub and sub.tarif_plan and sub.tarif_plan.is_recurring:
                plan = sub.tarif_plan
                amount = str(li.unit_price)
                currency = (invoice.currency or "USD").upper()
                period = plan.billing_period.value
                interval = BILLING_PERIOD_TO_PAYPAL.get(
                    period, {"interval_unit": "MONTH", "interval_count": 1}
                )

                # Create product first
                product_resp = adapter.create_product(plan.name)
                if not product_resp.success:
                    return product_resp

                return adapter.create_billing_plan(
                    product_id=product_resp.data["product_id"],
                    name=plan.name,
                    amount=amount,
                    currency=currency,
                    interval=interval["interval_unit"],
                    interval_count=interval["interval_count"],
                )

    from src.sdk.interface import SDKResponse

    return SDKResponse(success=False, error="No recurring items found")


def _reconcile_payment(custom_id, order_id, response_data):
    """Emit PaymentCapturedEvent if PayPal says paid but our invoice is still PENDING."""
    try:
        invoice_id = UUID(custom_id)
    except (ValueError, TypeError):
        return

    container = current_app.container
    invoice_repo = container.invoice_repository()
    invoice = invoice_repo.find_by_id(invoice_id)
    if not invoice or invoice.status != InvoiceStatus.PENDING:
        return

    logger.info(
        "Reconciliation: PayPal order %s paid but invoice %s still PENDING",
        order_id,
        invoice_id,
    )
    emit_payment_captured(
        invoice_id=invoice_id,
        payment_reference=order_id,
        amount=response_data.get("amount_total", response_data.get("amount", "0")),
        currency=response_data.get("currency", "USD"),
        provider="paypal",
        transaction_id=order_id,
    )
