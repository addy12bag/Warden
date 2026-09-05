"""
SQLite schema definitions -- matches PROJECT.md section 6 exactly.

Two tables:
    payments  -- the synthetic batch (agent-facing, loaded from
                 backend/data/payments.csv, NEVER includes ground truth)
    decisions -- one row per agent decision, written by
                 backend/core/executor.py -- this table IS the audit trail

The ground-truth file (payments_ground_truth.csv) is intentionally
NOT loaded into this database during the decision phase. It is only
joined in during Stage 7 (evaluation), read directly from CSV, to
guarantee the agent never had access to it.
"""

CREATE_PAYMENTS_TABLE = """
CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    customer_name TEXT,
    amount REAL NOT NULL,
    payment_method TEXT NOT NULL,
    error_code TEXT NOT NULL,
    error_category TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    customer_tenure_days INTEGER,
    language_pref TEXT NOT NULL DEFAULT 'en'
);
"""

CREATE_DECISIONS_TABLE = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id TEXT NOT NULL REFERENCES payments(id),
    root_cause TEXT NOT NULL,
    is_retryable INTEGER NOT NULL,        -- 0/1 boolean
    chosen_action TEXT NOT NULL,
    reasoning TEXT,
    message_sent TEXT,
    stopping_rule_triggered TEXT,
    outcome TEXT NOT NULL,
    timestamp TEXT NOT NULL
);
"""

ALL_SCHEMAS = [CREATE_PAYMENTS_TABLE, CREATE_DECISIONS_TABLE]
