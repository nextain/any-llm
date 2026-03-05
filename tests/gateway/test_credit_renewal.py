"""Tests for credit subscription renewal logic."""

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


def _seed_user_with_subscription(
    client: TestClient,
    master_key_header: dict[str, str],
    db: Session,
    *,
    user_id: str = "renewal-user",
    plan_name: str = "BASIC",
    renew_at: datetime | None = None,
    sub_status: str = "ACTIVE",
    existing_balance: float = 0.0,
) -> tuple[str, str]:
    """Helper: create gateway user + caret user + subscription, return (user_id, subscription_id)."""
    client.post("/v1/users", json={"user_id": user_id}, headers=master_key_header)

    plan = db.query(BillingPlan).filter(BillingPlan.name == plan_name, BillingPlan.active.is_(True)).first()
    assert plan is not None, f"Plan {plan_name} not found"

    caret_user = db.query(CaretUser).filter(CaretUser.user_id == user_id).first()
    if not caret_user:
        caret_user = CaretUser(user_id=user_id, provider="test", role="user")
        db.add(caret_user)
        db.flush()

    now = datetime.now(UTC)
    if renew_at is None:
        renew_at = now - timedelta(hours=1)

    sub = BillingSubscription(
        user_id=user_id,
        plan_id=plan.id,
        status=sub_status,
        start_at=now - timedelta(days=30),
        renew_at=renew_at,
    )
    db.add(sub)

    # Seed existing balance if requested
    if existing_balance > 0:
        bal = CreditBalance(
            user_id=user_id,
            pool_type="BONUS",
            amount=existing_balance,
            expires_at=None,
            priority=1,
        )
        db.add(bal)

    db.commit()
    return user_id, sub.id


def test_renew_paid_plan_creates_full_credits(
    client: TestClient, master_key_header: dict[str, str], test_db: Session,
) -> None:
    """BASIC (paid) plan: always add full monthly_credits regardless of balance."""
    from any_llm.gateway.billing.renewal import renew_subscriptions

    user_id, _ = _seed_user_with_subscription(
        client, master_key_header, test_db,
        user_id="renew-paid", plan_name="BASIC",
    )

    count = renew_subscriptions(test_db)
    assert count == 1

    bal = (
        test_db.query(CreditBalance)
        .filter(CreditBalance.user_id == user_id, CreditBalance.pool_type == "SUBSCRIPTION")
        .first()
    )
    assert bal is not None
    plan = test_db.query(BillingPlan).filter(BillingPlan.name == "BASIC").first()
    assert bal.amount == plan.monthly_credits  # Full 100 credits

    topup = (
        test_db.query(CreditTopup)
        .filter(CreditTopup.user_id == user_id, CreditTopup.source == "SUBSCRIPTION_RENEWAL")
        .first()
    )
    assert topup is not None
    assert topup.amount == plan.monthly_credits


def test_renew_free_plan_topup_when_low(
    client: TestClient, master_key_header: dict[str, str], test_db: Session,
) -> None:
    """FREE plan: top up only the difference when balance < monthly_credits."""
    from any_llm.gateway.billing.renewal import renew_subscriptions

    user_id, _ = _seed_user_with_subscription(
        client, master_key_header, test_db,
        user_id="free-low", plan_name="FREE", existing_balance=3.0,
    )

    count = renew_subscriptions(test_db)
    assert count == 1

    # Should top up 7.0 (10.0 - 3.0), not 10.0
    bal = (
        test_db.query(CreditBalance)
        .filter(CreditBalance.user_id == user_id, CreditBalance.pool_type == "SUBSCRIPTION")
        .first()
    )
    assert bal is not None
    assert bal.amount == 7.0  # 10 - 3 = 7


def test_renew_free_plan_skip_when_sufficient(
    client: TestClient, master_key_header: dict[str, str], test_db: Session,
) -> None:
    """FREE plan: skip credit allocation when balance >= monthly_credits."""
    from any_llm.gateway.billing.renewal import renew_subscriptions

    user_id, sub_id = _seed_user_with_subscription(
        client, master_key_header, test_db,
        user_id="free-sufficient", plan_name="FREE", existing_balance=15.0,
    )

    count = renew_subscriptions(test_db)
    assert count == 1  # renew_at still advances

    # No SUBSCRIPTION pool created
    sub_bal = (
        test_db.query(CreditBalance)
        .filter(CreditBalance.user_id == user_id, CreditBalance.pool_type == "SUBSCRIPTION")
        .first()
    )
    assert sub_bal is None


def test_not_due_subscription_skipped(
    client: TestClient, master_key_header: dict[str, str], test_db: Session,
) -> None:
    """Subscription not yet due should NOT be renewed."""
    from any_llm.gateway.billing.renewal import renew_subscriptions

    future = datetime.now(UTC) + timedelta(days=15)
    _seed_user_with_subscription(
        client, master_key_header, test_db, user_id="not-due", renew_at=future,
    )

    count = renew_subscriptions(test_db)
    assert count == 0


def test_cancelled_subscription_skipped(
    client: TestClient, master_key_header: dict[str, str], test_db: Session,
) -> None:
    """Cancelled subscription should NOT be renewed."""
    from any_llm.gateway.billing.renewal import renew_subscriptions

    _seed_user_with_subscription(
        client, master_key_header, test_db, user_id="cancelled-user", sub_status="CANCELLED",
    )

    count = renew_subscriptions(test_db)
    assert count == 0


def test_renew_at_updated_after_renewal(
    client: TestClient, master_key_header: dict[str, str], test_db: Session,
) -> None:
    """After renewal, renew_at should advance by renew_interval_days."""
    from any_llm.gateway.billing.renewal import renew_subscriptions

    past = datetime.now(UTC) - timedelta(hours=2)
    user_id, sub_id = _seed_user_with_subscription(
        client, master_key_header, test_db, user_id="renew-advance", renew_at=past,
    )

    renew_subscriptions(test_db)

    sub = test_db.query(BillingSubscription).filter(BillingSubscription.id == sub_id).first()
    plan = test_db.query(BillingPlan).filter(BillingPlan.id == sub.plan_id).first()
    expected = past + timedelta(days=int(plan.renew_interval_days))
    assert abs((sub.renew_at - expected).total_seconds()) < 1


def test_admin_trigger_renewal(
    client: TestClient, master_key_header: dict[str, str], test_db: Session,
) -> None:
    """POST /v1/admin/renewals/trigger should return renewed count."""
    _seed_user_with_subscription(
        client, master_key_header, test_db, user_id="admin-trigger",
    )

    response = client.post("/v1/admin/renewals/trigger", headers=master_key_header)
    assert response.status_code == 200
    data = response.json()
    assert data["renewed"] >= 1
