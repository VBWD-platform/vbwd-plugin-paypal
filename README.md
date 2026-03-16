# PayPal Plugin (Backend)

PayPal payment provider integration.

## Purpose

Implements the `IPaymentSdkAdapter` interface for PayPal. Provides order creation, payment capture, and webhook handling.

## Configuration (`plugins/config.json`)

```json
{
  "paypal": {
    "client_id": "...",
    "client_secret": "...",
    "mode": "sandbox",
    "return_url": "https://yourdomain.com/payment/success",
    "cancel_url": "https://yourdomain.com/payment/cancel"
  }
}
```

## API Routes

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/v1/paypal/create-order` | Bearer | Create PayPal order |
| POST | `/api/v1/paypal/capture/<order_id>` | Bearer | Capture approved payment |
| POST | `/api/v1/paypal/webhook` | PayPal signature | Receive PayPal IPN events |

## Events

Emits: `PaymentCapturedEvent` on successful capture.

## Database

No plugin-owned tables.

## Frontend Bundle

- User: `vbwd-fe-user/plugins/paypal/` (if present)

## Testing

```bash
docker compose run --rm test python -m pytest plugins/paypal/tests/ -v
```

---

## Related

| | Repository |
|-|------------|
| 👤 Frontend (user) | [vbwd-fe-user-plugin-paypal-payment](https://github.com/VBWD-platform/vbwd-fe-user-plugin-paypal-payment) |

**Core:** [vbwd-backend](https://github.com/VBWD-platform/vbwd-backend)
