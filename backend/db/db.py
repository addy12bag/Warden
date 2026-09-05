"""
SQLite connection and setup helper.
"""

import csv
import sqlite3
from pathlib import Path

from db.models import ALL_SCHEMAS

DB_PATH = Path(__file__).parent / "recovery_agent.db"

PAYMENTS_CSV_COLUMNS = [
    "id", "customer_id", "customer_name", "amount", "payment_method",
    "error_code", "error_category", "retry_count", "created_at",
    "customer_tenure_days", "language_pref",
]


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """
    Open a connection to the SQLite database, creating the file if it
    doesn't exist yet. Row factory is set to sqlite3.Row so callers can
    access columns by name (row["payment_id"]) rather than only by
    index, which matters once decisions rows have 9+ columns.
    """
    path = db_path or DB_PATH
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")  # enforce the payments FK on decisions
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    """Create the payments and decisions tables if they don't already exist."""
    conn = get_connection(db_path)
    try:
        for schema in ALL_SCHEMAS:
            conn.execute(schema)
        conn.commit()
    finally:
        conn.close()


def load_payments_csv(csv_path: str, db_path: Path | str | None = None, replace: bool = False) -> int:
    """
    Bulk-load backend/data/payments.csv into the `payments` table.

    Args:
        csv_path: path to the agent-facing payments.csv (NEVER the
                  ground-truth file -- there is no ground-truth column
                  in PAYMENTS_CSV_COLUMNS, so passing the wrong file
                  would fail loudly on unexpected columns rather than
                  silently leaking labels, but pass the right file).
        db_path: optional override, defaults to DB_PATH.
        replace: if True, clears existing rows in `payments` first
                 (useful for repeated local testing with a fresh batch).

    Returns:
        Number of rows loaded.
    """
    init_db(db_path)  # ensure tables exist before inserting
    conn = get_connection(db_path)
    try:
        if replace:
            conn.execute("DELETE FROM payments;")

        with open(csv_path, newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        missing_cols = set(PAYMENTS_CSV_COLUMNS) - set(reader.fieldnames or [])
        if missing_cols:
            raise ValueError(
                f"payments CSV at {csv_path} is missing expected columns: {missing_cols}. "
                f"Did you accidentally point this at payments_ground_truth.csv or a "
                f"differently-shaped file?"
            )

        placeholders = ", ".join("?" for _ in PAYMENTS_CSV_COLUMNS)
        col_list = ", ".join(PAYMENTS_CSV_COLUMNS)
        insert_sql = f"INSERT OR REPLACE INTO payments ({col_list}) VALUES ({placeholders})"

        for row in rows:
            values = [row[col] for col in PAYMENTS_CSV_COLUMNS]
            conn.execute(insert_sql, values)

        conn.commit()
        return len(rows)
    finally:
        conn.close()
