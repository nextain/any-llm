"""Billing management routes (admin + webhook integration)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from any_llm.gateway.auth import verify_jwt_or_api_key_or_master
from any_llm.gateway.billing.renewal import renew_subscriptions
from any_llm.gateway.db import (
    BillingPlan,
    BillingSubscription,
    CaretUser,
    CreditBalance,
    CreditTopup,
    get_db,
)
from any_llm.gateway.log_config import logger

router = APIRouter(prefix="/v1", tags=["billing"])


# ── Response models ──


class RenewalResponse(BaseModel):
    renewed: int


class SubscriptionRequest(BaseModel):
    user_id: str
    plan_name: str


class SubscriptionPatchRequest(BaseModel):
    status: str  # "ACTIVE", "CANCELLED", etc.


class TopupRequest(BaseModel):
    user_id: str
    amount: float
    source: str = "MANUAL"


class BillingResponse(BaseModel):
    success: bool
    detail: str | None = None


# ── Admin: manual renewal trigger ──


@router.post("/admin/renewals/trigger", response_model=RenewalResponse)
async def trigger_renewal(
    auth_result: Annotated[tuple, Depends(verify_jwt_or_api_key_or_master)],
    db: Annotated[Session, Depends(get_db)],
) -> RenewalResponse:
    """Manually trigger subscription renewal sweep (master key only)."""
    _, is_master, _, _ = auth_result
    if not is_master:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Master key required")

    count = renew_subscriptions(db)
    return RenewalResponse(renewed=count)


# ── Subscription management (called by webhook handler) ──


@router.post("/billing/subscription", response_model=BillingResponse)
async def create_or_upgrade_subscription(
    body: SubscriptionRequest,
    auth_result: Annotated[tuple, Depends(verify_jwt_or_api_key_or_master)],
    db: Annotated[Session, Depends(get_db)],
) -> BillingResponse:
    """Create or upgrade a user subscription (master key only)."""
    _, is_master, _, _ = auth_result
    if not is_master:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Master key required")

    plan = (
        db.query(BillingPlan)
        .filter(BillingPlan.name == body.plan_name, BillingPlan.active.is_(True))
        .first()
    )
    if not plan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Plan '{body.plan_name}' not found")

    caret_user = db.query(CaretUser).filter(CaretUser.user_id == body.user_id).first()
    if not caret_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # Cancel existing active subscriptions
    existing = (
        db.query(BillingSubscription)
        .filter(BillingSubscription.user_id == body.user_id, BillingSubscription.status == "ACTIVE")
        .all()
    )
    for sub in existing:
        sub.status = "SUPERSEDED"

    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    new_sub = BillingSubscription(
        user_id=body.user_id,
        plan_id=plan.id,
        status="ACTIVE",
        start_at=now,
        renew_at=now + timedelta(days=int(plan.renew_interval_days)),
    )
    db.add(new_sub)

    # Immediate credit allocation
    if plan.monthly_credits > 0:
        balance = CreditBalance(
            user_id=body.user_id,
            pool_type="SUBSCRIPTION",
            source_id=plan.id,
            amount=float(plan.monthly_credits),
            expires_at=now + timedelta(days=int(plan.renew_interval_days)),
            priority=2,
        )
        db.add(balance)

        topup = CreditTopup(
            user_id=body.user_id,
            pool_type="SUBSCRIPTION",
            amount=float(plan.monthly_credits),
            amount_usd=0.0,
            credits_per_usd=float(plan.credits_per_usd),
            expires_at=now + timedelta(days=int(plan.renew_interval_days)),
            source="SUBSCRIPTION_CREATED",
            metadata_={"plan_name": plan.name},
        )
        db.add(topup)

    db.commit()
    logger.info("Subscription created for user %s on plan %s", body.user_id, plan.name)
    return BillingResponse(success=True, detail=f"Subscribed to {plan.name}")


@router.patch("/billing/subscription/{user_id}", response_model=BillingResponse)
async def update_subscription_status(
    user_id: str,
    body: SubscriptionPatchRequest,
    auth_result: Annotated[tuple, Depends(verify_jwt_or_api_key_or_master)],
    db: Annotated[Session, Depends(get_db)],
) -> BillingResponse:
    """Update subscription status (e.g. cancel). Master key only."""
    _, is_master, _, _ = auth_result
    if not is_master:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Master key required")

    sub = (
        db.query(BillingSubscription)
        .filter(BillingSubscription.user_id == user_id, BillingSubscription.status == "ACTIVE")
        .first()
    )
    if not sub:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Active subscription not found")

    from datetime import UTC, datetime

    sub.status = body.status
    if body.status == "CANCELLED":
        sub.ends_at = datetime.now(UTC)

    db.commit()
    logger.info("Subscription %s for user %s updated to %s", sub.id, user_id, body.status)
    return BillingResponse(success=True, detail=f"Status updated to {body.status}")


@router.post("/billing/topup", response_model=BillingResponse)
async def manual_topup(
    body: TopupRequest,
    auth_result: Annotated[tuple, Depends(verify_jwt_or_api_key_or_master)],
    db: Annotated[Session, Depends(get_db)],
) -> BillingResponse:
    """Manually add credits to a user (master key only)."""
    _, is_master, _, _ = auth_result
    if not is_master:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Master key required")

    caret_user = db.query(CaretUser).filter(CaretUser.user_id == body.user_id).first()
    if not caret_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    balance = CreditBalance(
        user_id=body.user_id,
        pool_type="ADDON",
        amount=body.amount,
        expires_at=None,
        priority=3,
    )
    db.add(balance)

    topup = CreditTopup(
        user_id=body.user_id,
        pool_type="ADDON",
        amount=body.amount,
        amount_usd=None,
        credits_per_usd=None,
        expires_at=None,
        source=body.source,
        metadata_={"manual": True},
    )
    db.add(topup)

    db.commit()
    logger.info("Manual topup %.1f credits for user %s", body.amount, body.user_id)
    return BillingResponse(success=True, detail=f"Added {body.amount} credits")
