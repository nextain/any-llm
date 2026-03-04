"""Billing plan constants — single source of truth for credit values.

Keep in sync with pricing page (naia.nextain.io/pricing).
"""

# ── FREE plan ────────────────────────────────────────────────
FREE_SIGNUP_BONUS_CREDITS = 500.0    # One-time bonus on first join (BONUS pool, no expiry)
FREE_MONTHLY_CREDITS = 300.0         # Monthly top-up if balance < this value
FREE_RENEW_INTERVAL_DAYS = 30
FREE_CREDITS_PER_USD = 1000.0

# ── BASIC plan ($10/mo) ─────────────────────────────────────
BASIC_MONTHLY_CREDITS = 10000.0
BASIC_ADD_AMOUNT_USD = 10.0
BASIC_PRICE_USD = 10.0
BASIC_RENEW_INTERVAL_DAYS = 30
BASIC_CREDITS_PER_USD = 1000.0

# ── Seed data for BillingPlan table ─────────────────────────
PLAN_SEEDS = [
    {
        "name": "FREE",
        "monthly_credits": FREE_MONTHLY_CREDITS,
        "monthly_bonus_credits": FREE_SIGNUP_BONUS_CREDITS,
        "add_amount_usd": 0.0,
        "add_bonus_percent": 0.0,
        "price_usd": 0.0,
        "currency": "USD",
        "credits_per_usd": FREE_CREDITS_PER_USD,
        "renew_interval_days": FREE_RENEW_INTERVAL_DAYS,
    },
    {
        "name": "BASIC",
        "monthly_credits": BASIC_MONTHLY_CREDITS,
        "monthly_bonus_credits": 0.0,
        "add_amount_usd": BASIC_ADD_AMOUNT_USD,
        "add_bonus_percent": 0.0,
        "price_usd": BASIC_PRICE_USD,
        "currency": "USD",
        "credits_per_usd": BASIC_CREDITS_PER_USD,
        "renew_interval_days": BASIC_RENEW_INTERVAL_DAYS,
    },
]
