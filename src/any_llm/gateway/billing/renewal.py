"""Monthly credit renewal for active subscriptions."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from any_llm.gateway.db import (
    BillingPlan,
    BillingSubscription,
    CreditBalance,
    CreditTopup,
)
from any_llm.gateway.log_config import logger


def _get_available_credits(db: Session, user_id: str) -> float:
    """Sum all non-expired credit balances for a user."""
    return float(
        db.query(func.coalesce(func.sum(CreditBalance.amount), 0.0))
        .filter(
            CreditBalance.user_id == user_id,
            (CreditBalance.expires_at.is_(None)) | (CreditBalance.expires_at > func.now()),
        )
        .scalar() or 0.0
    )


def renew_subscriptions(db: Session) -> int:
    """Renew all ACTIVE subscriptions whose renew_at <= now.

    Renewal behavior depends on plan type:
    - FREE plans (price_usd == 0): top-up only if balance < monthly_credits
      (e.g. balance < 300 → fill to 300, not add 300)
    - Paid plans: unconditionally add monthly_credits

    For each due subscription:
    1. Look up the BillingPlan for monthly_credits
    2. For free plans, check current balance and top-up the difference
    3. Create a CreditBalance (pool_type=SUBSCRIPTION, expires_at=renew_at+30d, priority=2)
    4. Create a CreditTopup (source=SUBSCRIPTION_RENEWAL)
    5. Advance renew_at by renew_interval_days

    Returns:
        Number of subscriptions renewed.
    """
    now = datetime.now(UTC)

    due_subs = (
        db.query(BillingSubscription)
        .filter(
            BillingSubscription.renew_at <= now,
            BillingSubscription.status == "ACTIVE",
        )
        .all()
    )

    renewed = 0
    for sub in due_subs:
        plan = db.query(BillingPlan).filter(BillingPlan.id == sub.plan_id).first()
        if not plan:
            logger.warning("Subscription %s references missing plan %s", sub.id, sub.plan_id)
            continue

        if plan.monthly_credits <= 0:
            sub.renew_at = sub.renew_at + timedelta(days=int(plan.renew_interval_days))
            renewed += 1
            continue

        # Determine credit amount to add
        is_free = plan.price_usd == 0.0
        if is_free:
            current_balance = _get_available_credits(db, sub.user_id)
            if current_balance >= plan.monthly_credits:
                # Balance already sufficient — skip credit allocation, just advance
                sub.renew_at = sub.renew_at + timedelta(days=int(plan.renew_interval_days))
                renewed += 1
                logger.info(
                    "Skipped credit top-up for user %s (balance %.1f >= %.1f)",
                    sub.user_id, current_balance, plan.monthly_credits,
                )
                continue
            # Top up to monthly_credits, not add monthly_credits
            topup_amount = float(plan.monthly_credits) - current_balance
        else:
            topup_amount = float(plan.monthly_credits)

        expires_at = sub.renew_at + timedelta(days=int(plan.renew_interval_days))

        balance = CreditBalance(
            user_id=sub.user_id,
            pool_type="SUBSCRIPTION",
            source_id=plan.id,
            amount=topup_amount,
            expires_at=expires_at,
            priority=2,
        )
        db.add(balance)

        topup = CreditTopup(
            user_id=sub.user_id,
            pool_type="SUBSCRIPTION",
            amount=topup_amount,
            amount_usd=0.0,
            credits_per_usd=float(plan.credits_per_usd),
            expires_at=expires_at,
            source="SUBSCRIPTION_RENEWAL",
            metadata_={"plan_name": plan.name, "subscription_id": sub.id},
        )
        db.add(topup)

        sub.renew_at = sub.renew_at + timedelta(days=int(plan.renew_interval_days))
        renewed += 1

        logger.info(
            "Renewed subscription %s for user %s: +%.1f credits",
            sub.id, sub.user_id, topup_amount,
        )

    if renewed > 0:
        db.commit()

    logger.info("Renewal sweep complete: %d subscriptions renewed", renewed)
    return renewed
