"""
Unit tests for backend/evaluate.py (Stage 7).

Uses small, fully hand-crafted decisions + ground-truth datasets with
known correct answers, rather than the real 500-record batch -- this
lets every metric be checked against an exact expected value instead
of just "did it run without crashing."

Run with:
    cd backend && python -m pytest tests/test_evaluate.py -v
"""

import sys
import os
import csv
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from evaluate import compute_metrics, RETRY_ACTIONS
from db.db import init_db, get_connection


def make_test_db(payments: list[tuple], decisions: list[tuple]) -> str:
    db_path = tempfile.mktemp(suffix=".db")
    init_db(db_path)
    conn = get_connection(db_path)
    for p in payments:
        conn.execute(
            """INSERT INTO payments (id, customer_id, customer_name, amount,
               payment_method, error_code, error_category, retry_count,
               created_at, customer_tenure_days, language_pref)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            p,
        )
    for d in decisions:
        conn.execute(
            """INSERT INTO decisions (payment_id, root_cause, is_retryable,
               chosen_action, reasoning, message_sent, stopping_rule_triggered,
               outcome, timestamp) VALUES (?,?,?,?,?,?,?,?,?)""",
            d,
        )
    conn.commit()
    conn.close()
    return db_path


def make_ground_truth_csv(labels: dict) -> str:
    """labels: {payment_id: bool}"""
    path = tempfile.mktemp(suffix=".csv")
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "_ground_truth_recoverable"])
        for pid, recoverable in labels.items():
            writer.writerow([pid, str(recoverable)])
    return path


def default_payment(pid, amount=100.0, error_code="gateway_timeout", created_at="2026-08-01T00:00:00"):
    return (pid, "cust_1", "Test", amount, "card", error_code, "transient", 0, created_at, 100, "en")


def default_decision(pid, chosen_action, outcome, error_code="gateway_timeout",
                      timestamp="2026-08-01T01:00:00", stopping_rule=None):
    return (pid, error_code, 1, chosen_action, "test reasoning", None, stopping_rule, outcome, timestamp)


# ---------------------------------------------------------------------------
# 1. Recovery rate and money recovered -- exact known values
# ---------------------------------------------------------------------------

def test_recovery_rate_and_money_recovered_exact_values():
    payments = [default_payment("txn_1", 100.0), default_payment("txn_2", 200.0),
                default_payment("txn_3", 300.0), default_payment("txn_4", 400.0)]
    decisions = [
        default_decision("txn_1", "retry_now", "recovered"),
        default_decision("txn_2", "retry_now", "recovered"),
        default_decision("txn_3", "escalate", "escalated"),
        default_decision("txn_4", "no_action", "still_failing"),
    ]
    db_path = make_test_db(payments, decisions)
    gt_path = make_ground_truth_csv({"txn_1": True, "txn_2": True, "txn_3": False, "txn_4": True})

    result = compute_metrics(db_path, gt_path)

    assert result.total_decisions == 4
    assert result.total_batch_amount == 1000.0
    assert result.recovered_count == 2
    assert result.recovered_amount == 300.0  # 100 + 200
    assert result.recovery_rate_pct == 50.0
    assert result.still_at_risk_amount == 700.0  # 300 (escalated) + 400 (still_failing)

    os.remove(db_path)
    os.remove(gt_path)


# ---------------------------------------------------------------------------
# 2. False-retry rate -- the core integrity check
# ---------------------------------------------------------------------------

def test_false_retry_detects_retry_on_unrecoverable_payment():
    payments = [default_payment("txn_bad", 500.0, error_code="risk_block")]
    decisions = [default_decision("txn_bad", "retry_now", "still_failing", error_code="risk_block")]
    db_path = make_test_db(payments, decisions)
    gt_path = make_ground_truth_csv({"txn_bad": False})  # ground truth: NOT recoverable

    result = compute_metrics(db_path, gt_path)

    assert result.false_retry_count == 1
    assert result.false_retry_rate_pct == 100.0
    assert result.false_retry_examples[0]["payment_id"] == "txn_bad"

    os.remove(db_path)
    os.remove(gt_path)


def test_false_retry_zero_when_no_bad_retries():
    payments = [default_payment("txn_good", 500.0)]
    decisions = [default_decision("txn_good", "retry_now", "recovered")]
    db_path = make_test_db(payments, decisions)
    gt_path = make_ground_truth_csv({"txn_good": True})  # ground truth: recoverable, retry was correct

    result = compute_metrics(db_path, gt_path)

    assert result.false_retry_count == 0
    assert result.false_retry_rate_pct == 0.0
    assert result.false_retry_examples == []

    os.remove(db_path)
    os.remove(gt_path)


def test_false_retry_ignores_non_retry_actions_even_if_unrecoverable():
    # escalate on an unrecoverable payment is CORRECT behavior, not a
    # false retry -- only retry_now/retry_delayed count.
    payments = [default_payment("txn_escalated", 500.0)]
    decisions = [default_decision("txn_escalated", "escalate", "escalated")]
    db_path = make_test_db(payments, decisions)
    gt_path = make_ground_truth_csv({"txn_escalated": False})

    result = compute_metrics(db_path, gt_path)

    assert result.false_retry_count == 0

    os.remove(db_path)
    os.remove(gt_path)


def test_false_retry_counts_both_retry_now_and_retry_delayed():
    payments = [default_payment("txn_1", 100.0), default_payment("txn_2", 200.0)]
    decisions = [
        default_decision("txn_1", "retry_now", "still_failing"),
        default_decision("txn_2", "retry_delayed", "still_failing"),
    ]
    db_path = make_test_db(payments, decisions)
    gt_path = make_ground_truth_csv({"txn_1": False, "txn_2": False})

    result = compute_metrics(db_path, gt_path)

    assert result.false_retry_count == 2

    os.remove(db_path)
    os.remove(gt_path)


# ---------------------------------------------------------------------------
# 3. Exceptions list -- honest and complete, not cherry-picked
# ---------------------------------------------------------------------------

def test_exceptions_includes_still_failing_and_pending_only():
    payments = [default_payment(f"txn_{i}", 100.0) for i in range(4)]
    decisions = [
        default_decision("txn_0", "retry_now", "recovered"),
        default_decision("txn_1", "escalate", "escalated"),
        default_decision("txn_2", "no_action", "still_failing"),
        default_decision("txn_3", "retry_delayed", "pending"),
    ]
    db_path = make_test_db(payments, decisions)
    gt_path = make_ground_truth_csv({f"txn_{i}": True for i in range(4)})

    result = compute_metrics(db_path, gt_path)

    exception_ids = {e["payment_id"] for e in result.exceptions}
    assert exception_ids == {"txn_2", "txn_3"}  # still_failing and pending only
    assert "txn_0" not in exception_ids  # recovered -- not an exception
    assert "txn_1" not in exception_ids  # escalated -- not an exception (handed to human, not "unresolved")

    os.remove(db_path)
    os.remove(gt_path)


def test_exceptions_are_never_silently_capped_in_the_result_object():
    # console printing caps at 20, but the actual result.exceptions list
    # must contain ALL of them -- "reported honestly, not cherry-picked"
    payments = [default_payment(f"txn_{i}", 100.0) for i in range(30)]
    decisions = [default_decision(f"txn_{i}", "no_action", "still_failing") for i in range(30)]
    db_path = make_test_db(payments, decisions)
    gt_path = make_ground_truth_csv({f"txn_{i}": True for i in range(30)})

    result = compute_metrics(db_path, gt_path)

    assert len(result.exceptions) == 30  # not capped at 20

    os.remove(db_path)
    os.remove(gt_path)


# ---------------------------------------------------------------------------
# 4. Recovery rate by action -- correct per-bucket math
# ---------------------------------------------------------------------------

def test_recovery_rate_by_action_is_bucketed_correctly():
    payments = [default_payment(f"txn_{i}", 100.0) for i in range(4)]
    decisions = [
        default_decision("txn_0", "retry_now", "recovered"),
        default_decision("txn_1", "retry_now", "still_failing"),
        default_decision("txn_2", "escalate", "escalated"),
        default_decision("txn_3", "escalate", "escalated"),
    ]
    db_path = make_test_db(payments, decisions)
    gt_path = make_ground_truth_csv({f"txn_{i}": True for i in range(4)})

    result = compute_metrics(db_path, gt_path)

    assert result.recovery_rate_by_action["retry_now"]["count"] == 2
    assert result.recovery_rate_by_action["retry_now"]["recovered"] == 1
    assert result.recovery_rate_by_action["retry_now"]["recovery_rate_pct"] == 50.0
    assert result.recovery_rate_by_action["escalate"]["recovered"] == 0
    assert result.recovery_rate_by_action["escalate"]["recovery_rate_pct"] == 0.0

    os.remove(db_path)
    os.remove(gt_path)


# ---------------------------------------------------------------------------
# 5. Time-to-decision
# ---------------------------------------------------------------------------

def test_time_to_decision_computes_correct_average_hours():
    payments = [
        default_payment("txn_1", 100.0, created_at="2026-08-01T00:00:00"),
        default_payment("txn_2", 100.0, created_at="2026-08-01T00:00:00"),
    ]
    decisions = [
        default_decision("txn_1", "retry_now", "recovered", timestamp="2026-08-01T01:00:00"),  # 1 hour
        default_decision("txn_2", "retry_now", "recovered", timestamp="2026-08-01T03:00:00"),  # 3 hours
    ]
    db_path = make_test_db(payments, decisions)
    gt_path = make_ground_truth_csv({"txn_1": True, "txn_2": True})

    result = compute_metrics(db_path, gt_path)

    assert result.avg_time_to_decision_hours == 2.0  # (1+3)/2
    assert "NOT actual time-to-money-recovered" in result.time_to_decision_note

    os.remove(db_path)
    os.remove(gt_path)


def test_time_to_decision_handles_unparseable_timestamps_gracefully():
    payments = [default_payment("txn_1", 100.0, created_at="not-a-date")]
    decisions = [default_decision("txn_1", "retry_now", "recovered", timestamp="also-not-a-date")]
    db_path = make_test_db(payments, decisions)
    gt_path = make_ground_truth_csv({"txn_1": True})

    result = compute_metrics(db_path, gt_path)

    assert result.avg_time_to_decision_hours is None  # no crash, graceful None

    os.remove(db_path)
    os.remove(gt_path)


# ---------------------------------------------------------------------------
# 6. Error handling -- empty decisions table, mismatched files
# ---------------------------------------------------------------------------

def test_empty_decisions_table_raises_clear_error():
    db_path = tempfile.mktemp(suffix=".db")
    init_db(db_path)  # tables exist but empty
    gt_path = make_ground_truth_csv({"txn_1": True})

    try:
        compute_metrics(db_path, gt_path)
        assert False, "expected ValueError for empty decisions table"
    except ValueError as e:
        assert "no rows found" in str(e).lower()

    os.remove(db_path)
    os.remove(gt_path)


def test_unmatched_payment_id_raises_clear_error():
    payments = [default_payment("txn_1", 100.0)]
    decisions = [default_decision("txn_1", "retry_now", "recovered")]
    db_path = make_test_db(payments, decisions)
    gt_path = make_ground_truth_csv({"txn_DIFFERENT_ID": True})  # doesn't match txn_1

    try:
        compute_metrics(db_path, gt_path)
        assert False, "expected ValueError for unmatched payment_id"
    except ValueError as e:
        assert "not found in the ground-truth file" in str(e)

    os.remove(db_path)
    os.remove(gt_path)


def test_wrong_ground_truth_file_raises_clear_error():
    payments = [default_payment("txn_1", 100.0)]
    decisions = [default_decision("txn_1", "retry_now", "recovered")]
    db_path = make_test_db(payments, decisions)

    # a CSV without the _ground_truth_recoverable column at all
    bad_gt_path = tempfile.mktemp(suffix=".csv")
    with open(bad_gt_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "amount"])
        writer.writerow(["txn_1", "100.0"])

    try:
        compute_metrics(db_path, bad_gt_path)
        assert False, "expected ValueError for missing ground truth column"
    except ValueError as e:
        assert "_ground_truth_recoverable" in str(e)

    os.remove(db_path)
    os.remove(bad_gt_path)


# ---------------------------------------------------------------------------
# Test runner
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
