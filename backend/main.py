"""
FastAPI application entrypoint.

Serves the dashboard (Stage 8) via a REST API backed by the
`payments` and `decisions` tables.

DESIGN NOTE: this API is READ-ONLY with respect to running the agent
pipeline. There is deliberately NO "run batch" endpoint that triggers
live Groq/Gemini API calls from a web request -- given the real quota
limits discovered during Stage 5 development, an accidental page
refresh or repeated request against such an endpoint could silently
burn API quota. Running the pipeline stays an explicit CLI action
(`python run_full_batch.py`); this API only reads what's already been
written to the database.

Endpoints:
    GET  /api/health                 -- basic health check
    GET  /api/metrics                -- recovery rate, money recovered, etc. (Stage 7 evaluation)
    GET  /api/decisions              -- list all agent decisions (joined with payment info)
    GET  /api/decisions/{payment_id} -- full audit trail for one payment
    GET  /api/exceptions             -- unresolved (still_failing/pending) cases only

Run locally:
    cd backend
    uvicorn main:app --reload

ENCODING NOTE: forces UTF-8 mode at the top, same reasoning as
run_full_batch.py -- this API serves payment amounts (rupee symbol)
and Hinglish message text, which can trigger encoding errors on
systems where Python's default text encoding isn't UTF-8. See
run_full_batch.py's module docstring for the full incident writeup.
"""

import os
import sys

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from db.db import DB_PATH
from evaluate import compute_metrics

app = FastAPI(title="AI Revenue Recovery Agent", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # Vite dev server
    allow_methods=["*"],
    allow_headers=["*"],
)

GROUND_TRUTH_PATH = Path(__file__).parent / "data" / "payments_ground_truth.csv"


def _get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _decision_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "payment_id": row["payment_id"],
        "root_cause": row["root_cause"],
        "is_retryable": bool(row["is_retryable"]),
        "chosen_action": row["chosen_action"],
        "reasoning": row["reasoning"],
        "message_sent": row["message_sent"],
        "stopping_rule_triggered": row["stopping_rule_triggered"],
        "outcome": row["outcome"],
        "timestamp": row["timestamp"],
        "amount": row["amount"],
        "payment_method": row["payment_method"],
        "customer_tenure_days": row["customer_tenure_days"],
        "language_pref": row["language_pref"],
        "created_at": row["created_at"],
    }


@app.get("/api/health")
def health():
    db_exists = DB_PATH.exists()
    return {"status": "ok", "database_initialized": db_exists}


@app.get("/api/metrics")
def get_metrics():
    """
    Runs the Stage 7 evaluation (decisions joined against hidden ground
    truth) and returns the result as JSON for the dashboard's top-line
    metrics strip.
    """
    if not DB_PATH.exists():
        raise HTTPException(
            status_code=404,
            detail="No database found. Run `python run_full_batch.py` first to generate decisions.",
        )
    try:
        result = compute_metrics(str(DB_PATH), str(GROUND_TRUTH_PATH))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "total_decisions": result.total_decisions,
        "total_batch_amount": result.total_batch_amount,
        "recovered_count": result.recovered_count,
        "recovered_amount": result.recovered_amount,
        "recovery_rate_pct": result.recovery_rate_pct,
        "still_at_risk_amount": result.still_at_risk_amount,
        "recovery_rate_by_action": result.recovery_rate_by_action,
        "avg_time_to_decision_hours": result.avg_time_to_decision_hours,
        "time_to_decision_note": result.time_to_decision_note,
        "false_retry_count": result.false_retry_count,
        "false_retry_rate_pct": result.false_retry_rate_pct,
        "false_retry_examples": result.false_retry_examples,
        "outcome_breakdown": result.outcome_breakdown,
        "exceptions_count": len(result.exceptions),
    }


@app.get("/api/decisions")
def list_decisions():
    """Returns every decision, joined with payment context, newest first."""
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="No database found. Run run_full_batch.py first.")

    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT d.*, p.amount, p.payment_method, p.customer_tenure_days,
                   p.language_pref, p.created_at
            FROM decisions d
            JOIN payments p ON d.payment_id = p.id
            ORDER BY d.timestamp DESC
            """
        ).fetchall()
        return [_decision_row_to_dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/decisions/{payment_id}")
def get_decision(payment_id: str):
    """Full audit trail for a single payment."""
    conn = _get_conn()
    try:
        row = conn.execute(
            """
            SELECT d.*, p.amount, p.payment_method, p.customer_tenure_days,
                   p.language_pref, p.created_at, p.customer_name, p.error_code
            FROM decisions d
            JOIN payments p ON d.payment_id = p.id
            WHERE d.payment_id = ?
            """,
            (payment_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"No decision found for payment_id '{payment_id}'")
        result = _decision_row_to_dict(row)
        result["customer_name"] = row["customer_name"]
        result["error_code"] = row["error_code"]
        return result
    finally:
        conn.close()


@app.get("/api/exceptions")
def list_exceptions():
    """Every still_failing/pending decision -- the honest, unresolved list."""
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="No database found. Run run_full_batch.py first.")

    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT d.*, p.amount, p.payment_method, p.customer_tenure_days,
                   p.language_pref, p.created_at
            FROM decisions d
            JOIN payments p ON d.payment_id = p.id
            WHERE d.outcome IN ('still_failing', 'pending')
            ORDER BY p.amount DESC
            """
        ).fetchall()
        return [_decision_row_to_dict(r) for r in rows]
    finally:
        conn.close()
