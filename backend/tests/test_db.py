"""
Unit tests for backend/db/db.py.

Uses temporary SQLite files, never the real recovery_agent.db.

Run with:
    cd backend && python -m pytest tests/test_db.py -v
"""

import sys
import os
import tempfile
import csv
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.db import init_db, load_payments_csv, get_connection, PAYMENTS_CSV_COLUMNS


def temp_db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    return path


def temp_csv(rows: list[dict]) -> str:
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=PAYMENTS_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def make_row(payment_id="txn_1"):
    return {
        "id": payment_id, "customer_id": "cust_1", "customer_name": "Test",
        "amount": "500.0", "payment_method": "card", "error_code": "gateway_timeout",
        "error_category": "transient", "retry_count": "0", "created_at": "2026-08-25T00:00:00",
        "customer_tenure_days": "100", "language_pref": "en",
    }


# ---------------------------------------------------------------------------
# 1. init_db -- creates both tables
# ---------------------------------------------------------------------------

def test_init_db_creates_both_tables():
    db_path = temp_db_path()
    init_db(db_path)

    conn = get_connection(db_path)
    tables = {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()

    assert "payments" in tables
    assert "decisions" in tables
    os.remove(db_path)


def test_init_db_is_idempotent():
    db_path = temp_db_path()
    init_db(db_path)
    init_db(db_path)  # should not raise on second call
    init_db(db_path)
    os.remove(db_path)


# ---------------------------------------------------------------------------
# 2. load_payments_csv -- happy path
# ---------------------------------------------------------------------------

def test_load_payments_csv_loads_all_rows():
    db_path = temp_db_path()
    csv_path = temp_csv([make_row("txn_1"), make_row("txn_2"), make_row("txn_3")])

    count = load_payments_csv(csv_path, db_path)

    assert count == 3
    conn = get_connection(db_path)
    db_count = conn.execute("SELECT COUNT(*) as c FROM payments").fetchone()["c"]
    conn.close()
    assert db_count == 3
    os.remove(db_path)
    os.remove(csv_path)


def test_load_payments_csv_preserves_field_values():
    db_path = temp_db_path()
    row = make_row("txn_specific")
    row["amount"] = "1234.56"
    csv_path = temp_csv([row])

    load_payments_csv(csv_path, db_path)

    conn = get_connection(db_path)
    result = conn.execute("SELECT * FROM payments WHERE id = ?", ("txn_specific",)).fetchone()
    conn.close()

    assert result["amount"] == 1234.56
    assert result["error_code"] == "gateway_timeout"
    os.remove(db_path)
    os.remove(csv_path)


def test_load_payments_csv_creates_tables_if_missing():
    # Deliberately skip calling init_db first -- load_payments_csv
    # should handle that itself.
    db_path = temp_db_path()
    csv_path = temp_csv([make_row("txn_1")])

    count = load_payments_csv(csv_path, db_path)  # no init_db() call first

    assert count == 1
    os.remove(db_path)
    os.remove(csv_path)


# ---------------------------------------------------------------------------
# 3. load_payments_csv -- replace flag
# ---------------------------------------------------------------------------

def test_replace_true_clears_existing_rows_first():
    db_path = temp_db_path()
    csv_path_1 = temp_csv([make_row("txn_old_1"), make_row("txn_old_2")])
    csv_path_2 = temp_csv([make_row("txn_new_1")])

    load_payments_csv(csv_path_1, db_path, replace=True)
    load_payments_csv(csv_path_2, db_path, replace=True)

    conn = get_connection(db_path)
    ids = {row["id"] for row in conn.execute("SELECT id FROM payments").fetchall()}
    conn.close()

    assert ids == {"txn_new_1"}  # old rows gone
    os.remove(db_path)
    os.remove(csv_path_1)
    os.remove(csv_path_2)


def test_replace_false_keeps_existing_rows():
    db_path = temp_db_path()
    csv_path_1 = temp_csv([make_row("txn_old_1")])
    csv_path_2 = temp_csv([make_row("txn_new_1")])

    load_payments_csv(csv_path_1, db_path, replace=False)
    load_payments_csv(csv_path_2, db_path, replace=False)

    conn = get_connection(db_path)
    ids = {row["id"] for row in conn.execute("SELECT id FROM payments").fetchall()}
    conn.close()

    assert ids == {"txn_old_1", "txn_new_1"}  # both present
    os.remove(db_path)
    os.remove(csv_path_1)
    os.remove(csv_path_2)


# ---------------------------------------------------------------------------
# 4. Wrong-file safety check -- catches accidentally loading the
#    ground-truth file instead of the agent-facing one
# ---------------------------------------------------------------------------

def test_missing_expected_columns_raises_clear_error():
    db_path = temp_db_path()
    fd, csv_path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "some_other_column"])
        writer.writeheader()
        writer.writerow({"id": "txn_1", "some_other_column": "x"})

    try:
        load_payments_csv(csv_path, db_path)
        assert False, "expected ValueError for missing columns"
    except ValueError as e:
        assert "missing expected columns" in str(e)
    finally:
        os.remove(db_path) if os.path.exists(db_path) else None
        os.remove(csv_path)


# ---------------------------------------------------------------------------
# 5. Foreign key enforcement
# ---------------------------------------------------------------------------

def test_foreign_keys_are_enforced():
    db_path = temp_db_path()
    init_db(db_path)

    conn = get_connection(db_path)
    try:
        conn.execute(
            """INSERT INTO decisions (payment_id, root_cause, is_retryable,
               chosen_action, outcome, timestamp)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("nonexistent_payment", "gateway_timeout", 1, "retry_now", "recovered", "2026-08-25T00:00:00"),
        )
        conn.commit()
        assert False, "expected IntegrityError for FK violation"
    except Exception as e:
        assert "FOREIGN KEY" in str(e) or "IntegrityError" in str(type(e).__name__)
    finally:
        conn.close()
        os.remove(db_path)


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
