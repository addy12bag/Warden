"""
Stage 2 -- Deterministic root-cause classifier.

Maps a raw failed-payment record (error_code + context) to:
    - root_cause (human-readable category)
    - is_retryable (bool)
    - recommended_primary_action (one of the bounded action set)

This module is intentionally rule-based, NOT ML/LLM-driven. Every
mapping must be explainable and auditable -- this is what lets the
agent safely refuse to retry hard declines regardless of what any
downstream LLM layer might otherwise suggest.

Reference table (see PROJECT.md section 7):

    error_code            category            retryable   primary_action
    -------------------- ------------------- ----------- --------------------
    gateway_timeout       transient           yes         retry_now
    network_drop          transient           yes         retry_now
    insufficient_funds    soft_decline        yes         retry_delayed
    do_not_honor          soft_decline        yes*        retry_delayed
    velocity_limit        soft_decline        yes         retry_delayed
    invalid_cvv           user_error          limited     prompt_method_switch
    expired_card          hard_decline        no          prompt_method_switch
    card_stolen_lost      hard_decline        no          escalate
    account_closed        hard_decline        no          no_action
    restricted_card       hard_decline        no          escalate
    risk_block            compliance_block    no          escalate

    * do_not_honor becomes non-retryable after repeated failures --
      see STOPPING_RULE_MAX_SOFT_RETRIES.

Edge-case override rules (applied AFTER the static table lookup, in order):

    1. GLOBAL_MAX_RETRIES: if retry_count >= 3, force is_retryable=False
       and action="escalate" -- regardless of error_category. No cause
       justifies a 4th automated attempt; the batch-level stopping rule
       in Stage 3 duplicates this as a second independent gate, but the
       classifier itself must never claim something is retryable past
       the cap.

    2. SOFT_DECLINE_ESCALATION_THRESHOLD: if error_category is
       "soft_decline" or "transient" and retry_count >= 2, downgrade the
       action from retry_now/retry_delayed to "escalate". Two prior
       failures on a cause that is normally easy to recover suggests an
       unmodeled problem (e.g. a persistently empty account, a
       different underlying issue) -- blindly trying a third time is
       exactly the "blind retry" failure mode this project exists to
       avoid.

    3. USER_ERROR_SINGLE_PROMPT: if error_category is "user_error"
       (invalid_cvv) and retry_count >= 1, escalate instead of prompting
       again. A customer who mistyped a CVV once and failed again after
       being prompted is unlikely to succeed a third time without help.

    4. HARD_DECLINE_FLOOR: hard_decline and compliance_block are NEVER
       retryable, under any retry_count or amount. This rule cannot be
       overridden by any other rule -- it is the safety floor referenced
       throughout PROJECT.md.

    5. HIGH_VALUE_FLAG (audit-only, does not change action): if
       error_category is "soft_decline" and amount >= HIGH_VALUE_THRESHOLD,
       the reasoning string notes this as a priority case. This does not
       change is_retryable or the action -- it exists purely so the
       audit trail and dashboard can surface high-value cases for human
       attention without changing agent behavior.

TODO (Stage 2 -- DONE):
    - Implement classify_payment(payment: dict) -> ClassificationResult
    - Implement batch classification helper
    - Unit tests for each error_code, including retry_count edge cases
"""

from dataclasses import dataclass

GLOBAL_MAX_RETRIES = 3
SOFT_DECLINE_ESCALATION_THRESHOLD = 2
HIGH_VALUE_THRESHOLD = 10_000.0

RETRYABLE_ACTIONS = {"retry_now", "retry_delayed"}


@dataclass
class ClassificationResult:
    payment_id: str
    root_cause: str
    error_category: str
    is_retryable: bool
    recommended_primary_action: str
    reasoning: str


# Root-cause reference table -- single source of truth, matches PROJECT.md
ROOT_CAUSE_TABLE = {
    "gateway_timeout":    ("transient",         True,  "retry_now"),
    "network_drop":       ("transient",         True,  "retry_now"),
    "insufficient_funds": ("soft_decline",      True,  "retry_delayed"),
    "do_not_honor":       ("soft_decline",      True,  "retry_delayed"),
    "velocity_limit":     ("soft_decline",      True,  "retry_delayed"),
    "invalid_cvv":        ("user_error",        True,  "prompt_method_switch"),
    "expired_card":       ("hard_decline",      False, "prompt_method_switch"),
    "card_stolen_lost":   ("hard_decline",      False, "escalate"),
    "account_closed":     ("hard_decline",      False, "no_action"),
    "restricted_card":    ("hard_decline",      False, "escalate"),
    "risk_block":         ("compliance_block",  False, "escalate"),
}


def classify_payment(payment: dict) -> ClassificationResult:
    """
    Classify a single payment record.

    Args:
        payment: dict with at least 'id' and 'error_code' keys.
                 Optional context keys used for override rules:
                 'retry_count' (int, default 0) and 'amount' (float, default 0.0).
                 Matches a row from payments.csv.

    Returns:
        ClassificationResult

    Raises:
        ValueError: if 'error_code' is missing or not in ROOT_CAUSE_TABLE.
                    Fail loud here -- an unrecognized error_code must
                    never silently fall through to a default retryable
                    verdict.
    """
    payment_id = payment.get("id", "<unknown>")
    error_code = payment.get("error_code")

    if not error_code:
        raise ValueError(f"classify_payment: payment {payment_id} is missing 'error_code'")

    if error_code not in ROOT_CAUSE_TABLE:
        raise ValueError(
            f"classify_payment: unrecognized error_code '{error_code}' for payment "
            f"{payment_id}. Add it to ROOT_CAUSE_TABLE explicitly -- do not guess."
        )

    retry_count = int(payment.get("retry_count", 0) or 0)
    amount = float(payment.get("amount", 0.0) or 0.0)

    error_category, base_retryable, base_action = ROOT_CAUSE_TABLE[error_code]

    is_retryable = base_retryable
    action = base_action
    reasoning_parts = [
        f"error_code='{error_code}' maps to category='{error_category}' "
        f"(base_retryable={base_retryable}, base_action='{base_action}')."
    ]

    # Rule 4 first: hard floor, nothing below can override this back to retryable.
    is_hard_floor = error_category in ("hard_decline", "compliance_block")
    if is_hard_floor:
        is_retryable = False
        reasoning_parts.append(
            f"HARD_DECLINE_FLOOR: category '{error_category}' is never retryable "
            f"regardless of context; action remains '{action}'."
        )

    # Rule 1: global retry cap -- applies to everyone, including hard-floor
    # categories (it just restates what's already true for them, and
    # actively overrides otherwise-retryable categories).
    if retry_count >= GLOBAL_MAX_RETRIES:
        is_retryable = False
        if action in RETRYABLE_ACTIONS:
            action = "escalate"
        reasoning_parts.append(
            f"GLOBAL_MAX_RETRIES triggered: retry_count={retry_count} >= "
            f"{GLOBAL_MAX_RETRIES}. Forcing is_retryable=False, action='escalate'."
        )

    # Rule 2: soft-decline / transient categories that have already failed
    # twice get escalated rather than retried a third time.
    elif (
        error_category in ("soft_decline", "transient")
        and retry_count >= SOFT_DECLINE_ESCALATION_THRESHOLD
        and action in RETRYABLE_ACTIONS
    ):
        action = "escalate"
        reasoning_parts.append(
            f"SOFT_DECLINE_ESCALATION_THRESHOLD triggered: category="
            f"'{error_category}' with retry_count={retry_count} >= "
            f"{SOFT_DECLINE_ESCALATION_THRESHOLD}. Downgrading action to "
            f"'escalate' -- repeated failure on a normally-recoverable "
            f"cause signals an unmodeled problem rather than bad luck. "
            f"is_retryable left as {is_retryable} (classifier verdict); "
            f"actual next action is escalation, not another attempt."
        )

    # Rule 3: user_error (invalid_cvv) -- one prompt is reasonable, a
    # second failure after being prompted is not something to keep
    # retrying blindly.
    elif (
        error_category == "user_error"
        and retry_count >= 1
        and action == "prompt_method_switch"
    ):
        action = "escalate"
        reasoning_parts.append(
            f"USER_ERROR_SINGLE_PROMPT triggered: category='user_error' "
            f"with retry_count={retry_count} >= 1. Customer already "
            f"prompted once and failed again; escalating rather than "
            f"prompting a second time."
        )

    # Rule 5: high-value flag, audit-only -- never changes is_retryable or action.
    if error_category == "soft_decline" and amount >= HIGH_VALUE_THRESHOLD:
        reasoning_parts.append(
            f"HIGH_VALUE_FLAG (audit-only): amount={amount:.2f} >= "
            f"{HIGH_VALUE_THRESHOLD:.2f}. Flagged for dashboard priority "
            f"visibility; does not change is_retryable or action."
        )

    return ClassificationResult(
        payment_id=payment_id,
        root_cause=error_code,
        error_category=error_category,
        is_retryable=is_retryable,
        recommended_primary_action=action,
        reasoning=" ".join(reasoning_parts),
    )


def classify_batch(payments: list[dict]) -> list[ClassificationResult]:
    """
    Classify a full batch of payments.

    Does not swallow per-record errors silently -- if one payment has
    an unrecognized error_code, classify_payment() raises ValueError
    and that propagates here. For a batch job you likely want to catch
    and log per-record in the caller (executor.py, Stage 6) rather than
    silently skip; this function intentionally stays strict.
    """
    return [classify_payment(payment) for payment in payments]
