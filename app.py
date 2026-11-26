import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from functools import wraps
from html import escape
from pathlib import Path

import jwt
from dotenv import load_dotenv
from flask import Flask, current_app, g, jsonify, request, send_from_directory
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, inspect, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from werkzeug.security import check_password_hash, generate_password_hash


load_dotenv(Path(__file__).with_name(".env"), override=False)
db = SQLAlchemy()

EVENT_TOKENS = {"impression": 1, "click": 2, "buy": 5}
PRODUCTS = [
    ("Ceramic Pour-Over Set", 48.00, "photo-1495474472287-4d71bcdd2085"),
    ("Everyday Linen Tote", 32.00, "photo-1544816155-12df9643f363"),
    ("Hand-thrown Stoneware Mug", 28.00, "photo-1514228742587-6b1558fcca3d"),
    ("Botanical Desk Journal", 24.00, "photo-1517842645767-c639042777db"),
    ("Woven Market Basket", 56.00, "photo-1590736969955-71cc94901144"),
    ("Amber Glass Carafe", 42.00, "photo-1513558161293-cdab7659a3b6"),
    ("Minimalist Table Lamp", 89.00, "photo-1507473885765-e6ed057f782c"),
    ("Organic Cotton Throw", 76.00, "photo-1600210492486-724fe5c67fb0"),
]


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="seller")


class Plan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(40), unique=True, nullable=False)
    price = db.Column(db.Float, nullable=False)
    token_quota = db.Column(db.Integer, nullable=False)
    overage_rate = db.Column(db.Float, nullable=False, default=0)
    allow_overage = db.Column(db.Boolean, nullable=False, default=False)


class Subscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("plan.id"), nullable=False)
    tokens_used = db.Column(db.Integer, nullable=False, default=0)
    overage_tokens = db.Column(db.Integer, nullable=False, default=0)
    cycle_start = db.Column(db.DateTime, nullable=False, default=utcnow)
    status = db.Column(db.String(20), nullable=False, default="active")
    seller = db.relationship("User")
    plan = db.relationship("Plan")


class ApiKey(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True)
    api_key = db.Column(db.String(100), nullable=False, unique=True)


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    name = db.Column(db.String(160), nullable=False)
    price = db.Column(db.Float, nullable=False)
    image_url = db.Column(db.String(500), nullable=False, default="")
    promoted = db.Column(db.Boolean, nullable=False, default=False)


class UsageEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    event_type = db.Column(db.String(20), nullable=False)
    tokens = db.Column(db.Integer, nullable=False)
    is_overage = db.Column(db.Boolean, nullable=False, default=False)
    idempotency_key = db.Column(db.String(160), nullable=False, unique=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    product = db.relationship("Product")


class Invoice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("plan.id"), nullable=True, index=True)
    plan_fee = db.Column(db.Float, nullable=False)
    overage_tokens = db.Column(db.Integer, nullable=False)
    overage_amount = db.Column(db.Float, nullable=False)
    total = db.Column(db.Float, nullable=False)
    period = db.Column(db.String(40), nullable=False)
    status = db.Column(db.String(24), nullable=False, default="pending")
    invoice_type = db.Column(db.String(24), nullable=False, default="usage")
    activation_key = db.Column(db.String(160), unique=True, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class RazorpayPlanLink(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("plan.id"), nullable=False, unique=True)
    provider_plan_id = db.Column(db.String(80), nullable=False, unique=True)


class GatewaySubscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("plan.id"), nullable=False)
    provider_subscription_id = db.Column(db.String(80), nullable=False, unique=True)
    status = db.Column(db.String(24), nullable=False, default="created", index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    seller = db.relationship("User")
    plan = db.relationship("Plan")


class PaymentTransaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("plan.id"), nullable=False)
    provider_subscription_id = db.Column(db.String(80), nullable=False, index=True)
    provider_payment_id = db.Column(db.String(80), unique=True)
    amount_paise = db.Column(db.Integer, nullable=False)
    currency = db.Column(db.String(3), nullable=False, default="INR")
    status = db.Column(db.String(24), nullable=False, default="created", index=True)
    failure_reason = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    plan = db.relationship("Plan")


class BillingEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    seller_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("plan.id"))
    provider_event_id = db.Column(db.String(120), unique=True)
    provider_subscription_id = db.Column(db.String(80), index=True)
    provider_payment_id = db.Column(db.String(80), index=True)
    event_type = db.Column(db.String(80), nullable=False)
    source = db.Column(db.String(24), nullable=False)
    status = db.Column(db.String(24), nullable=False)
    details = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)


def _billing_event(
    session,
    event_type,
    source,
    status,
    seller_id=None,
    plan_id=None,
    provider_subscription_id=None,
    provider_payment_id=None,
    provider_event_id=None,
    details=None,
):
    session.add(
        BillingEvent(
            seller_id=seller_id,
            plan_id=plan_id,
            provider_event_id=provider_event_id,
            provider_subscription_id=provider_subscription_id,
            provider_payment_id=provider_payment_id,
            event_type=event_type[:80],
            source=source[:24],
            status=status[:24],
            details=json.dumps(details or {}, separators=(",", ":")),
        )
    )


def _razorpay_credentials():
    key_id = current_app.config.get("RAZORPAY_KEY_ID")
    key_secret = current_app.config.get("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        raise RazorpayConfigurationError(
            "Razorpay sandbox is not configured. Set RAZORPAY_KEY_ID and "
            "RAZORPAY_KEY_SECRET in .env, then restart the app."
        )
    return key_id, key_secret


def _razorpay_request(method, path, payload=None):
    key_id, key_secret = _razorpay_credentials()
    auth_value = base64.b64encode(
        f"{key_id}:{key_secret}".encode("utf-8")
    ).decode("ascii")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    http_request = urllib.request.Request(
        "https://api.razorpay.com/v1" + path,
        data=body,
        method=method,
        headers={
            "Authorization": "Basic " + auth_value,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(http_request, timeout=20) as response:
            response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        response_body = error.read().decode("utf-8", errors="replace")
        try:
            provider_error = json.loads(response_body).get("error", {})
            message = provider_error.get("description") or provider_error.get("code")
        except (ValueError, AttributeError):
            message = None
        raise RazorpayError(
            message or f"Razorpay returned HTTP {error.code}"
        ) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise RazorpayError("Could not reach Razorpay. Please try again.") from error
    try:
        return json.loads(response_body) if response_body else {}
    except ValueError as error:
        raise RazorpayError("Razorpay returned an unreadable response.") from error


def _amount_in_paise(amount):
    return int(
        (Decimal(str(amount)) * Decimal("100")).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def _is_razorpay_id(value, prefix):
    return (
        isinstance(value, str)
        and value.startswith(prefix)
        and value[len(prefix) :].isalnum()
        and len(value) <= 80
    )


def _ensure_razorpay_plan(plan):
    link = db.session.scalar(
        select(RazorpayPlanLink).where(RazorpayPlanLink.plan_id == plan.id)
    )
    if link:
        return link.provider_plan_id
    response = _razorpay_request(
        "POST",
        "/plans",
        {
            "period": "monthly",
            "interval": 1,
            "item": {
                "name": f"Morrow {plan.name} monthly",
                "description": f"{plan.token_quota} monthly usage tokens",
                "amount": _amount_in_paise(plan.price),
                "currency": "INR",
            },
            "notes": {"morrow_plan_id": str(plan.id)},
        },
    )
    provider_plan_id = response.get("id")
    if not _is_razorpay_id(provider_plan_id, "plan_"):
        raise RazorpayError("Razorpay did not return a subscription plan ID.")
    link = RazorpayPlanLink(plan_id=plan.id, provider_plan_id=provider_plan_id)
    db.session.add(link)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = db.session.scalar(
            select(RazorpayPlanLink).where(RazorpayPlanLink.plan_id == plan.id)
        )
        if existing:
            return existing.provider_plan_id
        raise
    return provider_plan_id


def _cancel_provider_subscription(provider_subscription_id):
    return _razorpay_request(
        "POST",
        f"/subscriptions/{provider_subscription_id}/cancel",
        {"cancel_at_cycle_end": False},
    )


def _apply_captured_subscription_payment(
    provider_subscription_id, payment_data, source, provider_event_id=None
):
    payment_id = payment_data.get("id")
    if not payment_id:
        raise RazorpayError("Payment record is missing its provider payment ID.")
    with Session(db.engine) as session:
        with session.begin():
            gateway_subscription = session.scalar(
                select(GatewaySubscription)
                .where(
                    GatewaySubscription.provider_subscription_id
                    == provider_subscription_id
                )
                .with_for_update()
            )
            if gateway_subscription is None:
                _billing_event(
                    session,
                    "payment.captured.unmatched",
                    source,
                    "unmatched",
                    provider_subscription_id=provider_subscription_id,
                    provider_payment_id=payment_id,
                    provider_event_id=provider_event_id,
                )
                return {"activated": False, "reason": "Unknown subscription"}

            existing_payment = session.scalar(
                select(PaymentTransaction)
                .where(PaymentTransaction.provider_payment_id == payment_id)
                .with_for_update()
            )
            if existing_payment and existing_payment.status == "captured":
                _billing_event(
                    session,
                    "payment.duplicate_confirmation",
                    source,
                    "duplicate",
                    seller_id=gateway_subscription.seller_id,
                    plan_id=gateway_subscription.plan_id,
                    provider_subscription_id=provider_subscription_id,
                    provider_payment_id=payment_id,
                    provider_event_id=provider_event_id,
                )
                return {
                    "activated": True,
                    "duplicate": True,
                    "seller_id": gateway_subscription.seller_id,
                }
            if existing_payment is None:
                existing_payment = session.scalar(
                    select(PaymentTransaction)
                    .where(
                        PaymentTransaction.provider_subscription_id
                        == provider_subscription_id,
                        PaymentTransaction.provider_payment_id.is_(None),
                        PaymentTransaction.status.in_(("created", "authenticated")),
                    )
                    .order_by(PaymentTransaction.id.desc())
                    .with_for_update()
                )

            plan = session.get(Plan, gateway_subscription.plan_id)
            if plan is None:
                raise RazorpayError("The billed plan no longer exists.")
            expected_amount = _amount_in_paise(plan.price)
            received_amount = int(payment_data.get("amount") or 0)
            currency = str(payment_data.get("currency") or "").upper()
            if received_amount != expected_amount or currency != "INR":
                _billing_event(
                    session,
                    "payment.captured.amount_mismatch",
                    source,
                    "rejected",
                    seller_id=gateway_subscription.seller_id,
                    plan_id=plan.id,
                    provider_subscription_id=provider_subscription_id,
                    provider_payment_id=payment_id,
                    provider_event_id=provider_event_id,
                    details={
                        "expected_amount_paise": expected_amount,
                        "received_amount_paise": received_amount,
                        "expected_currency": "INR",
                        "received_currency": currency,
                    },
                )
                return {"activated": False, "reason": "Payment amount mismatch"}

            if existing_payment is None:
                existing_payment = PaymentTransaction(
                    seller_id=gateway_subscription.seller_id,
                    plan_id=plan.id,
                    provider_subscription_id=provider_subscription_id,
                    provider_payment_id=payment_id,
                    amount_paise=received_amount,
                    currency=currency,
                    status="captured",
                )
                session.add(existing_payment)
            else:
                existing_payment.provider_payment_id = payment_id
                existing_payment.status = "captured"
                existing_payment.amount_paise = received_amount
                existing_payment.currency = currency
                existing_payment.failure_reason = None
                existing_payment.updated_at = utcnow()

            now = utcnow()
            newer_active_subscription = session.scalar(
                select(GatewaySubscription.id)
                .where(
                    GatewaySubscription.seller_id == gateway_subscription.seller_id,
                    GatewaySubscription.id != gateway_subscription.id,
                    GatewaySubscription.status == "active",
                )
                .limit(1)
            )
            subscription = session.scalar(
                select(Subscription)
                .where(
                    Subscription.seller_id == gateway_subscription.seller_id,
                    Subscription.status == "active",
                )
                .order_by(Subscription.id.desc())
                .with_for_update()
            )
            previous_cycle_start = subscription.cycle_start if subscription else now
            amount = Decimal(received_amount) / Decimal("100")
            session.add(
                Invoice(
                    seller_id=gateway_subscription.seller_id,
                    plan_id=gateway_subscription.plan_id,
                    plan_fee=float(amount),
                    overage_tokens=0,
                    overage_amount=0,
                    total=float(amount),
                    period=f"{previous_cycle_start:%Y-%m-%d} to {now:%Y-%m-%d}",
                    status="paid",
                    invoice_type="subscription",
                    created_at=now,
                )
            )
            if newer_active_subscription or gateway_subscription.status in (
                "cancelled",
                "completed",
                "expired",
            ):
                _billing_event(
                    session,
                    "payment.captured.superseded",
                    source,
                    "captured_no_entitlement",
                    seller_id=gateway_subscription.seller_id,
                    plan_id=plan.id,
                    provider_subscription_id=provider_subscription_id,
                    provider_payment_id=payment_id,
                    provider_event_id=provider_event_id,
                    details={
                        "reason": "A newer or cancelled subscription prevents "
                        "this payment from changing the active plan.",
                        "amount_paise": received_amount,
                        "currency": currency,
                    },
                )
                return {
                    "activated": False,
                    "captured": True,
                    "reason": "Payment was captured, but its subscription was "
                    "already superseded or cancelled.",
                    "seller_id": gateway_subscription.seller_id,
                }
            if subscription is None:
                subscription = Subscription(
                    seller_id=gateway_subscription.seller_id,
                    plan_id=plan.id,
                    cycle_start=now,
                    status="active",
                )
                session.add(subscription)
            else:
                subscription.plan_id = plan.id
                subscription.tokens_used = 0
                subscription.overage_tokens = 0
                subscription.cycle_start = now
                subscription.status = "active"

            gateway_subscription.status = "active"
            gateway_subscription.updated_at = now
            _ensure_api_key(session, gateway_subscription.seller_id)
            _billing_event(
                session,
                "payment.captured",
                source,
                "captured",
                seller_id=gateway_subscription.seller_id,
                plan_id=plan.id,
                provider_subscription_id=provider_subscription_id,
                provider_payment_id=payment_id,
                provider_event_id=provider_event_id,
                details={"amount_paise": received_amount, "currency": currency},
            )
            session.flush()
            return {
                "activated": True,
                "duplicate": False,
                "seller_id": gateway_subscription.seller_id,
                "gateway_subscription_id": gateway_subscription.id,
            }


def _cancel_superseded_subscriptions(seller_id, keep_provider_subscription_id):
    previous_subscriptions = db.session.scalars(
        select(GatewaySubscription).where(
            GatewaySubscription.seller_id == seller_id,
            GatewaySubscription.provider_subscription_id
            != keep_provider_subscription_id,
            GatewaySubscription.status.in_(
                ("created", "authenticated", "pending", "active", "cancel_pending")
            ),
        )
    ).all()
    warnings = []
    for previous in previous_subscriptions:
        try:
            _cancel_provider_subscription(previous.provider_subscription_id)
        except RazorpayError as exc:
            previous.status = "cancel_pending"
            warnings.append(
                "Could not cancel previous Razorpay subscription "
                f"{previous.provider_subscription_id}: {exc}"
            )
            _billing_event(
                db.session,
                "subscription.cancel_failed",
                "api",
                "retry_required",
                seller_id=seller_id,
                plan_id=previous.plan_id,
                provider_subscription_id=previous.provider_subscription_id,
                details={"reason": str(exc)},
            )
        else:
            previous.status = "cancelled"
            _billing_event(
                db.session,
                "subscription.cancelled",
                "api",
                "cancelled",
                seller_id=seller_id,
                plan_id=previous.plan_id,
                provider_subscription_id=previous.provider_subscription_id,
            )
        previous.updated_at = utcnow()
    if previous_subscriptions:
        db.session.commit()
    return warnings


class QuotaExceeded(Exception):
    pass


class RazorpayError(Exception):
    pass


class RazorpayConfigurationError(RazorpayError):
    pass


def _json_error(message, status):
    return jsonify({"error": message}), status


def _serialize_product(product):
    return {
        "id": product.id,
        "seller_id": product.seller_id,
        "name": product.name,
        "price": product.price,
        "image_url": product.image_url,
        "promoted": product.promoted,
    }


def _serialize_plan(plan):
    return {
        "id": plan.id,
        "name": plan.name,
        "price": plan.price,
        "currency": "INR",
        "token_quota": plan.token_quota,
        "overage_rate": plan.overage_rate,
        "allow_overage": plan.allow_overage,
    }


def _new_api_key():
    return "sk_live_" + secrets.token_urlsafe(32)


def _ensure_api_key(session, seller_id):
    key = session.scalar(select(ApiKey).where(ApiKey.seller_id == seller_id))
    if key is None:
        key = ApiKey(seller_id=seller_id, api_key=_new_api_key())
        session.add(key)
        session.flush()
    return key.api_key


def _record_usage(seller_id, product_id, event_type, idempotency_key):
    token_cost = EVENT_TOKENS[event_type]
    with Session(db.engine) as session:
        with session.begin():
            duplicate = session.scalar(
                select(UsageEvent.id).where(
                    UsageEvent.idempotency_key == idempotency_key
                )
            )
            if duplicate is not None:
                return {"duplicate": True, "tokens": 0, "is_overage": False}

            product = session.get(Product, product_id)
            if product is None or product.seller_id != seller_id:
                raise ValueError("Product not found for this seller")
            subscription = session.scalar(
                select(Subscription)
                .where(
                    Subscription.seller_id == seller_id,
                    Subscription.status == "active",
                )
                .order_by(Subscription.id.desc())
                .with_for_update()
            )
            if subscription is None:
                raise ValueError("Seller has no active subscription")

            plan = session.get(Plan, subscription.plan_id)
            if plan is None:
                raise ValueError("Subscription plan was not found")
            if plan.price > 0:
                payment_confirmed = session.scalar(
                    select(PaymentTransaction.id)
                    .join(
                        GatewaySubscription,
                        GatewaySubscription.provider_subscription_id
                        == PaymentTransaction.provider_subscription_id,
                    )
                    .where(
                        GatewaySubscription.seller_id == seller_id,
                        GatewaySubscription.plan_id == plan.id,
                        GatewaySubscription.status == "active",
                        PaymentTransaction.seller_id == seller_id,
                        PaymentTransaction.plan_id == plan.id,
                        PaymentTransaction.status == "captured",
                    )
                    .limit(1)
                )
                if payment_confirmed is None:
                    raise ValueError(
                        "The paid plan is not active because Razorpay payment "
                        "has not been verified."
                    )
            current_usage = subscription.tokens_used
            exceeds_quota = current_usage + token_cost > plan.token_quota
            if exceeds_quota and not plan.allow_overage:
                session.query(Product).filter_by(seller_id=seller_id).update(
                    {Product.promoted: False}, synchronize_session=False
                )
                return {"blocked": True, "tokens": 0, "is_overage": False}

            overage_before = max(0, current_usage - plan.token_quota)
            overage_after = max(
                0, current_usage + token_cost - plan.token_quota
            )
            is_overage = overage_after > overage_before
            subscription.tokens_used += token_cost
            subscription.overage_tokens += overage_after - overage_before
            session.add(
                UsageEvent(
                    seller_id=seller_id,
                    product_id=product_id,
                    event_type=event_type,
                    tokens=token_cost,
                    is_overage=is_overage,
                    idempotency_key=idempotency_key,
                )
            )
            session.flush()
            return {
                "duplicate": False,
                "tokens": token_cost,
                "is_overage": is_overage,
            }


def _role_required(*roles):
    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            auth_header = request.headers.get("Authorization", "")
            token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
            if not token:
                return _json_error("Authentication required", 401)
            try:
                payload = jwt.decode(
                    token, current_app.config["SECRET_KEY"], algorithms=["HS256"]
                )
                user_id = int(payload["sub"])
            except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
                return _json_error("Invalid or expired token", 401)
            user = db.session.get(User, user_id)
            if user is None:
                return _json_error("User no longer exists", 401)
            if user.role not in roles:
                return _json_error("You do not have permission to access this resource", 403)
            g.current_user = user
            return view(*args, **kwargs)

        return wrapped

    return decorate


def create_app(test_config=None):
    secret_key = os.environ.get("SECRET_KEY")
    database_url = os.environ.get("DATABASE_URL")
    if not secret_key:
        raise RuntimeError("SECRET_KEY is missing from the environment or .env file")
    if not database_url:
        raise RuntimeError("DATABASE_URL is missing from the environment or .env file")

    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.config.update(
        SECRET_KEY=secret_key,
        SQLALCHEMY_DATABASE_URI=database_url,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        JSON_SORT_KEYS=False,
        RAZORPAY_KEY_ID=os.environ.get("RAZORPAY_KEY_ID", ""),
        RAZORPAY_KEY_SECRET=os.environ.get("RAZORPAY_KEY_SECRET", ""),
        RAZORPAY_WEBHOOK_SECRET=os.environ.get("RAZORPAY_WEBHOOK_SECRET", ""),
        RAZORPAY_TOTAL_COUNT=int(os.environ.get("RAZORPAY_TOTAL_COUNT", "120")),
    )
    if test_config:
        app.config.update(test_config)
    db.init_app(app)
    CORS(app, resources={r"/api/*": {"origins": ["http://localhost:5000", "http://127.0.0.1:5000"]}})

    @app.get("/")
    def marketplace_page():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/login")
    @app.get("/login.html")
    def login_page():
        return send_from_directory(app.static_folder, "login.html")

    @app.get("/seller")
    @app.get("/seller.html")
    def seller_page():
        return send_from_directory(app.static_folder, "seller.html")

    @app.get("/admin")
    @app.get("/admin.html")
    def admin_page():
        return send_from_directory(app.static_folder, "admin.html")

    @app.post("/api/auth/register")
    def register():
        data = request.get_json(silent=True) or {}
        name = str(data.get("name", "")).strip()
        email = str(data.get("email", "")).strip().lower()
        password = data.get("password", "")
        if not name or len(name) > 120 or not email or len(email) > 255:
            return _json_error("Name and a valid email are required", 400)
        if not isinstance(password, str) or len(password) < 8:
            return _json_error("Password must be at least 8 characters", 400)
        if db.session.scalar(select(User.id).where(User.email == email)):
            return _json_error("An account with this email already exists", 409)
        user = User(
            name=name,
            email=email,
            password_hash=generate_password_hash(password),
            role="seller",
        )
        db.session.add(user)
        db.session.commit()
        return _auth_response(user), 201

    @app.post("/api/auth/login")
    def login():
        data = request.get_json(silent=True) or {}
        email = str(data.get("email", "")).strip().lower()
        password = data.get("password", "")
        user = db.session.scalar(select(User).where(User.email == email))
        if user is None or not isinstance(password, str) or not check_password_hash(
            user.password_hash, password
        ):
            return _json_error("Email or password is incorrect", 401)
        return _auth_response(user)

    def _auth_response(user):
        token = jwt.encode(
            {
                "sub": str(user.id),
                "role": user.role,
                "iat": utcnow(),
                "exp": utcnow() + timedelta(hours=12),
            },
            app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        return jsonify(
            {
                "token": token,
                "user": {
                    "id": user.id,
                    "name": user.name,
                    "email": user.email,
                    "role": user.role,
                },
            }
        )

    @app.get("/api/plans")
    def get_plans():
        return jsonify([_serialize_plan(p) for p in db.session.scalars(select(Plan).order_by(Plan.price))])

    @app.post("/api/subscribe")
    @_role_required("seller")
    def subscribe():
        data = request.get_json(silent=True) or {}
        plan = db.session.get(Plan, data.get("plan_id"))
        if plan is None:
            return _json_error("Plan not found", 404)
        subscription = db.session.scalar(
            select(Subscription)
            .where(Subscription.seller_id == g.current_user.id)
            .order_by(Subscription.id.desc())
            .with_for_update()
        )
        now = utcnow()
        changed = subscription is None or subscription.plan_id != plan.id or subscription.status != "active"
        if subscription is None:
            subscription = Subscription(seller_id=g.current_user.id, plan_id=plan.id)
            db.session.add(subscription)
        elif subscription.plan_id != plan.id or subscription.status != "active":
            subscription.plan_id = plan.id
            subscription.tokens_used = 0
            subscription.overage_tokens = 0
            subscription.cycle_start = now
            subscription.status = "active"

        if changed:
            db.session.add(
                Invoice(
                    seller_id=g.current_user.id,
                    plan_id=plan.id,
                    plan_fee=float(plan.price),
                    overage_tokens=0,
                    overage_amount=0,
                    total=float(plan.price),
                    period=f"{now:%Y-%m-%d} demo activation",
                    status="sample",
                    invoice_type="demo_activation",
                    activation_key=f"demo:{g.current_user.id}:{plan.id}:{secrets.token_hex(10)}",
                    created_at=now,
                )
            )
        api_key = _ensure_api_key(db.session, g.current_user.id)
        _billing_event(
            db.session,
            "subscription.demo_activated",
            "seller_api",
            "active",
            seller_id=g.current_user.id,
            plan_id=plan.id,
            details={"demo": True, "payment_taken": False},
        )
        db.session.commit()
        return jsonify(
            {
                "message": "Plan activated in demo mode",
                "plan": _serialize_plan(plan),
                "api_key": api_key,
                "demo": True,
                "payment_taken": False,
                "invoice_created": changed,
                "usage_reset": changed,
            }
        )

    @app.post("/api/billing/checkout")
    @_role_required("seller")
    def billing_checkout():
        data = request.get_json(silent=True) or {}
        try:
            plan_id = int(data.get("plan_id"))
        except (TypeError, ValueError):
            return _json_error("plan_id must be an integer", 400)
        plan = db.session.get(Plan, plan_id)
        if plan is None:
            return _json_error("Plan not found", 404)
        if plan.price <= 0:
            return _json_error("Free plans do not require payment checkout.", 400)
        try:
            key_id, _ = _razorpay_credentials()
            provider_plan_id = _ensure_razorpay_plan(plan)
            current_gateway = db.session.scalar(
                select(GatewaySubscription)
                .where(
                    GatewaySubscription.seller_id == g.current_user.id,
                    GatewaySubscription.plan_id == plan.id,
                    GatewaySubscription.status == "active",
                )
                .order_by(GatewaySubscription.id.desc())
            )
            if current_gateway:
                return _json_error(
                    "This plan already has an active Razorpay subscription.",
                    409,
                )
            existing_pending = db.session.scalar(
                select(GatewaySubscription)
                .where(
                    GatewaySubscription.seller_id == g.current_user.id,
                    GatewaySubscription.plan_id == plan.id,
                    GatewaySubscription.status.in_(("created", "authenticated")),
                )
                .order_by(GatewaySubscription.id.desc())
            )
            if existing_pending:
                provider_subscription_id = (
                    existing_pending.provider_subscription_id
                )
            else:
                provider_response = _razorpay_request(
                    "POST",
                    "/subscriptions",
                    {
                        "plan_id": provider_plan_id,
                        "total_count": app.config["RAZORPAY_TOTAL_COUNT"],
                        "quantity": 1,
                        "customer_notify": True,
                        "notes": {
                            "seller_id": str(g.current_user.id),
                            "local_plan_id": str(plan.id),
                        },
                    },
                )
                provider_subscription_id = provider_response.get("id")
                if not _is_razorpay_id(provider_subscription_id, "sub_"):
                    raise RazorpayError(
                        "Razorpay did not return a subscription ID."
                    )
                gateway_subscription = GatewaySubscription(
                    seller_id=g.current_user.id,
                    plan_id=plan.id,
                    provider_subscription_id=provider_subscription_id,
                    status=provider_response.get("status", "created"),
                )
                db.session.add(gateway_subscription)
                db.session.add(
                    PaymentTransaction(
                        seller_id=g.current_user.id,
                        plan_id=plan.id,
                        provider_subscription_id=provider_subscription_id,
                        amount_paise=_amount_in_paise(plan.price),
                        currency="INR",
                        status="created",
                    )
                )
                _billing_event(
                    db.session,
                    "checkout.created",
                    "api",
                    "created",
                    seller_id=g.current_user.id,
                    plan_id=plan.id,
                    provider_subscription_id=provider_subscription_id,
                    details={
                        "amount_paise": _amount_in_paise(plan.price),
                        "currency": "INR",
                    },
                )
                db.session.commit()
        except RazorpayConfigurationError as exc:
            db.session.rollback()
            return _json_error(str(exc), 503)
        except RazorpayError as exc:
            db.session.rollback()
            return _json_error(str(exc), 502)
        except IntegrityError:
            db.session.rollback()
            return _json_error(
                "A billing checkout was created concurrently. Refresh and retry.",
                409,
            )
        return jsonify(
            {
                "key_id": key_id,
                "subscription_id": provider_subscription_id,
                "plan": _serialize_plan(plan),
                "amount": _amount_in_paise(plan.price),
                "currency": "INR",
                "billing_cycles": app.config["RAZORPAY_TOTAL_COUNT"],
                "customer": {
                    "name": g.current_user.name,
                    "email": g.current_user.email,
                },
            }
        )

    @app.post("/api/billing/verify")
    @_role_required("seller")
    def billing_verify():
        data = request.get_json(silent=True) or {}
        provider_subscription_id = data.get("razorpay_subscription_id")
        payment_id = data.get("razorpay_payment_id")
        signature = data.get("razorpay_signature")
        if not all((
            _is_razorpay_id(provider_subscription_id, "sub_"),
            _is_razorpay_id(payment_id, "pay_"),
            isinstance(signature, str)
            and len(signature) == 64
            and all(character in "0123456789abcdefABCDEF" for character in signature),
        )):
            return _json_error("Razorpay payment details are incomplete.", 400)
        gateway_subscription = db.session.scalar(
            select(GatewaySubscription).where(
                GatewaySubscription.provider_subscription_id
                == provider_subscription_id,
                GatewaySubscription.seller_id == g.current_user.id,
            )
        )
        if gateway_subscription is None:
            return _json_error("Subscription checkout was not found.", 404)
        try:
            _, key_secret = _razorpay_credentials()
        except RazorpayError as exc:
            return _json_error(str(exc), 503)
        expected_signature = hmac.new(
            key_secret.encode("utf-8"),
            f"{payment_id}|{provider_subscription_id}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected_signature, signature):
            _billing_event(
                db.session,
                "checkout.signature_rejected",
                "checkout",
                "rejected",
                seller_id=g.current_user.id,
                plan_id=gateway_subscription.plan_id,
                provider_subscription_id=provider_subscription_id,
                provider_payment_id=payment_id,
            )
            db.session.commit()
            return _json_error("Payment signature verification failed.", 400)
        try:
            payment_data = _razorpay_request("GET", f"/payments/{payment_id}")
        except RazorpayError as exc:
            _billing_event(
                db.session,
                "checkout.verification_unavailable",
                "checkout",
                "retry",
                seller_id=g.current_user.id,
                plan_id=gateway_subscription.plan_id,
                provider_subscription_id=provider_subscription_id,
                provider_payment_id=payment_id,
                details={"reason": str(exc)},
            )
            db.session.commit()
            return _json_error(
                "Signature is valid, but payment status could not be confirmed. "
                "Please retry verification in a moment.",
                502,
            )
        linked_subscription_id = payment_data.get("subscription_id")
        if linked_subscription_id and linked_subscription_id != provider_subscription_id:
            return _json_error("Payment does not match this subscription.", 400)
        if payment_data.get("status") != "captured":
            gateway_subscription.status = "authenticated"
            gateway_subscription.updated_at = utcnow()
            _billing_event(
                db.session,
                "checkout.payment_pending",
                "checkout",
                str(payment_data.get("status") or "pending"),
                seller_id=g.current_user.id,
                plan_id=gateway_subscription.plan_id,
                provider_subscription_id=provider_subscription_id,
                provider_payment_id=payment_id,
            )
            db.session.commit()
            return (
                jsonify(
                    {
                        "status": "processing",
                        "message": "Payment is processing. The subscription will "
                        "activate after Razorpay confirms the charge.",
                    }
                ),
                202,
            )
        payment_data["id"] = payment_id
        outcome = _apply_captured_subscription_payment(
            provider_subscription_id, payment_data, "checkout"
        )
        if outcome.get("captured"):
            return (
                jsonify(
                    {
                        "status": "captured_no_activation",
                        "message": outcome["reason"],
                    }
                ),
                202,
            )
        if not outcome.get("activated"):
            return _json_error(outcome.get("reason", "Payment could not be applied."), 409)
        cancellation_warnings = _cancel_superseded_subscriptions(
            g.current_user.id, provider_subscription_id
        )
        return jsonify(
            {
                "status": "active",
                "message": "Payment verified and subscription activated.",
                "cancellation_warnings": cancellation_warnings,
            }
        )

    @app.post("/api/billing/checkout-event")
    @_role_required("seller")
    def billing_checkout_event():
        data = request.get_json(silent=True) or {}
        event_type = data.get("event_type")
        provider_subscription_id = data.get("razorpay_subscription_id")
        if (
            event_type not in ("checkout.dismissed", "payment.failed")
            or not _is_razorpay_id(provider_subscription_id, "sub_")
        ):
            return _json_error("Unsupported checkout event.", 400)
        gateway_subscription = db.session.scalar(
            select(GatewaySubscription).where(
                GatewaySubscription.provider_subscription_id
                == provider_subscription_id,
                GatewaySubscription.seller_id == g.current_user.id,
            )
        )
        if gateway_subscription is None:
            return _json_error("Subscription checkout was not found.", 404)
        _billing_event(
            db.session,
            event_type,
            "checkout_client",
            "reported",
            seller_id=g.current_user.id,
            plan_id=gateway_subscription.plan_id,
            provider_subscription_id=provider_subscription_id,
            provider_payment_id=data.get("razorpay_payment_id"),
            details={
                "reason": str(data.get("reason", ""))[:300],
            },
        )
        db.session.commit()
        return jsonify({"message": "Billing event recorded."})

    @app.post("/api/billing/webhook")
    def billing_webhook():
        webhook_secret = app.config.get("RAZORPAY_WEBHOOK_SECRET")
        if not webhook_secret:
            return _json_error(
                "Razorpay webhook is not configured on this server.", 503
            )
        raw_body = request.get_data(cache=False)
        signature = request.headers.get("X-Razorpay-Signature", "")
        expected_signature = hmac.new(
            webhook_secret.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()
        if not signature or not hmac.compare_digest(expected_signature, signature):
            return _json_error("Invalid webhook signature.", 400)
        try:
            payload = json.loads(raw_body)
        except (UnicodeDecodeError, ValueError):
            return _json_error("Invalid webhook JSON.", 400)
        if not isinstance(payload, dict) or not isinstance(payload.get("payload", {}), dict):
            return _json_error("Invalid webhook payload shape.", 400)
        event_type = str(payload.get("event") or "unknown")
        provider_event_id = request.headers.get("X-Razorpay-Event-Id")
        if provider_event_id and db.session.scalar(
            select(BillingEvent.id).where(
                BillingEvent.provider_event_id == provider_event_id
            )
        ):
            return jsonify({"message": "Duplicate webhook ignored."})
        entity_data = payload.get("payload") or {}
        payment_entry = entity_data.get("payment") or {}
        subscription_entry = entity_data.get("subscription") or {}
        if not isinstance(payment_entry, dict) or not isinstance(subscription_entry, dict):
            return _json_error("Invalid webhook entity shape.", 400)
        payment_data = payment_entry.get("entity") or {}
        subscription_data = subscription_entry.get("entity") or {}
        if not isinstance(payment_data, dict) or not isinstance(subscription_data, dict):
            return _json_error("Invalid webhook entity shape.", 400)
        provider_subscription_id = (
            payment_data.get("subscription_id") or subscription_data.get("id")
        )
        gateway_subscription = None
        if provider_subscription_id:
            gateway_subscription = db.session.scalar(
                select(GatewaySubscription).where(
                    GatewaySubscription.provider_subscription_id
                    == provider_subscription_id
                )
            )
        seller_id = gateway_subscription.seller_id if gateway_subscription else None
        plan_id = gateway_subscription.plan_id if gateway_subscription else None
        payment_id = payment_data.get("id")

        if event_type in ("payment.captured", "subscription.charged") and payment_id:
            payment_data["status"] = "captured"
            outcome = _apply_captured_subscription_payment(
                provider_subscription_id,
                payment_data,
                "webhook",
                provider_event_id,
            )
            if outcome.get("activated"):
                _cancel_superseded_subscriptions(
                    outcome["seller_id"], provider_subscription_id
                )
        elif event_type == "payment.failed":
            if gateway_subscription and payment_id:
                transaction = db.session.scalar(
                    select(PaymentTransaction).where(
                        PaymentTransaction.provider_payment_id == payment_id
                    )
                )
                if transaction is None:
                    transaction = db.session.scalar(
                        select(PaymentTransaction)
                        .where(
                            PaymentTransaction.provider_subscription_id
                            == provider_subscription_id,
                            PaymentTransaction.provider_payment_id.is_(None),
                            PaymentTransaction.status.in_(("created", "authenticated")),
                        )
                        .order_by(PaymentTransaction.id.desc())
                    )
                if transaction is None:
                    transaction = PaymentTransaction(
                        seller_id=gateway_subscription.seller_id,
                        plan_id=gateway_subscription.plan_id,
                        provider_subscription_id=provider_subscription_id,
                        provider_payment_id=payment_id,
                        amount_paise=int(payment_data.get("amount") or 0),
                        currency=str(payment_data.get("currency") or "INR"),
                        status="failed",
                        failure_reason=str(
                            payment_data.get("error_description")
                            or payment_data.get("error_reason")
                            or "Payment failed"
                        )[:500],
                    )
                    db.session.add(transaction)
                else:
                    transaction.provider_payment_id = payment_id
                    transaction.amount_paise = int(payment_data.get("amount") or 0)
                    transaction.currency = str(payment_data.get("currency") or "INR")
                    transaction.status = "failed"
                    transaction.failure_reason = str(
                        payment_data.get("error_description")
                        or payment_data.get("error_reason")
                        or "Payment failed"
                    )[:500]
                    transaction.updated_at = utcnow()
        elif event_type.startswith("subscription.") and gateway_subscription:
            provider_status = str(
                subscription_data.get("status")
                or event_type.split(".", 1)[1]
            ).lower()
            gateway_subscription.status = provider_status[:24]
            gateway_subscription.updated_at = utcnow()
            if provider_status in ("halted", "cancelled", "completed", "expired"):
                local_subscription = db.session.scalar(
                    select(Subscription)
                    .where(
                        Subscription.seller_id == gateway_subscription.seller_id,
                        Subscription.status == "active",
                    )
                    .order_by(Subscription.id.desc())
                )
                another_active = db.session.scalar(
                    select(GatewaySubscription.id).where(
                        GatewaySubscription.seller_id
                        == gateway_subscription.seller_id,
                        GatewaySubscription.provider_subscription_id
                        != provider_subscription_id,
                        GatewaySubscription.status == "active",
                    )
                )
                if local_subscription and not another_active:
                    local_subscription.status = "past_due" if provider_status == "halted" else "cancelled"
                    db.session.query(Product).filter_by(
                        seller_id=gateway_subscription.seller_id
                    ).update({Product.promoted: False}, synchronize_session=False)

        captured_event_handled = (
            event_type in ("payment.captured", "subscription.charged")
            and bool(payment_id)
        )
        if not captured_event_handled:
            _billing_event(
                db.session,
                event_type,
                "webhook",
                "received",
                seller_id=seller_id,
                plan_id=plan_id,
                provider_subscription_id=provider_subscription_id,
                provider_payment_id=payment_id,
                provider_event_id=provider_event_id,
                details={
                    "provider_status": payment_data.get("status")
                    or subscription_data.get("status"),
                },
            )
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            if provider_event_id and db.session.scalar(
                select(BillingEvent.id).where(
                    BillingEvent.provider_event_id == provider_event_id
                )
            ):
                return jsonify({"message": "Duplicate webhook ignored."})
            raise
        return jsonify({"message": "Webhook processed."})

    @app.get("/api/billing/history")
    @_role_required("seller")
    def billing_history():
        payments = db.session.scalars(
            select(PaymentTransaction)
            .where(PaymentTransaction.seller_id == g.current_user.id)
            .order_by(PaymentTransaction.created_at.desc())
            .limit(100)
        ).all()
        events = db.session.scalars(
            select(BillingEvent)
            .where(BillingEvent.seller_id == g.current_user.id)
            .order_by(BillingEvent.created_at.desc())
            .limit(100)
        ).all()
        gateway_subscriptions = db.session.scalars(
            select(GatewaySubscription)
            .where(GatewaySubscription.seller_id == g.current_user.id)
            .order_by(GatewaySubscription.created_at.desc())
            .limit(50)
        ).all()
        return jsonify(
            {
                "payments": [
                    {
                        "id": payment.id,
                        "plan": payment.plan.name if payment.plan else "",
                        "provider_payment_id": payment.provider_payment_id,
                        "provider_subscription_id": payment.provider_subscription_id,
                        "amount": float(Decimal(payment.amount_paise) / Decimal("100")),
                        "currency": payment.currency,
                        "status": payment.status,
                        "failure_reason": payment.failure_reason,
                        "created_at": payment.created_at.isoformat(),
                    }
                    for payment in payments
                ],
                "subscriptions": [
                    {
                        "plan": gateway_subscription.plan.name
                        if gateway_subscription.plan
                        else "",
                        "provider_subscription_id": gateway_subscription.provider_subscription_id,
                        "status": gateway_subscription.status,
                        "created_at": gateway_subscription.created_at.isoformat(),
                        "updated_at": gateway_subscription.updated_at.isoformat(),
                    }
                    for gateway_subscription in gateway_subscriptions
                ],
                "events": [
                    {
                        "id": event.id,
                        "provider_event_id": event.provider_event_id,
                        "event_type": event.event_type,
                        "source": event.source,
                        "status": event.status,
                        "provider_subscription_id": event.provider_subscription_id,
                        "provider_payment_id": event.provider_payment_id,
                        "details": json.loads(event.details or "{}"),
                        "created_at": event.created_at.isoformat(),
                    }
                    for event in events
                ],
            }
        )

    @app.route("/api/products", methods=["GET", "POST"])
    @app.route("/api/products/<int:product_id>", methods=["PUT", "DELETE"])
    @_role_required("seller")
    def products(product_id=None):
        if request.method == "GET":
            rows = db.session.scalars(
                select(Product)
                .where(Product.seller_id == g.current_user.id)
                .order_by(Product.id)
            )
            return jsonify([_serialize_product(row) for row in rows])
        if request.method == "DELETE":
            product = db.session.scalar(
                select(Product).where(
                    Product.id == product_id, Product.seller_id == g.current_user.id
                )
            )
            if product is None:
                return _json_error("Product not found", 404)
            db.session.delete(product)
            db.session.commit()
            return jsonify({"message": "Product deleted"})

        data = request.get_json(silent=True) or {}
        name = str(data.get("name", "")).strip()
        image_url = str(data.get("image_url", "")).strip()
        try:
            price = float(data.get("price"))
        except (TypeError, ValueError):
            return _json_error("Price must be a positive number", 400)
        if not name or len(name) > 160 or not 0 < price <= 10000000:
            return _json_error("Product name and a valid positive price are required", 400)
        if request.method == "POST":
            product = Product(
                seller_id=g.current_user.id,
                name=name,
                price=price,
                image_url=image_url,
            )
            db.session.add(product)
        else:
            product = db.session.scalar(
                select(Product).where(
                    Product.id == product_id, Product.seller_id == g.current_user.id
                )
            )
            if product is None:
                return _json_error("Product not found", 404)
            product.name = name
            product.price = price
            product.image_url = image_url
        db.session.commit()
        return jsonify(_serialize_product(product)), 201 if request.method == "POST" else 200

    @app.patch("/api/products/<int:product_id>/promote")
    @_role_required("seller")
    def promote_product(product_id):
        product = db.session.scalar(
            select(Product).where(
                Product.id == product_id, Product.seller_id == g.current_user.id
            )
        )
        if product is None:
            return _json_error("Product not found", 404)
        data = request.get_json(silent=True) or {}
        if not isinstance(data.get("promoted"), bool):
            return _json_error("promoted must be true or false", 400)
        if data["promoted"]:
            subscription = db.session.scalar(
                select(Subscription)
                .where(
                    Subscription.seller_id == g.current_user.id,
                    Subscription.status == "active",
                )
                .order_by(Subscription.id.desc())
            )
            plan = db.session.get(Plan, subscription.plan_id) if subscription else None
            if plan is None:
                return _json_error("Choose an active plan before promoting products", 409)
            if not plan.allow_overage and subscription.tokens_used >= plan.token_quota:
                return _json_error("Your token quota is exhausted", 409)
        product.promoted = data["promoted"]
        db.session.commit()
        return jsonify(_serialize_product(product))

    @app.get("/api/analytics")
    @_role_required("seller")
    def analytics():
        subscription = db.session.scalar(
            select(Subscription)
            .where(
                Subscription.seller_id == g.current_user.id,
                Subscription.status == "active",
            )
            .order_by(Subscription.id.desc())
        )
        if subscription is None:
            return jsonify(
                {
                    "events_per_day": [],
                    "totals": {"reach": 0, "clicks": 0, "buys": 0},
                    "conversion_rate": 0,
                    "tokens_used": 0,
                    "token_quota": 0,
                    "remaining_tokens": 0,
                    "overage_tokens": 0,
                    "top_products": [],
                }
            )
        plan = db.session.get(Plan, subscription.plan_id)
        rows = db.session.execute(
            select(
                func.date(UsageEvent.created_at).label("event_date"),
                UsageEvent.event_type,
                func.count(UsageEvent.id).label("event_count"),
            )
            .where(
                UsageEvent.seller_id == g.current_user.id,
                UsageEvent.created_at >= subscription.cycle_start,
            )
            .group_by(func.date(UsageEvent.created_at), UsageEvent.event_type)
            .order_by(func.date(UsageEvent.created_at))
        ).all()
        daily = {}
        for row in rows:
            day = str(row.event_date)
            daily.setdefault(day, {"date": day, "reach": 0, "clicks": 0, "buys": 0})
            field = {"impression": "reach", "click": "clicks", "buy": "buys"}.get(
                row.event_type
            )
            if field:
                daily[day][field] = row.event_count
        totals = {
            "reach": sum(day["reach"] for day in daily.values()),
            "clicks": sum(day["clicks"] for day in daily.values()),
            "buys": sum(day["buys"] for day in daily.values()),
        }
        top_rows = db.session.execute(
            select(Product.id, Product.name, func.count(UsageEvent.id).label("events"))
            .join(UsageEvent, UsageEvent.product_id == Product.id)
            .where(
                UsageEvent.seller_id == g.current_user.id,
                UsageEvent.created_at >= subscription.cycle_start,
            )
            .group_by(Product.id, Product.name)
            .order_by(func.count(UsageEvent.id).desc())
            .limit(5)
        ).all()
        return jsonify(
            {
                "events_per_day": list(daily.values()),
                "totals": totals,
                "conversion_rate": round(
                    totals["buys"] / totals["reach"] * 100, 2
                )
                if totals["reach"]
                else 0,
                "tokens_used": subscription.tokens_used,
                "token_quota": plan.token_quota if plan else 0,
                "remaining_tokens": max(
                    0, (plan.token_quota if plan else 0) - subscription.tokens_used
                ),
                "overage_tokens": subscription.overage_tokens,
                "top_products": [
                    {"id": row.id, "name": row.name, "events": row.events}
                    for row in top_rows
                ],
            }
        )

    @app.get("/api/invoices")
    @_role_required("seller")
    def invoices():
        rows = db.session.scalars(
            select(Invoice)
            .where(Invoice.seller_id == g.current_user.id)
            .order_by(Invoice.created_at.desc())
        )
        return jsonify([_serialize_invoice(row) for row in rows])

    @app.get("/api/invoices/<int:invoice_id>/download")
    @_role_required("seller")
    def download_invoice(invoice_id):
        invoice = db.session.scalar(
            select(Invoice).where(
                Invoice.id == invoice_id, Invoice.seller_id == g.current_user.id
            )
        )
        if invoice is None:
            return _json_error("Invoice not found", 404)
        status = getattr(invoice, "status", None) or "pending"
        status_label = {
            "paid": "PAID",
            "sample": "SAMPLE — NO PAYMENT TAKEN",
            "pending": "PENDING — SAMPLE/UNPAID",
        }.get(status, status.upper())
        plan = db.session.get(Plan, invoice.plan_id) if getattr(invoice, "plan_id", None) else None
        if status == "paid":
            invoice_notice = "This invoice records a captured payment."
        elif status == "sample":
            invoice_notice = "This is a sample/demo document. No payment was taken and no bank or card details were collected."
        else:
            invoice_notice = "This invoice is pending and unpaid. No payment was taken."
        body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Morrow Invoice #{invoice.id}</title>
<style>body{{font:16px Arial,sans-serif;max-width:760px;margin:48px auto;color:#17211f}}header{{display:flex;justify-content:space-between;gap:20px;border-bottom:2px solid #17211f;padding-bottom:18px}}h1{{margin:0 0 4px}}.status{{border:2px solid #e7664d;color:#a43d2d;font-weight:800;padding:10px 12px;text-align:center}}table{{width:100%;border-collapse:collapse;margin-top:32px}}td{{padding:14px;border-bottom:1px solid #ddd}}td:last-child{{text-align:right}}.total{{font-weight:bold;font-size:1.2em}}button{{border:0;background:#17211f;color:white;padding:10px 14px;cursor:pointer}}.demo{{background:#fff1ee;border-left:4px solid #e7664d;padding:12px}}@media print{{button{{display:none}}body{{margin:20px}}}}</style></head>
<body><header><div><h1>Morrow</h1><strong>Invoice #{invoice.id}</strong></div><div class="status">{escape(status_label)}</div></header>
<p>{escape(g.current_user.name)} &middot; {escape(g.current_user.email)}</p>
<p><strong>Plan:</strong> {escape(plan.name if plan else "Not specified")}<br><strong>Billing period:</strong> {escape(invoice.period)}<br><strong>Issued:</strong> {escape(invoice.created_at.strftime('%Y-%m-%d'))}</p>
<div class="demo">{escape(invoice_notice)}</div>
<table><tr><td>Plan fee</td><td>INR {invoice.plan_fee:.2f}</td></tr>
<tr><td>Overage ({invoice.overage_tokens} tokens)</td><td>INR {invoice.overage_amount:.2f}</td></tr>
<tr class="total"><td>Total</td><td>INR {invoice.total:.2f}</td></tr></table>
<p><button onclick="window.print()">Print / Save as PDF</button></p></body></html>"""
        response = app.response_class(body, mimetype="text/html")
        response.headers["Content-Disposition"] = f'attachment; filename="invoice-{invoice.id}.html"'
        return response

    @app.post("/api/usage/record")
    def record_usage():
        api_key = request.headers.get("x-api-key", "")
        if not api_key:
            return _json_error("x-api-key header is required", 401)
        key_row = db.session.scalar(select(ApiKey).where(ApiKey.api_key == api_key))
        if key_row is None:
            return _json_error("Invalid API key", 401)
        data = request.get_json(silent=True) or {}
        try:
            product_id = int(data.get("product_id"))
        except (TypeError, ValueError):
            return _json_error("product_id must be an integer", 400)
        event_type = data.get("event_type")
        idempotency_key = data.get("idempotency_key")
        if not isinstance(event_type, str) or event_type not in EVENT_TOKENS:
            return _json_error("event_type must be impression, click, or buy", 400)
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 160:
            return _json_error("A unique idempotency_key of at most 160 characters is required", 400)
        try:
            result = _record_usage(
                key_row.seller_id, product_id, event_type, idempotency_key.strip()
            )
        except ValueError as exc:
            return _json_error(str(exc), 404)
        except IntegrityError:
            db.session.rollback()
            return jsonify({"message": "Duplicate event ignored", "duplicate": True})
        if result.get("blocked"):
            return _json_error("Token quota exceeded; promoted products have been paused", 429)
        return jsonify({"message": "Usage recorded", **result})

    @app.get("/api/marketplace/recommended")
    def recommended():
        rows = db.session.execute(
            select(Product, ApiKey.api_key)
            .join(Subscription, Subscription.seller_id == Product.seller_id)
            .join(Plan, Plan.id == Subscription.plan_id)
            .join(ApiKey, ApiKey.seller_id == Product.seller_id)
            .where(
                Product.promoted.is_(True),
                Subscription.status == "active",
                or_(
                    Plan.allow_overage.is_(True),
                    Subscription.tokens_used < Plan.token_quota,
                ),
            )
            .order_by(Product.id)
        ).all()
        products_out = []
        for product, api_key in rows:
            try:
                outcome = _record_usage(
                    product.seller_id,
                    product.id,
                    "impression",
                    "marketplace-" + secrets.token_urlsafe(24),
                )
            except ValueError:
                continue
            if outcome.get("blocked"):
                continue
            products_out.append(_serialize_product(product))
        return jsonify(products_out)

    @app.post("/api/marketplace/event")
    def marketplace_event():
        data = request.get_json(silent=True) or {}
        try:
            product_id = int(data.get("product_id"))
        except (TypeError, ValueError):
            return _json_error("product_id must be an integer", 400)
        event_type = data.get("event_type")
        idempotency_key = data.get("idempotency_key")
        if not isinstance(event_type, str) or event_type not in ("click", "buy"):
            return _json_error("event_type must be click or buy", 400)
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 160:
            return _json_error("A unique idempotency_key of at most 160 characters is required", 400)
        product = db.session.get(Product, product_id)
        if product is None or not product.promoted:
            return _json_error("Product is not currently available", 404)
        key_row = db.session.scalar(select(ApiKey).where(ApiKey.seller_id == product.seller_id))
        if key_row is None:
            return _json_error("Seller metering is not configured", 409)
        try:
            result = _record_usage(
                product.seller_id,
                product.id,
                event_type,
                idempotency_key.strip(),
            )
        except ValueError as exc:
            return _json_error(str(exc), 404)
        if result.get("blocked"):
            return _json_error("This seller has reached their usage limit", 429)
        return jsonify({"message": "Shopper action recorded", **result})

    @app.get("/api/usage/live")
    @_role_required("seller")
    def usage_live():
        billing_status = (
            db.session.scalar(
                select(GatewaySubscription.status)
                .where(
                    GatewaySubscription.seller_id == g.current_user.id,
                    GatewaySubscription.status.notin_(
                        ("cancelled", "completed", "expired")
                    ),
                )
                .order_by(GatewaySubscription.id.desc())
                .limit(1)
            )
            or "free"
        )
        active_subscription = db.session.scalar(
            select(Subscription)
            .where(
                Subscription.seller_id == g.current_user.id,
                Subscription.status == "active",
            )
            .order_by(Subscription.id.desc())
        )
        subscription = active_subscription or db.session.scalar(
            select(Subscription)
            .where(Subscription.seller_id == g.current_user.id)
            .order_by(Subscription.id.desc())
        )
        if subscription is None:
            key = db.session.scalar(
                select(ApiKey).where(ApiKey.seller_id == g.current_user.id)
            )
            return jsonify(
                {
                    "subscription": None,
                    "api_key": key.api_key if key else None,
                    "billing_status": billing_status,
                }
            )
        if subscription.status != "active":
            billing_status = subscription.status
        plan = db.session.get(Plan, subscription.plan_id)
        if (
            billing_status == "free"
            and subscription.status == "active"
            and plan is not None
            and plan.price > 0
        ):
            billing_status = "active"
        key = db.session.scalar(
            select(ApiKey).where(ApiKey.seller_id == g.current_user.id)
        )
        return jsonify(
            {
                "subscription": {
                    "tokens_used": subscription.tokens_used,
                    "overage_tokens": subscription.overage_tokens,
                    "token_quota": plan.token_quota if plan else 0,
                    "plan": _serialize_plan(plan) if plan else None,
                    "cycle_start": subscription.cycle_start.isoformat(),
                    "status": subscription.status,
                },
                "api_key": key.api_key if key else None,
                "billing_status": billing_status,
            }
        )

    @app.get("/api/admin/summary")
    @_role_required("admin")
    def admin_summary():
        revenue = db.session.scalar(
            select(func.coalesce(func.sum(Invoice.total), 0)).where(Invoice.status == "paid")
        ) or 0
        rows = db.session.execute(
            select(User, Subscription, Plan)
            .join(Subscription, Subscription.seller_id == User.id)
            .join(Plan, Plan.id == Subscription.plan_id)
            .where(User.role == "seller", Subscription.status == "active")
            .order_by(User.name)
        ).all()
        return jsonify(
            {
                "total_revenue": round(float(revenue), 2),
                "usage_per_seller": [
                    {
                        "seller_id": user.id,
                        "name": user.name,
                        "email": user.email,
                        "plan": plan.name,
                        "tokens_used": sub.tokens_used,
                        "token_quota": plan.token_quota,
                        "overage_tokens": sub.overage_tokens,
                    }
                    for user, sub, plan in rows
                ],
            }
        )

    @app.post("/api/admin/close-cycle")
    @_role_required("admin")
    def close_cycle():
        now = utcnow()
        with Session(db.engine) as session:
            with session.begin():
                subscriptions = session.scalars(
                    select(Subscription)
                    .where(Subscription.status == "active")
                    .with_for_update()
                ).all()
                created = []
                for sub in subscriptions:
                    plan = session.get(Plan, sub.plan_id)
                    if plan is None:
                        continue
                    period = f"{sub.cycle_start:%Y-%m-%d} to {now:%Y-%m-%d}"
                    overage_amount = round(sub.overage_tokens * plan.overage_rate, 2)
                    invoice = Invoice(
                        seller_id=sub.seller_id,
                        plan_id=sub.plan_id,
                        plan_fee=0,
                        overage_tokens=sub.overage_tokens,
                        overage_amount=overage_amount,
                        total=overage_amount,
                        period=period,
                        status="pending",
                        invoice_type="usage",
                        created_at=now,
                    )
                    session.add(invoice)
                    created.append(invoice)
                    sub.tokens_used = 0
                    sub.overage_tokens = 0
                    sub.cycle_start = now
                session.flush()
                count = len(created)
        return jsonify({"message": "Billing cycle closed", "invoices_created": count})

    @app.errorhandler(404)
    def not_found(error):
        if request.path.startswith("/api/"):
            return _json_error("Resource not found", 404)
        return error

    return app


def _serialize_invoice(invoice):
    return {
        "id": invoice.id,
        "plan_id": getattr(invoice, "plan_id", None),
        "plan_fee": invoice.plan_fee,
        "overage_tokens": invoice.overage_tokens,
        "overage_amount": invoice.overage_amount,
        "total": invoice.total,
        "currency": "INR",
        "period": invoice.period,
        "status": getattr(invoice, "status", None) or "pending",
        "invoice_type": getattr(invoice, "invoice_type", None) or "usage",
        "created_at": invoice.created_at.isoformat(),
    }


def init_db(app):
    with app.app_context():
        db.create_all()
        # Additive migration: keeps existing databases working when the app is upgraded.
        invoice_columns = {
            column["name"] for column in inspect(db.engine).get_columns("invoice")
        }
        if "plan_id" not in invoice_columns:
            db.session.execute(db.text(
                "ALTER TABLE invoice ADD COLUMN plan_id INTEGER NULL"
            ))
        if "status" not in invoice_columns:
            db.session.execute(db.text(
                "ALTER TABLE invoice ADD COLUMN status VARCHAR(24) NOT NULL DEFAULT 'pending'"
            ))
        if "invoice_type" not in invoice_columns:
            db.session.execute(db.text(
                "ALTER TABLE invoice ADD COLUMN invoice_type VARCHAR(24) NOT NULL DEFAULT 'usage'"
            ))
        if "activation_key" not in invoice_columns:
            db.session.execute(db.text(
                "ALTER TABLE invoice ADD COLUMN activation_key VARCHAR(160) NULL"
            ))
        db.session.execute(db.text(
            "UPDATE invoice SET invoice_type='subscription', status='paid' "
            "WHERE plan_fee > 0 AND invoice_type='usage'"
        ))
        db.session.execute(db.text(
            "UPDATE invoice SET invoice_type='usage', status='pending' "
            "WHERE plan_fee = 0"
        ))
        db.session.flush()
        plan_seeds = [
            ("Free", 0, 1000, 0, False),
            ("Pro", 999, 50000, 0.05, True),
            ("Business", 4999, 500000, 0.02, True),
        ]
        for name, price, quota, overage_rate, allow_overage in plan_seeds:
            if db.session.scalar(select(Plan.id).where(Plan.name == name)) is None:
                db.session.add(
                    Plan(
                        name=name,
                        price=price,
                        token_quota=quota,
                        overage_rate=overage_rate,
                        allow_overage=allow_overage,
                    )
                )
        db.session.flush()
        free = db.session.scalar(select(Plan).where(Plan.name == "Free"))
        accounts = [
            ("Demo Admin", "admin@demo.com", "admin123", "admin"),
            ("Harbor & Pine", "seller1@demo.com", "seller123", "seller"),
            ("Sunday Studio", "seller2@demo.com", "seller123", "seller"),
        ]
        users = []
        for name, email, password, role in accounts:
            user = db.session.scalar(select(User).where(User.email == email))
            if user is None:
                user = User(
                    name=name,
                    email=email,
                    password_hash=generate_password_hash(password),
                    role=role,
                )
                db.session.add(user)
                db.session.flush()
            users.append(user)
        for seller in users[1:]:
            if not db.session.scalar(
                select(Subscription.id).where(Subscription.seller_id == seller.id)
            ):
                db.session.add(
                    Subscription(seller_id=seller.id, plan_id=free.id)
                )
            _ensure_api_key(db.session, seller.id)
        db.session.flush()
        for seller_index, seller in enumerate(users[1:]):
            has_products = db.session.scalar(
                select(Product.id).where(Product.seller_id == seller.id).limit(1)
            )
            if has_products is not None:
                continue
            for product_index, (name, price, photo_id) in enumerate(
                PRODUCTS[seller_index * 4 : seller_index * 4 + 4]
            ):
                db.session.add(
                    Product(
                        seller_id=seller.id,
                        name=name,
                        price=price,
                        image_url=(
                            f"https://images.unsplash.com/{photo_id}"
                            "?auto=format&fit=crop&w=900&q=85"
                        ),
                        promoted=product_index < 2,
                    )
                )
        legacy_paid_subscriptions = db.session.execute(
            select(Subscription, Plan)
            .join(Plan, Plan.id == Subscription.plan_id)
            .where(Subscription.status == "active", Plan.price > 0)
        ).all()
        for subscription, paid_plan in legacy_paid_subscriptions:
            paid_transaction = db.session.scalar(
                select(PaymentTransaction.id)
                .where(
                    PaymentTransaction.seller_id == subscription.seller_id,
                    PaymentTransaction.plan_id == paid_plan.id,
                    PaymentTransaction.status == "captured",
                )
                .limit(1)
            )
            if paid_transaction is not None:
                continue
            subscription.status = "payment_required"
            _billing_event(
                db.session,
                "subscription.legacy_payment_required",
                "migration",
                "payment_required",
                seller_id=subscription.seller_id,
                plan_id=paid_plan.id,
                details={
                    "reason": "No captured Razorpay payment was on record; "
                    "paid usage is paused until checkout confirms payment."
                },
            )
        db.session.commit()


app = create_app()


if __name__ == "__main__":
    init_db(app)
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=os.environ.get("FLASK_ENV") == "development")
