"""
Unit tests for backend/main.py (Stage 8 API layer).

Uses a temporary SQLite DB with hand-crafted data (not the real
recovery_agent.db) so these tests are self-contained and don't depend
on a real batch run having happened first.

Run with:
    cd backend && python -m pytest tests/test_main.py -v
"""

import sys
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient

from db.db import init_db, get_connection


def make_test_db_with_data():
    db_path = tempfile.mktemp(suffix=".db")
    init_db(db_path)
    conn = get_connection(db_path)

    payments = [
        ("txn_1", "cust_1", "Alice", 500.0, "card", "gateway_timeout", "transient", 0, "2026-08-01T00:00:00", 100, "en"),
        ("txn_2", "cust_2", "Bob", 1000.0, "card", "expired_card", "hard_decline", 0, "2026-08-01T01:00:00", 500, "en"),
        ("txn_3", "cust_3", "Chen", 250.0, "upi", "insufficient_funds", "soft_decline", 1, "2026-08-01T02:00:00", 50, "hi-en"),
    ]
    for p in payments:
        conn.execute(
            """INSERT INTO payments (id, customer_id, customer_name, amount,
               payment_method, error_code, error_category, retry_count,
               created_at, customer_tenure_days, language_pref)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            p,
        )

    decisions = [
        ("txn_1", "gateway_timeout", 1, "retry_now", "test reasoning 1", None, None, "recovered", "2026-08-01T00:30:00"),
        ("txn_2", "expired_card", 0, "prompt_method_switch", "test reasoning 2", "Please update your card.", None, "still_failing", "2026-08-01T01:15:00"),
        ("txn_3", "insufficient_funds", 1, "escalate", "test reasoning 3", None, "AUTO_ESCALATE_THRESHOLD", "escalated", "2026-08-01T02:20:00"),
    ]
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


def make_test_ground_truth_csv():
    import csv
    path = tempfile.mktemp(suffix=".csv")
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "_ground_truth_recoverable"])
        writer.writerow(["txn_1", "True"])
        writer.writerow(["txn_2", "False"])
        writer.writerow(["txn_3", "True"])
    return path


def get_client(db_path, gt_path):
    """
    Returns a TestClient with main.DB_PATH and main.GROUND_TRUTH_PATH
    patched to point at the given test files, so tests never touch the
    real recovery_agent.db.
    """
    import main
    patcher1 = patch.object(main, "DB_PATH", Path(db_path))
    patcher2 = patch.object(main, "GROUND_TRUTH_PATH", Path(gt_path))
    patcher1.start()
    patcher2.start()
    client = TestClient(main.app)
    return client, patcher1, patcher2


# ---------------------------------------------------------------------------
# 1. Health check
# ---------------------------------------------------------------------------

def test_health_reports_database_initialized_true_when_db_exists():
    db_path = make_test_db_with_data()
    gt_path = make_test_ground_truth_csv()
    client, p1, p2 = get_client(db_path, gt_path)

    r = client.get("/api/health")

    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["database_initialized"] is True

    p1.stop(); p2.stop()
    os.remove(db_path); os.remove(gt_path)


def test_health_reports_database_initialized_false_when_missing():
    fake_path = tempfile.mktemp(suffix=".db")  # never created
    gt_path = make_test_ground_truth_csv()
    client, p1, p2 = get_client(fake_path, gt_path)

    r = client.get("/api/health")

    assert r.status_code == 200
    assert r.json()["database_initialized"] is False

    p1.stop(); p2.stop()
    os.remove(gt_path)


# ---------------------------------------------------------------------------
# 2. Metrics endpoint
# ---------------------------------------------------------------------------

def test_metrics_returns_correct_shape_and_values():
    db_path = make_test_db_with_data()
    gt_path = make_test_ground_truth_csv()
    client, p1, p2 = get_client(db_path, gt_path)

    r = client.get("/api/metrics")

    assert r.status_code == 200
    body = r.json()
    assert body["total_decisions"] == 3
    assert body["recovered_count"] == 1
    assert body["false_retry_count"] == 0  # nothing was retried against unrecoverable
    assert "time_to_decision_note" in body
    assert "recovery_rate_by_action" in body

    p1.stop(); p2.stop()
    os.remove(db_path); os.remove(gt_path)


def test_metrics_404s_cleanly_when_no_database():
    fake_path = tempfile.mktemp(suffix=".db")
    gt_path = make_test_ground_truth_csv()
    client, p1, p2 = get_client(fake_path, gt_path)

    r = client.get("/api/metrics")

    assert r.status_code == 404
    assert "run_full_batch.py" in r.json()["detail"]

    p1.stop(); p2.stop()
    os.remove(gt_path)


# ---------------------------------------------------------------------------
# 3. Decisions list endpoint
# ---------------------------------------------------------------------------

def test_list_decisions_returns_all_rows_with_expected_fields():
    db_path = make_test_db_with_data()
    gt_path = make_test_ground_truth_csv()
    client, p1, p2 = get_client(db_path, gt_path)

    r = client.get("/api/decisions")

    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 3
    for field in ("payment_id", "root_cause", "chosen_action", "outcome", "amount", "reasoning"):
        assert field in rows[0]

    p1.stop(); p2.stop()
    os.remove(db_path); os.remove(gt_path)


def test_list_decisions_404s_when_no_database():
    fake_path = tempfile.mktemp(suffix=".db")
    gt_path = make_test_ground_truth_csv()
    client, p1, p2 = get_client(fake_path, gt_path)

    r = client.get("/api/decisions")

    assert r.status_code == 404

    p1.stop(); p2.stop()
    os.remove(gt_path)


# ---------------------------------------------------------------------------
# 4. Single decision endpoint
# ---------------------------------------------------------------------------

def test_get_single_decision_returns_full_audit_trail():
    db_path = make_test_db_with_data()
    gt_path = make_test_ground_truth_csv()
    client, p1, p2 = get_client(db_path, gt_path)

    r = client.get("/api/decisions/txn_2")

    assert r.status_code == 200
    body = r.json()
    assert body["payment_id"] == "txn_2"
    assert body["chosen_action"] == "prompt_method_switch"
    assert body["message_sent"] == "Please update your card."
    assert body["customer_name"] == "Bob"
    assert body["error_code"] == "expired_card"

    p1.stop(); p2.stop()
    os.remove(db_path); os.remove(gt_path)


def test_get_single_decision_404s_for_unknown_payment_id():
    db_path = make_test_db_with_data()
    gt_path = make_test_ground_truth_csv()
    client, p1, p2 = get_client(db_path, gt_path)

    r = client.get("/api/decisions/does_not_exist")

    assert r.status_code == 404
    assert "does_not_exist" in r.json()["detail"]

    p1.stop(); p2.stop()
    os.remove(db_path); os.remove(gt_path)


# ---------------------------------------------------------------------------
# 5. Exceptions endpoint
# ---------------------------------------------------------------------------

def test_exceptions_returns_only_still_failing_and_pending():
    db_path = make_test_db_with_data()
    gt_path = make_test_ground_truth_csv()
    client, p1, p2 = get_client(db_path, gt_path)

    r = client.get("/api/exceptions")

    assert r.status_code == 200
    rows = r.json()
    ids = {row["payment_id"] for row in rows}
    assert ids == {"txn_2"}  # only txn_2 is still_failing; txn_1 recovered, txn_3 escalated
    assert "txn_1" not in ids
    assert "txn_3" not in ids  # escalated is not the same as "unresolved exception"

    p1.stop(); p2.stop()
    os.remove(db_path); os.remove(gt_path)


def test_exceptions_ordered_by_amount_descending():
    db_path = tempfile.mktemp(suffix=".db")
    init_db(db_path)
    conn = get_connection(db_path)
    conn.execute("""INSERT INTO payments VALUES ('txn_a','c1','A',100.0,'card','gateway_timeout','transient',0,'2026-08-01T00:00:00',10,'en')""")
    conn.execute("""INSERT INTO payments VALUES ('txn_b','c2','B',999.0,'card','gateway_timeout','transient',0,'2026-08-01T00:00:00',10,'en')""")
    conn.execute("""INSERT INTO decisions (payment_id, root_cause, is_retryable, chosen_action, outcome, timestamp) VALUES ('txn_a','gateway_timeout',1,'retry_now','still_failing','2026-08-01T00:30:00')""")
    conn.execute("""INSERT INTO decisions (payment_id, root_cause, is_retryable, chosen_action, outcome, timestamp) VALUES ('txn_b','gateway_timeout',1,'retry_now','still_failing','2026-08-01T00:30:00')""")
    conn.commit()
    conn.close()

    gt_path = make_test_ground_truth_csv()
    client, p1, p2 = get_client(db_path, gt_path)

    r = client.get("/api/exceptions")
    rows = r.json()

    assert rows[0]["payment_id"] == "txn_b"  # higher amount first
    assert rows[1]["payment_id"] == "txn_a"

    p1.stop(); p2.stop()
    os.remove(db_path); os.remove(gt_path)


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
