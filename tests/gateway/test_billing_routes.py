"""Tests for billing API routes (subscription management + topup)."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from any_llm.gateway.db import (
    BillingPlan,
    BillingSubscription,
    CaretUser,
    CreditBalance,
    CreditTopup,
)


def _ensure_caret_user(db: Session, user_id: str) -> None:
    """Create a CaretUser if not exists."""
    if not db.query(CaretUser).filter(CaretUser.user_id == user_id).first():
        db.add(CaretUser(user_id=user_id, provider="test", role="user"))
        db.commit()


def test_create_subscription(
    client: TestClient, master_key_header: dict[str, str], test_db: Session,
) -> None:
    """POST /v1/billing/subscription creates subscription + credits."""
    client.post("/v1/users", json={"user_id": "billing-user-1"}, headers=master_key_header)
    _ensure_caret_user(test_db, "billing-user-1")

    response = client.post(
        "/v1/billing/subscription",
        json={"user_id": "billing-user-1", "plan_name": "BASIC"},
        headers=master_key_header,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True

    # CreditBalance should exist
    bal = (
        test_db.query(CreditBalance)
        .filter(CreditBalance.user_id == "billing-user-1", CreditBalance.pool_type == "SUBSCRIPTION")
        .first()
    )
    assert bal is not None
    assert bal.amount == 100.0  # BASIC plan monthly_credits


def test_cancel_subscription(
    client: TestClient, master_key_header: dict[str, str], test_db: Session,
) -> None:
    """PATCH /v1/billing/subscription/{user_id} cancels subscription."""
    client.post("/v1/users", json={"user_id": "billing-cancel"}, headers=master_key_header)
    _ensure_caret_user(test_db, "billing-cancel")

    # Create subscription first
    client.post(
        "/v1/billing/subscription",
        json={"user_id": "billing-cancel", "plan_name": "BASIC"},
        headers=master_key_header,
    )

    # Cancel it
    response = client.patch(
        "/v1/billing/subscription/billing-cancel",
        json={"status": "CANCELLED"},
        headers=master_key_header,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True

    sub = (
        test_db.query(BillingSubscription)
        .filter(
            BillingSubscription.user_id == "billing-cancel",
            BillingSubscription.status == "CANCELLED",
        )
        .first()
    )
    assert sub is not None
    assert sub.ends_at is not None


def test_manual_topup(
    client: TestClient, master_key_header: dict[str, str], test_db: Session,
) -> None:
    """POST /v1/billing/topup adds credits."""
    client.post("/v1/users", json={"user_id": "billing-topup"}, headers=master_key_header)
    _ensure_caret_user(test_db, "billing-topup")

    response = client.post(
        "/v1/billing/topup",
        json={"user_id": "billing-topup", "amount": 50.0, "source": "PROMO"},
        headers=master_key_header,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True

    bal = (
        test_db.query(CreditBalance)
        .filter(CreditBalance.user_id == "billing-topup", CreditBalance.pool_type == "ADDON")
        .first()
    )
    assert bal is not None
    assert bal.amount == 50.0


def test_billing_requires_master_key(
    client: TestClient, master_key_header: dict[str, str],
) -> None:
    """Billing endpoints should reject non-master key requests."""
    # No auth header at all → should fail
    response = client.post(
        "/v1/billing/subscription",
        json={"user_id": "x", "plan_name": "BASIC"},
    )
    assert response.status_code in (401, 403)

    response = client.post("/v1/admin/renewals/trigger")
    assert response.status_code in (401, 403)
