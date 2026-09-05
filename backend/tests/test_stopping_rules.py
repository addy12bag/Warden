"""
Unit tests for backend/core/stopping_rules.py (Stage 3).

Run with:
    cd backend && python -m pytest tests/test_stopping_rules.py -v

Or without pytest installed:
    cd backend && python tests/test_stopping_rules.py
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.stopping_rules import (
    check_stopping_rules,
    PERMANENT_BLOCK_LIST,
    MAX_RETRIES,
    COOLDOWN_MINUTES,
    AUTO_ESCALATE_THRESHOLD,
)

FIXED_NOW = datetime(2026, 8, 24, 12, 0, 0)


def make_payment(payment_id="txn_test", customer_id="cust_test", minutes_since_last_attempt=60):
    return {
        "id": payment_id,
        "customer_id": customer_id,
        "created_at": (FIXED_NOW - timedelta(minutes=minutes_since_last_attempt)).isoformat(),
    }


# ---------------------------------------------------------------------------
# 1. Happy path -- action passes through unmodified when nothing is triggered
# ---------------------------------------------------------------------------

def test_valid_retry_now_passes_when_well_outside_cooldown_and_under_caps():
    payment = make_payment(minutes_since_last_attempt=60)
    verdict = check_stopping_rules(
        payment, error_category="transient", is_retryable=True,
        proposed_action="retry_now", retry_count=1, now=FIXED_NOW,
    )
    assert verdict.allowed is True
    assert verdict.rule_triggered is None
    assert verdict.forced_action is None


def test_escalate_always_passes_regardless_of_context():
    payment = make_payment()
    verdict = check_stopping_rules(
        payment, error_category="hard_decline", is_retryable=False,
        proposed_action="escalate", retry_count=5, now=FIXED_NOW,
    )
    assert verdict.allowed is True


# ---------------------------------------------------------------------------
# 2. Permanent block list -- checked first, cannot be bypassed
# ---------------------------------------------------------------------------

def test_permanent_block_list_blocks_retry():
    PERMANENT_BLOCK_LIST.add("txn_blocked")
    try:
        payment = make_payment(payment_id="txn_blocked", minutes_since_last_attempt=999)
        verdict = check_stopping_rules(
            payment, error_category="soft_decline", is_retryable=True,
            proposed_action="retry_delayed", retry_count=0, now=FIXED_NOW,
        )
        assert verdict.allowed is False
        assert verdict.rule_triggered == "PERMANENT_BLOCK_LIST"
        assert verdict.forced_action == "escalate"
    finally:
        PERMANENT_BLOCK_LIST.discard("txn_blocked")


def test_permanent_block_list_blocks_customer_prompt_too():
    PERMANENT_BLOCK_LIST.add("cust_flagged")
    try:
        payment = make_payment(customer_id="cust_flagged", minutes_since_last_attempt=999)
        verdict = check_stopping_rules(
            payment, error_category="user_error", is_retryable=True,
            proposed_action="prompt_method_switch", retry_count=0, now=FIXED_NOW,
        )
        assert verdict.allowed is False
        assert verdict.rule_triggered == "PERMANENT_BLOCK_LIST"
    finally:
        PERMANENT_BLOCK_LIST.discard("cust_flagged")


def test_block_list_does_not_block_escalate_or_no_action():
    PERMANENT_BLOCK_LIST.add("txn_blocked2")
    try:
        payment = make_payment(payment_id="txn_blocked2")
        verdict = check_stopping_rules(
            payment, error_category="soft_decline", is_retryable=True,
            proposed_action="escalate", retry_count=0, now=FIXED_NOW,
        )
        # escalate is already the safe action, block list shouldn't need to fire
        assert verdict.allowed is True
    finally:
        PERMANENT_BLOCK_LIST.discard("txn_blocked2")


# ---------------------------------------------------------------------------
# 3. Classifier sanity check -- LLM cannot override is_retryable=False
# ---------------------------------------------------------------------------

def test_llm_cannot_override_classifier_not_retryable():
    payment = make_payment(minutes_since_last_attempt=999)
    verdict = check_stopping_rules(
        payment, error_category="hard_decline", is_retryable=False,
        proposed_action="retry_now", retry_count=0, now=FIXED_NOW,
    )
    assert verdict.allowed is False
    assert verdict.rule_triggered == "CLASSIFIER_NOT_RETRYABLE"
    assert verdict.forced_action == "escalate"


def test_non_retryable_but_non_retry_action_is_fine():
    # is_retryable=False but the LLM correctly chose a non-retry action
    payment = make_payment()
    verdict = check_stopping_rules(
        payment, error_category="hard_decline", is_retryable=False,
        proposed_action="prompt_method_switch", retry_count=0, now=FIXED_NOW,
    )
    assert verdict.allowed is True


# ---------------------------------------------------------------------------
# 4. Never-retry categories (belt and suspenders on top of is_retryable)
# ---------------------------------------------------------------------------

def test_never_retry_category_blocks_even_if_marked_retryable_by_mistake():
    # Simulates a hypothetical upstream bug where is_retryable was
    # miscomputed as True for a hard_decline -- this rule should still
    # catch it independently.
    payment = make_payment(minutes_since_last_attempt=999)
    verdict = check_stopping_rules(
        payment, error_category="compliance_block", is_retryable=True,
        proposed_action="retry_now", retry_count=0, now=FIXED_NOW,
    )
    assert verdict.allowed is False
    assert verdict.rule_triggered == "NEVER_RETRY_CATEGORY"


# ---------------------------------------------------------------------------
# 5. Global max retries
# ---------------------------------------------------------------------------

def test_max_retries_exceeded_blocks_retry():
    payment = make_payment(minutes_since_last_attempt=999)
    verdict = check_stopping_rules(
        payment, error_category="soft_decline", is_retryable=True,
        proposed_action="retry_delayed", retry_count=MAX_RETRIES, now=FIXED_NOW,
    )
    assert verdict.allowed is False
    assert verdict.rule_triggered == "MAX_RETRIES_EXCEEDED"
    assert verdict.forced_action == "escalate"


def test_retry_count_just_under_max_is_allowed_outside_other_rules():
    # MAX_RETRIES=3, so retry_count=2 should NOT trigger this rule.
    # But AUTO_ESCALATE_THRESHOLD=2 WILL trigger for a retry action at
    # retry_count=2 -- so to isolate MAX_RETRIES specifically, propose
    # a safe terminal action instead.
    payment = make_payment(minutes_since_last_attempt=999)
    verdict = check_stopping_rules(
        payment, error_category="soft_decline", is_retryable=True,
        proposed_action="escalate", retry_count=MAX_RETRIES - 1, now=FIXED_NOW,
    )
    assert verdict.allowed is True


# ---------------------------------------------------------------------------
# 6. Mandatory auto-escalation threshold -- blocks prompts too, not just retries
# ---------------------------------------------------------------------------

def test_auto_escalate_threshold_blocks_customer_prompt():
    payment = make_payment(minutes_since_last_attempt=999)
    verdict = check_stopping_rules(
        payment, error_category="user_error", is_retryable=True,
        proposed_action="prompt_method_switch", retry_count=AUTO_ESCALATE_THRESHOLD, now=FIXED_NOW,
    )
    assert verdict.allowed is False
    assert verdict.rule_triggered == "AUTO_ESCALATE_THRESHOLD"


def test_auto_escalate_threshold_allows_safe_terminal_actions():
    payment = make_payment(minutes_since_last_attempt=999)
    for action in ("escalate", "no_action"):
        verdict = check_stopping_rules(
            payment, error_category="soft_decline", is_retryable=True,
            proposed_action=action, retry_count=AUTO_ESCALATE_THRESHOLD, now=FIXED_NOW,
        )
        assert verdict.allowed is True, f"{action} should pass at the escalation threshold"


def test_below_auto_escalate_threshold_prompts_still_allowed():
    payment = make_payment(minutes_since_last_attempt=999)
    verdict = check_stopping_rules(
        payment, error_category="user_error", is_retryable=True,
        proposed_action="prompt_method_switch", retry_count=AUTO_ESCALATE_THRESHOLD - 1, now=FIXED_NOW,
    )
    assert verdict.allowed is True


# ---------------------------------------------------------------------------
# 7. Cooldown window
# ---------------------------------------------------------------------------

def test_cooldown_blocks_retry_now_too_soon_after_last_attempt():
    payment = make_payment(minutes_since_last_attempt=5)
    verdict = check_stopping_rules(
        payment, error_category="transient", is_retryable=True,
        proposed_action="retry_now", retry_count=1, now=FIXED_NOW,
    )
    assert verdict.allowed is False
    assert verdict.rule_triggered == "COOLDOWN_WINDOW_ACTIVE"
    assert verdict.forced_action == "retry_delayed"


def test_cooldown_allows_retry_after_window_elapsed():
    payment = make_payment(minutes_since_last_attempt=COOLDOWN_MINUTES + 1)
    verdict = check_stopping_rules(
        payment, error_category="transient", is_retryable=True,
        proposed_action="retry_now", retry_count=1, now=FIXED_NOW,
    )
    assert verdict.allowed is True


def test_cooldown_boundary_exactly_at_threshold_is_allowed():
    payment = make_payment(minutes_since_last_attempt=COOLDOWN_MINUTES)
    verdict = check_stopping_rules(
        payment, error_category="transient", is_retryable=True,
        proposed_action="retry_now", retry_count=1, now=FIXED_NOW,
    )
    assert verdict.allowed is True  # elapsed == cooldown, not strictly less than


def test_cooldown_not_checked_on_first_attempt():
    # retry_count=0 means there's no "last attempt" to cool down from
    payment = {"id": "txn_first", "customer_id": "cust_1", "created_at": FIXED_NOW.isoformat()}
    verdict = check_stopping_rules(
        payment, error_category="transient", is_retryable=True,
        proposed_action="retry_now", retry_count=0, now=FIXED_NOW,
    )
    assert verdict.allowed is True


def test_cooldown_unverifiable_fails_safe():
    payment = {"id": "txn_no_timestamp", "customer_id": "cust_1"}  # no created_at at all
    verdict = check_stopping_rules(
        payment, error_category="transient", is_retryable=True,
        proposed_action="retry_now", retry_count=1, now=FIXED_NOW,
    )
    assert verdict.allowed is False
    assert verdict.rule_triggered == "COOLDOWN_UNVERIFIABLE"
    assert verdict.forced_action == "escalate"


def test_cooldown_not_checked_for_non_retry_actions():
    payment = make_payment(minutes_since_last_attempt=1)  # very recent
    verdict = check_stopping_rules(
        payment, error_category="user_error", is_retryable=True,
        proposed_action="prompt_method_switch", retry_count=1, now=FIXED_NOW,
    )
    assert verdict.allowed is True  # cooldown only applies to retry actions


# ---------------------------------------------------------------------------
# 8. Rule precedence -- when multiple rules could fire, block list and
#    classifier checks take priority over cooldown/threshold checks
# ---------------------------------------------------------------------------

def test_block_list_takes_precedence_over_cooldown():
    PERMANENT_BLOCK_LIST.add("txn_both")
    try:
        # This payment would ALSO fail cooldown, but block list should
        # be the reported reason since it's checked first.
        payment = make_payment(payment_id="txn_both", minutes_since_last_attempt=1)
        verdict = check_stopping_rules(
            payment, error_category="soft_decline", is_retryable=True,
            proposed_action="retry_now", retry_count=1, now=FIXED_NOW,
        )
        assert verdict.rule_triggered == "PERMANENT_BLOCK_LIST"
    finally:
        PERMANENT_BLOCK_LIST.discard("txn_both")


def test_not_retryable_takes_precedence_over_max_retries():
    payment = make_payment(minutes_since_last_attempt=999)
    verdict = check_stopping_rules(
        payment, error_category="hard_decline", is_retryable=False,
        proposed_action="retry_now", retry_count=MAX_RETRIES, now=FIXED_NOW,
    )
    assert verdict.rule_triggered == "CLASSIFIER_NOT_RETRYABLE"


# ---------------------------------------------------------------------------
# Test runner (in case pytest isn't installed in this environment)
# ---------------------------------------------------------------------------

def _run_all():
    import traceback

    tests = [obj for name, obj in list(globals().items())
              if name.startswith("test_") and callable(obj)]

    passed, failed = 0, 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {test.__name__}: {e}")
            failed += 1
        except Exception:
            print(f"  ERROR {test.__name__}:")
            traceback.print_exc()
            failed += 1

    print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
    return failed == 0


if __name__ == "__main__":
    success = _run_all()
    sys.exit(0 if success else 1)
