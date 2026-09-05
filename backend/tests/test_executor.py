"""
Unit tests for backend/core/executor.py (Stage 6).

Uses a temporary SQLite file per test (not the real recovery_agent.db)
so these tests never touch or depend on your local dev database.
Mocks decision_agent.decide_action and messaging_agent.draft_message
to avoid real API calls -- this suite tests executor.py's own logic
(outcome simulation, DB writes, batch orchestration), not the LLM
integrations, which already have their own test suites.

Run with:
    cd backend && python -m pytest tests/test_executor.py -v
"""

import sys
import os
import tempfile
from pathlib import Path
from unittest.mock import patch
from collections import Counter

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.executor import (
    execute_action,
    simulate_outcome,
    log_decision,
    process_payment,
    run_batch,
    DecisionRecord,
    OUTCOME_OPTIONS,
    DETERMINISTIC_OUTCOMES,
    BASE_SUCCESS_PROBABILITY,
)
from core.decision_agent import DecisionResult
from core.messaging_agent import MessageResult
from db.db import init_db, get_connection


def make_payment(payment_id="txn_test", error_code="gateway_timeout", retry_count=0,
                  amount=500.0, customer_tenure_days=100, language_pref="en"):
    return {
        "id": payment_id,
        "error_code": error_code,
        "retry_count": retry_count,
        "amount": amount,
        "customer_tenure_days": customer_tenure_days,
        "language_pref": language_pref,
        "payment_method": "card",
        "customer_id": "cust_test",
    }


def insert_payment_row(payment: dict, db_path: str):
    """
    Insert a minimal matching row into the `payments` table so that
    decisions.payment_id's foreign key constraint is satisfied. The
    decisions table intentionally enforces this FK (see db/models.py)
    to guarantee the audit trail can never reference a payment that
    doesn't exist -- so tests must set up payments first, matching how
    the real pipeline works (payments.csv is loaded before any batch run).
    """
    conn = get_connection(db_path)
    conn.execute(
        """
        INSERT OR REPLACE INTO payments (
            id, customer_id, customer_name, amount, payment_method,
            error_code, error_category, retry_count, created_at,
            customer_tenure_days, language_pref
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payment["id"], payment.get("customer_id", "cust_test"), "Test Customer",
            payment.get("amount", 500.0), payment.get("payment_method", "card"),
            payment.get("error_code", "gateway_timeout"), "transient",
            payment.get("retry_count", 0), "2026-08-25T00:00:00",
            payment.get("customer_tenure_days", 100), payment.get("language_pref", "en"),
        ),
    )
    conn.commit()
    conn.close()


@patch.dict(os.environ, {}, clear=True)
def temp_db():
    """Yields a path to a fresh temp SQLite file, cleaned up after."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)  # let init_db create it fresh
    init_db(path)
    return path


# ---------------------------------------------------------------------------
# 1. Outcome simulation -- deterministic actions
# ---------------------------------------------------------------------------

def test_escalate_always_escalated():
    p = make_payment()
    for _ in range(20):
        assert execute_action(p, "escalate") == "escalated"


def test_no_action_always_still_failing():
    p = make_payment()
    for _ in range(20):
        assert execute_action(p, "no_action") == "still_failing"


def test_deterministic_outcomes_mapping_is_exhaustive_for_non_probabilistic_actions():
    assert DETERMINISTIC_OUTCOMES == {"escalate": "escalated", "no_action": "still_failing"}


# ---------------------------------------------------------------------------
# 2. Outcome simulation -- probabilistic actions, reproducibility
# ---------------------------------------------------------------------------

def test_same_payment_id_and_action_gives_same_outcome_on_repeated_calls():
    p = make_payment(payment_id="txn_repro_test")
    first = execute_action(p, "retry_now")
    for _ in range(10):
        assert execute_action(p, "retry_now") == first


def test_different_payment_ids_can_give_different_outcomes():
    # Not a strict guarantee for any single pair, but across enough
    # distinct IDs at a mid-range probability we should see both
    # outcomes appear -- if this ever flakes, the RNG seeding is broken.
    outcomes = {execute_action(make_payment(payment_id=f"txn_{i}"), "retry_delayed") for i in range(50)}
    assert "recovered" in outcomes
    assert "still_failing" in outcomes


def test_outcome_is_always_in_outcome_options():
    for action in list(BASE_SUCCESS_PROBABILITY.keys()) + list(DETERMINISTIC_OUTCOMES.keys()):
        for i in range(5):
            outcome = execute_action(make_payment(payment_id=f"txn_check_{action}_{i}"), action)
            assert outcome in OUTCOME_OPTIONS


def test_unknown_action_fails_safe_to_still_failing():
    p = make_payment()
    assert simulate_outcome(p, "some_action_not_in_any_mapping") == "still_failing"


# ---------------------------------------------------------------------------
# 3. Tenure bonus for prompt_method_switch
# ---------------------------------------------------------------------------

def test_high_tenure_customers_recover_more_often_on_prompt_method_switch():
    # Statistical test: across many distinct payment IDs, long-tenure
    # customers should show a higher recovery rate than short-tenure
    # ones for prompt_method_switch specifically.
    short_tenure_recovered = sum(
        1 for i in range(300)
        if execute_action(
            make_payment(payment_id=f"txn_short_{i}", customer_tenure_days=10),
            "prompt_method_switch",
        ) == "recovered"
    )
    long_tenure_recovered = sum(
        1 for i in range(300)
        if execute_action(
            make_payment(payment_id=f"txn_long_{i}", customer_tenure_days=1000),
            "prompt_method_switch",
        ) == "recovered"
    )
    # Expect roughly a TENURE_BONUS-sized gap (8%) at n=300; allow
    # generous slack for statistical noise while still catching a
    # completely broken/inverted implementation.
    assert long_tenure_recovered > short_tenure_recovered


# ---------------------------------------------------------------------------
# 4. log_decision -- actually writes to the DB correctly
# ---------------------------------------------------------------------------

def test_log_decision_writes_row_with_correct_values():
    db_path = temp_db()
    insert_payment_row(make_payment(payment_id="txn_log_test"), db_path)
    record = DecisionRecord(
        payment_id="txn_log_test",
        root_cause="gateway_timeout",
        is_retryable=True,
        chosen_action="retry_now",
        reasoning="test reasoning",
        message_sent=None,
        stopping_rule_triggered=None,
        outcome="recovered",
        timestamp="2026-08-25T12:00:00",
    )
    log_decision(record, db_path)

    conn = get_connection(db_path)
    row = conn.execute("SELECT * FROM decisions WHERE payment_id = ?", ("txn_log_test",)).fetchone()
    conn.close()

    assert row is not None
    assert row["root_cause"] == "gateway_timeout"
    assert row["is_retryable"] == 1  # stored as 0/1 int
    assert row["chosen_action"] == "retry_now"
    assert row["outcome"] == "recovered"
    assert row["message_sent"] is None
    os.remove(db_path)


def test_log_decision_stores_message_sent_when_present():
    db_path = temp_db()
    insert_payment_row(make_payment(payment_id="txn_msg_test"), db_path)
    record = DecisionRecord(
        payment_id="txn_msg_test",
        root_cause="expired_card",
        is_retryable=False,
        chosen_action="prompt_method_switch",
        reasoning="test",
        message_sent="Please update your payment method.",
        stopping_rule_triggered=None,
        outcome="recovered",
        timestamp="2026-08-25T12:00:00",
    )
    log_decision(record, db_path)

    conn = get_connection(db_path)
    row = conn.execute("SELECT message_sent FROM decisions WHERE payment_id = ?", ("txn_msg_test",)).fetchone()
    conn.close()

    assert row["message_sent"] == "Please update your payment method."
    os.remove(db_path)


# ---------------------------------------------------------------------------
# 5. process_payment -- full single-payment pipeline, LLM calls mocked
# ---------------------------------------------------------------------------

def _mock_decide(classification, payment_context):
    return DecisionResult(
        payment_id=payment_context["id"],
        chosen_action=classification["recommended_primary_action"],
        reasoning="mocked decision",
        raw_model_output="{}",
        fallback_used=False,
    )


def _mock_draft(payment, chosen_action, language_pref):
    return MessageResult(
        payment_id=payment["id"],
        message=f"mocked message for {chosen_action}",
        language_used=language_pref,
        fallback_used=False,
    )


@patch("core.executor.draft_message", side_effect=_mock_draft)
@patch("core.executor.decide_action", side_effect=_mock_decide)
def test_process_payment_writes_and_returns_matching_record(mock_decide, mock_draft):
    db_path = temp_db()
    payment = make_payment(payment_id="txn_process_test", error_code="gateway_timeout")
    insert_payment_row(payment, db_path)

    record = process_payment(payment, db_path)

    assert record.payment_id == "txn_process_test"
    assert record.chosen_action == "retry_now"  # classifier's recommendation for gateway_timeout
    assert record.outcome in OUTCOME_OPTIONS

    conn = get_connection(db_path)
    row = conn.execute("SELECT * FROM decisions WHERE payment_id = ?", ("txn_process_test",)).fetchone()
    conn.close()
    assert row is not None
    assert row["chosen_action"] == record.chosen_action
    os.remove(db_path)


@patch("core.executor.draft_message", side_effect=_mock_draft)
@patch("core.executor.decide_action", side_effect=_mock_decide)
def test_process_payment_skips_message_for_non_messaging_action(mock_decide, mock_draft):
    db_path = temp_db()
    payment = make_payment(payment_id="txn_no_msg", error_code="gateway_timeout")  # -> retry_now
    insert_payment_row(payment, db_path)

    record = process_payment(payment, db_path)

    assert record.chosen_action == "retry_now"
    assert record.message_sent is None
    mock_draft.assert_not_called()
    os.remove(db_path)


@patch("core.executor.draft_message", side_effect=_mock_draft)
@patch("core.executor.decide_action", side_effect=_mock_decide)
def test_process_payment_includes_message_for_messaging_action(mock_decide, mock_draft):
    db_path = temp_db()
    payment = make_payment(payment_id="txn_with_msg", error_code="expired_card")  # -> prompt_method_switch
    insert_payment_row(payment, db_path)

    record = process_payment(payment, db_path)

    assert record.chosen_action == "prompt_method_switch"
    assert record.message_sent == "mocked message for prompt_method_switch"
    mock_draft.assert_called_once()
    os.remove(db_path)


@patch("core.executor.draft_message", side_effect=_mock_draft)
def test_process_payment_records_stopping_rule_when_llm_overridden(mock_draft):
    # Force the LLM to propose something unsafe (retry on a hard
    # decline) and confirm the stopping rule override is recorded.
    def bad_decide(classification, payment_context):
        return DecisionResult(
            payment_id=payment_context["id"],
            chosen_action="retry_now",  # unsafe for a hard decline
            reasoning="LLM ignored classifier",
            raw_model_output="{}",
            fallback_used=False,
        )

    db_path = temp_db()
    payment = make_payment(payment_id="txn_override_test", error_code="expired_card")
    insert_payment_row(payment, db_path)

    with patch("core.executor.decide_action", side_effect=bad_decide):
        record = process_payment(payment, db_path)

    assert record.chosen_action == "escalate"  # stopping rules forced this
    assert record.stopping_rule_triggered == "CLASSIFIER_NOT_RETRYABLE"
    assert "[stopping_rules]" in record.reasoning
    os.remove(db_path)


# ---------------------------------------------------------------------------
# 6. run_batch -- multiple payments, resilience to individual failures
# ---------------------------------------------------------------------------

@patch("core.executor.draft_message", side_effect=_mock_draft)
@patch("core.executor.decide_action", side_effect=_mock_decide)
def test_run_batch_processes_all_payments_and_returns_matching_count(mock_decide, mock_draft):
    db_path = temp_db()
    payments = [
        make_payment(payment_id="txn_batch_1", error_code="gateway_timeout"),
        make_payment(payment_id="txn_batch_2", error_code="expired_card"),
        make_payment(payment_id="txn_batch_3", error_code="insufficient_funds"),
    ]
    for p in payments:
        insert_payment_row(p, db_path)

    records = run_batch(payments, db_path)

    assert len(records) == 3
    assert [r.payment_id for r in records] == ["txn_batch_1", "txn_batch_2", "txn_batch_3"]

    conn = get_connection(db_path)
    count = conn.execute("SELECT COUNT(*) as c FROM decisions").fetchone()["c"]
    conn.close()
    assert count == 3
    os.remove(db_path)


@patch("core.executor.draft_message", side_effect=_mock_draft)
@patch("core.executor.decide_action", side_effect=_mock_decide)
def test_run_batch_continues_past_malformed_payment(mock_decide, mock_draft):
    db_path = temp_db()
    payments = [
        make_payment(payment_id="txn_good_1", error_code="gateway_timeout"),
        {"id": "txn_bad", "error_code": "not_a_real_code"},  # will raise in classify_payment
        make_payment(payment_id="txn_good_2", error_code="insufficient_funds"),
    ]
    # All three need a payments row for the decisions FK, including the
    # malformed one -- in the real pipeline every row in payments.csv is
    # bulk-loaded into `payments` up front (via load_payments_csv) BEFORE
    # run_batch executes, so even a payment with a bad error_code still
    # exists in the payments table; only its *processing* fails.
    insert_payment_row(make_payment(payment_id="txn_good_1", error_code="gateway_timeout"), db_path)
    insert_payment_row(make_payment(payment_id="txn_bad", error_code="not_a_real_code"), db_path)
    insert_payment_row(make_payment(payment_id="txn_good_2", error_code="insufficient_funds"), db_path)

    records = run_batch(payments, db_path)

    assert len(records) == 3  # batch didn't stop early
    assert records[0].payment_id == "txn_good_1"
    assert records[1].payment_id == "txn_bad"
    assert records[1].outcome == "escalated"  # failed safe
    assert records[1].stopping_rule_triggered == "PROCESSING_ERROR"
    assert records[2].payment_id == "txn_good_2"
    os.remove(db_path)


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
