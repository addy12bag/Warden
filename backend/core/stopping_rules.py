"""
Stage 3 -- Stopping rules engine.

This module has FINAL VETO POWER over the LLM decision layer. No action
chosen by decision_agent.py is executed unless it passes through here
first. This is the core "governed agent, not blind loop" guarantee for
the project.

Relationship to Stage 2 (classifier.py):
    The classifier already applies retry-count-based escalation logic
    (GLOBAL_MAX_RETRIES, SOFT_DECLINE_ESCALATION_THRESHOLD, etc.) and
    produces a recommended_primary_action. This module does NOT
    re-derive that logic. Instead it acts as an INDEPENDENT second gate
    that checks the LLM decision layer's ACTUAL chosen action -- which
    may differ from the classifier's recommendation if the LLM
    exercised judgment -- against hard safety limits the classifier
    cannot see:

        1. Timing: has enough cooldown time passed since the last
           attempt on this payment_id? The classifier has no concept
           of wall-clock time between attempts, only a raw count.
        2. Permanent block list: is this specific payment_id/customer_id
           on an explicit block list (e.g. flagged for fraud review),
           independent of its error category?
        3. A final is_retryable sanity check: if the classifier says
           is_retryable=False, no action in RETRYABLE_ACTIONS may pass,
           no matter what the LLM chose or why.
        4. Mandatory escalation: if retry_count has reached
           AUTO_ESCALATE_THRESHOLD or higher, the ONLY actions that may
           pass are "escalate" or "no_action" -- even prompts like
           prompt_method_switch are blocked past this point, since
           repeated customer-facing prompts have their own fatigue cost.

    Two independent gates (classifier + stopping_rules) catching the
    same failure mode in different ways is intentional defense in
    depth, not redundancy -- if the classifier has a bug, or the LLM
    ignores its recommendation, this module still cannot be talked out
    of the hard limits.

Hard rules (see PROJECT.md section 8, Stage 3):
    - MAX_RETRIES: no more than 3 total retry attempts per payment_id
    - COOLDOWN_MINUTES: minimum wait between consecutive retry attempts
    - NEVER_RETRY_CATEGORIES: hard_decline and compliance_block payments
      must never be auto-retried, regardless of LLM suggestion
    - PERMANENT_BLOCK_LIST: specific payment/customer IDs explicitly
      barred from any retry action, independent of category
    - AUTO_ESCALATE_THRESHOLD: after N failed recovery attempts on a
      soft-decline payment, force escalation to a human instead of
      further automated retries

TODO (Stage 3 -- DONE):
    - Implement check_stopping_rules(payment, classification, retry_history)
    - Return either: (allowed=True, None) or (allowed=False, rule_name)
    - Wire into executor.py as a mandatory pre-execution gate
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

MAX_RETRIES = 3
COOLDOWN_MINUTES = 30
AUTO_ESCALATE_THRESHOLD = 2  # failed soft-decline retries before forced escalation

NEVER_RETRY_CATEGORIES = {"hard_decline", "compliance_block"}

RETRYABLE_ACTIONS = {"retry_now", "retry_delayed"}
CUSTOMER_PROMPT_ACTIONS = {"prompt_method_switch", "send_reminder"}
SAFE_TERMINAL_ACTIONS = {"escalate", "no_action"}

# Explicit permanent block list -- specific payment/customer identifiers
# barred from any retry action regardless of error category. In a real
# system this would be backed by a fraud/compliance table; here it's an
# in-memory set for the hackathon scope, kept separate from category
# logic on purpose (see module docstring point 2).
PERMANENT_BLOCK_LIST: set[str] = set()


@dataclass
class StoppingRuleVerdict:
    allowed: bool
    rule_triggered: str | None
    forced_action: str | None  # e.g. "escalate" if a rule overrides the chosen action
    reasoning: str


def _parse_last_attempt_time(payment: dict) -> datetime | None:
    """
    Best-effort parse of the payment's last attempt timestamp.
    Looks for 'last_attempt_at' first, falls back to 'created_at'.
    Returns None if neither is present/parseable -- callers must treat
    None as "cannot verify cooldown, do not assume it's safe."
    """
    raw = payment.get("last_attempt_at") or payment.get("created_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


def check_stopping_rules(
    payment: dict,
    error_category: str,
    is_retryable: bool,
    proposed_action: str,
    retry_count: int,
    now: datetime | None = None,
) -> StoppingRuleVerdict:
    """
    Gate a proposed action (from the LLM decision layer) against hard
    stopping rules. This is the final check before executor.py is
    permitted to act.

    Args:
        payment: raw payment dict, expected to include 'id' and
                 optionally 'customer_id', 'last_attempt_at' (or
                 'created_at' as fallback).
        error_category: classifier's error_category output (Stage 2).
        is_retryable: classifier's is_retryable output (Stage 2).
        proposed_action: the action the LLM decision layer chose.
        retry_count: number of prior attempts on this payment.
        now: current time for cooldown checks; defaults to
             datetime.now() if not provided (injectable for tests).

    Returns:
        StoppingRuleVerdict. If allowed=False, forced_action gives the
        safe replacement action the executor must use instead.
    """
    now = now or datetime.now()
    payment_id = payment.get("id", "<unknown>")
    customer_id = payment.get("customer_id")

    # Rule: permanent block list -- checked first, cannot be bypassed
    # by anything else, independent of category.
    if payment_id in PERMANENT_BLOCK_LIST or (customer_id and customer_id in PERMANENT_BLOCK_LIST):
        if proposed_action in RETRYABLE_ACTIONS or proposed_action in CUSTOMER_PROMPT_ACTIONS:
            return StoppingRuleVerdict(
                allowed=False,
                rule_triggered="PERMANENT_BLOCK_LIST",
                forced_action="escalate",
                reasoning=(
                    f"Payment {payment_id} (customer {customer_id}) is on the "
                    f"permanent block list. Proposed action '{proposed_action}' "
                    f"is blocked regardless of category; forcing 'escalate'."
                ),
            )

    # Rule: classifier sanity check -- if Stage 2 says not retryable,
    # no retryable action passes, no matter what the LLM chose or why
    # it thinks it knows better.
    if not is_retryable and proposed_action in RETRYABLE_ACTIONS:
        return StoppingRuleVerdict(
            allowed=False,
            rule_triggered="CLASSIFIER_NOT_RETRYABLE",
            forced_action="escalate",
            reasoning=(
                f"Classifier marked payment {payment_id} as is_retryable=False "
                f"(category='{error_category}'), but the LLM decision layer "
                f"proposed retryable action '{proposed_action}'. Overriding "
                f"to 'escalate' -- the classifier's verdict is authoritative "
                f"for retryability, the LLM does not have veto power here."
            ),
        )

    # Rule: never-retry categories -- belt-and-suspenders on top of the
    # is_retryable check above, in case a category is ever miscategorized
    # as retryable upstream.
    if error_category in NEVER_RETRY_CATEGORIES and proposed_action in RETRYABLE_ACTIONS:
        return StoppingRuleVerdict(
            allowed=False,
            rule_triggered="NEVER_RETRY_CATEGORY",
            forced_action="escalate",
            reasoning=(
                f"Category '{error_category}' is in NEVER_RETRY_CATEGORIES. "
                f"Proposed action '{proposed_action}' is blocked; forcing 'escalate'."
            ),
        )

    # Rule: global max retries -- hard cap regardless of category.
    if retry_count >= MAX_RETRIES and proposed_action in RETRYABLE_ACTIONS:
        return StoppingRuleVerdict(
            allowed=False,
            rule_triggered="MAX_RETRIES_EXCEEDED",
            forced_action="escalate",
            reasoning=(
                f"retry_count={retry_count} >= MAX_RETRIES={MAX_RETRIES}. "
                f"No further retry actions permitted; forcing 'escalate'."
            ),
        )

    # Rule: mandatory escalation threshold -- past this point, even
    # customer-facing prompts are blocked, not just retries. Repeated
    # prompts to a customer who hasn't resolved the issue carry their
    # own fatigue/trust cost, so only terminal safe actions remain.
    if retry_count >= AUTO_ESCALATE_THRESHOLD and proposed_action not in SAFE_TERMINAL_ACTIONS:
        return StoppingRuleVerdict(
            allowed=False,
            rule_triggered="AUTO_ESCALATE_THRESHOLD",
            forced_action="escalate",
            reasoning=(
                f"retry_count={retry_count} >= AUTO_ESCALATE_THRESHOLD="
                f"{AUTO_ESCALATE_THRESHOLD}. Proposed action '{proposed_action}' "
                f"is not a safe terminal action; forcing 'escalate'."
            ),
        )

    # Rule: cooldown window -- only meaningful for retry actions, and
    # only if we have prior attempt/creation timing to check against.
    if proposed_action in RETRYABLE_ACTIONS and retry_count > 0:
        last_attempt = _parse_last_attempt_time(payment)
        if last_attempt is None:
            # Cannot verify cooldown -- fail safe rather than assume
            # it's fine. This should be rare once executor.py properly
            # stamps last_attempt_at on every attempt.
            return StoppingRuleVerdict(
                allowed=False,
                rule_triggered="COOLDOWN_UNVERIFIABLE",
                forced_action="escalate",
                reasoning=(
                    f"Payment {payment_id} has retry_count={retry_count} > 0 "
                    f"but no parseable last_attempt_at/created_at timestamp, "
                    f"so cooldown cannot be verified. Failing safe to 'escalate' "
                    f"rather than risking a too-soon retry."
                ),
            )

        elapsed = now - last_attempt
        cooldown = timedelta(minutes=COOLDOWN_MINUTES)
        if elapsed < cooldown:
            remaining = cooldown - elapsed
            return StoppingRuleVerdict(
                allowed=False,
                rule_triggered="COOLDOWN_WINDOW_ACTIVE",
                forced_action="retry_delayed" if proposed_action == "retry_now" else proposed_action,
                reasoning=(
                    f"Only {elapsed} has elapsed since the last attempt on "
                    f"{payment_id}; COOLDOWN_MINUTES={COOLDOWN_MINUTES} requires "
                    f"waiting {remaining} more. Downgrading immediate action to "
                    f"a delayed retry rather than blocking outright."
                ),
            )

    # No rule triggered -- the LLM's proposed action passes the gate as-is.
    return StoppingRuleVerdict(
        allowed=True,
        rule_triggered=None,
        forced_action=None,
        reasoning=(
            f"Proposed action '{proposed_action}' for payment {payment_id} "
            f"passes all stopping rules unmodified."
        ),
    )
