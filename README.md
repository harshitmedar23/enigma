# Subscription Billing & Usage Metering Portal

A demo Flask marketplace and seller billing portal. The backend uses Flask,
Flask-SQLAlchemy, PyMySQL, and JWT; the frontend is plain HTML, CSS, and
JavaScript served from `static/`.

## Requirements

- Python 3.10+
- MySQL running locally
- A MySQL database named `saas_billing`

Create the database once in MySQL Workbench:

```sql
CREATE DATABASE saas_billing;
```

The application reads `SECRET_KEY`, `DATABASE_URL`, and `PORT` from the
existing `.env` file. Keep `.env` private and do not commit it. The expected
database URL uses the `mysql+pymysql` driver and the `saas_billing` database.

## Run on Windows

From the project directory:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

On first start, the app creates the tables and seeds plans, demo accounts, and
sample products. Open `http://127.0.0.1:5000`.

## Demo accounts

- Admin: `admin@demo.com` / `admin123`
- Sellers: `seller1@demo.com` or `seller2@demo.com` / `seller123`

## Plans and usage

- Free: INR 0, 1,000 tokens, overage blocked.
- Pro: INR 999/month, 50,000 tokens, INR 0.05 per overage token.
- Business: INR 4,999/month, 500,000 tokens, INR 0.02 per overage token.
- Reach/impression: 1 token; click: 2 tokens; buy: 5 tokens.

Sellers can manage products and promotions, inspect analytics and invoices,
and simulate shopper traffic. The seller plan flow is a local demo: clicking
Free, Pro, or Business activates that plan immediately, resets usage for a plan
change, and creates a clearly marked sample invoice. No Razorpay checkout,
payment credential, card data, or payment is involved in this seller flow.
Repeatedly selecting the currently active plan does not create another invoice.
Invoice downloads are self-contained `.html` files with a Print / Save as PDF
button and an explicit sample/unpaid status.

The admin can review revenue and usage, then close the usage cycle to issue
overage invoices and reset active token usage. Only invoices with an actual captured payment status count toward admin
revenue. Demo activation invoices and usage-cycle invoices remain
`sample` or `pending` and are excluded from revenue.

## Razorpay sandbox setup

Razorpay remains available for existing provider endpoints and unrelated
integration testing, but it is not used by the seller demo plan flow. If you
want to exercise those legacy endpoints, create Razorpay **Test Mode** API
credentials in the Razorpay Dashboard, then add these values to the local
`.env` file (never commit or share the secrets):

```dotenv
RAZORPAY_KEY_ID=rzp_test_your_key_id
RAZORPAY_KEY_SECRET=your_test_key_secret
RAZORPAY_WEBHOOK_SECRET=your_test_webhook_secret
RAZORPAY_TOTAL_COUNT=120
```

Restart `python app.py` after changing `.env`. The seller dashboard's
**Change plan** button activates a plan directly and never opens Razorpay.
Set up a Razorpay webhook pointing to `https://<your-public-host>/api/billing/webhook`
with the same webhook secret, and enable subscription and payment events,
including `subscription.activated`, `subscription.charged`,
`subscription.halted`, `subscription.cancelled`, `payment.captured`, and
`payment.failed`. A local `127.0.0.1` address cannot receive Razorpay webhooks;
use a public test/staging HTTPS host for webhook testing. The default 120
billing cycles represent 10 years of monthly renewal.

Without Razorpay credentials, the app still starts and the seller demo flow
works for every seeded plan. Legacy provider checkout endpoints may return a
configuration error rather than simulating a successful payment. Sandbox
payments require Test Mode credentials. Replace them with live credentials
only after end-to-end testing and webhook setup.

## API notes

Seller/admin pages use JWT bearer authentication. Usage recording accepts an
API key in the `x-api-key` header and requires a unique idempotency key.
Marketplace impressions and shopper actions are recorded by the server.
Seller demo activation is `POST /api/subscribe`; it does not use Razorpay.
Legacy paid plan checkout is created at `POST /api/billing/checkout`, then
verified at `POST /api/billing/verify`. Razorpay posts signed lifecycle events to
`POST /api/billing/webhook`; seller payment and audit history is available at
`GET /api/billing/history`.
