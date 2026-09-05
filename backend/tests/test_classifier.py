"""
Unit tests for backend/core/classifier.py (Stage 2).

Run with:
    cd backend && python -m pytest tests/test_classifier.py -v

Or without pytest installed:
    cd backend && python tests/test_classifier.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.classifier import (
    classify_payment,
    classify_batch,
    ROOT_CAUSE_TABLE,
    GLOBAL_MAX_RETRIES,
    SOFT_DECLINE_ESCALATION_THRESHOLD,
    HIGH_VALUE_THRESHOLD,
)


def make_payment(error_code, retry_count=0, amount=500.0, payment_id="txn_test"):
    return {
        "id": payment_id,
        "error_code": error_code,
        "retry_count": retry_count,
        "amount": amount,
    }


# ---------------------------------------------------------------------------
# 1. Base table coverage -- every error_code in ROOT_CAUSE_TABLE, at
#    retry_count=0, should classify to exactly what the table says with
#    no overrides triggered.
# ---------------------------------------------------------------------------

def test_all_error_codes_match_base_table_at_zero_retries():
    for error_code, (category, retryable, action) in ROOT_CAUSE_TABLE.items():
        payment = make_payment(error_code, retry_count=0)
        result = classify_payment(payment)

        assert result.root_cause == error_code
        assert result.error_category == category
        assert result.is_retryable == retryable, (
            f"{error_code}: expected is_retryable={retryable}, got {result.is_retryable}"
        )
        assert result.recommended_primary_action == action, (
            f"{error_code}: expected action='{action}', got '{result.recommended_primary_action}'"
        )
        assert result.reasoning  # never empty


# ---------------------------------------------------------------------------
# 2. Global max retries -- retry_count >= 3 forces escalate, regardless
#    of category, including categories that would normally retry.
# ---------------------------------------------------------------------------

def test_global_max_retries_forces_escalation_on_transient():
    payment = make_payment("gateway_timeout", retry_count=3)
    result = classify_payment(payment)
    assert result.is_retryable is False
    assert result.recommended_primary_action == "escalate"
    assert "GLOBAL_MAX_RETRIES" in result.reasoning


def test_global_max_retries_forces_escalation_on_soft_decline():
    payment = make_payment("insufficient_funds", retry_count=5)  # well past cap
    result = classify_payment(payment)
    assert result.is_retryable is False
    assert result.recommended_primary_action == "escalate"


def test_retry_count_just_below_global_cap_still_retryable():
    # GLOBAL_MAX_RETRIES = 3, so retry_count=2 should NOT trigger rule 1
    # (but for soft_decline it WILL trigger rule 2 at threshold 2 -- use
    # transient here specifically since transient's escalation threshold
    # is also 2, so test at retry_count=1 to isolate rule 1's boundary)
    payment = make_payment("gateway_timeout", retry_count=1)
    result = classify_payment(payment)
    assert result.is_retryable is True
    assert result.recommended_primary_action == "retry_now"


# ---------------------------------------------------------------------------
# 3. THE key edge case from the brief: high retry_count on a soft decline
#    should push toward escalation, NOT another retry.
# ---------------------------------------------------------------------------

def test_soft_decline_with_two_retries_escalates_not_retries():
    payment = make_payment("insufficient_funds", retry_count=2)
    result = classify_payment(payment)
    assert result.recommended_primary_action == "escalate", (
        "A soft decline that has already failed twice must escalate, "
        "not be retried a third time blindly."
    )
    assert "SOFT_DECLINE_ESCALATION_THRESHOLD" in result.reasoning


def test_soft_decline_with_zero_or_one_retry_still_retries():
    for rc in (0, 1):
        payment = make_payment("insufficient_funds", retry_count=rc)
        result = classify_payment(payment)
        assert result.recommended_primary_action == "retry_delayed", (
            f"retry_count={rc} should still allow a normal retry"
        )


def test_transient_category_also_escalates_after_threshold():
    # transient is included in the same escalation rule as soft_decline
    payment = make_payment("network_drop", retry_count=2)
    result = classify_payment(payment)
    assert result.recommended_primary_action == "escalate"


# ---------------------------------------------------------------------------
# 4. Hard decline floor -- never retryable, cannot be overridden upward
#    by any context (low retry_count, low amount, etc.)
# ---------------------------------------------------------------------------

def test_hard_decline_never_retryable_even_at_zero_retries():
    payment = make_payment("expired_card", retry_count=0)
    result = classify_payment(payment)
    assert result.is_retryable is False
    assert result.recommended_primary_action == "prompt_method_switch"
    assert "HARD_DECLINE_FLOOR" in result.reasoning


def test_compliance_block_never_retryable():
    payment = make_payment("risk_block", retry_count=0)
    result = classify_payment(payment)
    assert result.is_retryable is False
    assert result.recommended_primary_action == "escalate"


def test_hard_decline_stays_non_retryable_regardless_of_retry_count():
    for rc in (0, 1, 2, 5):
        payment = make_payment("card_stolen_lost", retry_count=rc)
        result = classify_payment(payment)
        assert result.is_retryable is False, f"retry_count={rc} must not make this retryable"


# ---------------------------------------------------------------------------
# 5. User error (invalid_cvv) -- one prompt allowed, second failure escalates.
# ---------------------------------------------------------------------------

def test_invalid_cvv_first_attempt_prompts_method_switch():
    payment = make_payment("invalid_cvv", retry_count=0)
    result = classify_payment(payment)
    assert result.recommended_primary_action == "prompt_method_switch"


def test_invalid_cvv_second_attempt_escalates():
    payment = make_payment("invalid_cvv", retry_count=1)
    result = classify_payment(payment)
    assert result.recommended_primary_action == "escalate"
    assert "USER_ERROR_SINGLE_PROMPT" in result.reasoning


# ---------------------------------------------------------------------------
# 6. High-value flag -- audit-only, must not change is_retryable or action.
# ---------------------------------------------------------------------------

def test_high_value_soft_decline_flagged_but_action_unchanged():
    low_value = make_payment("insufficient_funds", retry_count=0, amount=500.0)
    high_value = make_payment("insufficient_funds", retry_count=0, amount=15000.0)

    low_result = classify_payment(low_value)
    high_result = classify_payment(high_value)

    # Same action and retryability regardless of amount
    assert low_result.recommended_primary_action == high_result.recommended_primary_action
    assert low_result.is_retryable == high_result.is_retryable

    # But only the high-value one gets flagged in reasoning
    assert "HIGH_VALUE_FLAG" not in low_result.reasoning
    assert "HIGH_VALUE_FLAG" in high_result.reasoning


def test_high_value_threshold_boundary():
    at_threshold = make_payment("insufficient_funds", retry_count=0, amount=HIGH_VALUE_THRESHOLD)
    result = classify_payment(at_threshold)
    assert "HIGH_VALUE_FLAG" in result.reasoning  # >= threshold, inclusive


def test_high_value_flag_only_applies_to_soft_decline():
    # A hard decline at high amount should NOT get the soft-decline-only flag
    high_value_hard = make_payment("expired_card", retry_count=0, amount=50000.0)
    result = classify_payment(high_value_hard)
    assert "HIGH_VALUE_FLAG" not in result.reasoning


# ---------------------------------------------------------------------------
# 7. Missing / unrecognized error_code -- must fail loud, never guess.
# ---------------------------------------------------------------------------

def test_missing_error_code_raises_value_error():
    payment = {"id": "txn_bad", "retry_count": 0}
    try:
        classify_payment(payment)
        assert False, "expected ValueError for missing error_code"
    except ValueError as e:
        assert "missing" in str(e).lower()


def test_unrecognized_error_code_raises_value_error():
    payment = make_payment("some_new_code_not_in_table")
    try:
        classify_payment(payment)
        assert False, "expected ValueError for unrecognized error_code"
    except ValueError as e:
        assert "unrecognized" in str(e).lower()


# ---------------------------------------------------------------------------
# 8. Batch classification
# ---------------------------------------------------------------------------

def test_classify_batch_returns_one_result_per_payment_in_order():
    payments = [
        make_payment("gateway_timeout", payment_id="txn_1"),
        make_payment("expired_card", payment_id="txn_2"),
        make_payment("insufficient_funds", payment_id="txn_3"),
    ]
    results = classify_batch(payments)
    assert len(results) == 3
    assert [r.payment_id for r in results] == ["txn_1", "txn_2", "txn_3"]


def test_classify_batch_propagates_error_on_bad_record():
    payments = [
        make_payment("gateway_timeout", payment_id="txn_1"),
        make_payment("not_a_real_code", payment_id="txn_2"),
    ]
    try:
        classify_batch(payments)
        assert False, "expected ValueError to propagate from classify_batch"
    except ValueError:
        pass


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
