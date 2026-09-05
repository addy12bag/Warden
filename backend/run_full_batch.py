"""
Stage 6 -- run the full agent pipeline against the real synthetic batch.

This is the actual "do the thing" script: loads payments.csv into the
database, runs every payment through classify -> decide -> gate ->
message -> execute -> log, and reports a quick summary. This is what
produces the real `decisions` table that Stage 7's evaluation and
Stage 8's dashboard will read from.

NOTE: this makes real Groq + Gemini API calls, one pair per payment
that needs a decision (and a message, where applicable). Running this
against the full 500-record batch will make ~500-1000+ live API calls.
Check your API quotas before running the full batch -- see
manual_live_smoke_test.py / manual_live_smoke_test_messaging.py notes
on quota limits discovered during Stage 5 development. Start with
--limit on a small number first to confirm everything works before
committing your daily quota to a full run.

ENCODING NOTE: this script forces UTF-8 mode at the very top (before
any other imports) to avoid a real bug found during live testing: on
some machines/shells, Python's default text encoding falls back to
ASCII rather than UTF-8 (commonly on macOS when stdout isn't attached
to a fully-configured interactive terminal, or certain LANG/LC_ALL
locale settings). Since payment amounts render with a rupee symbol
(u20b9) and Hinglish messages contain non-ASCII characters, an
ASCII-defaulting environment causes every single LLM call in the batch
to fail with a UnicodeEncodeError, which the fail-safe fallback logic
correctly catches -- but silently masks by escalating every payment,
producing a misleadingly uniform 100% escalated result rather than
real agent decisions. Forcing UTF-8 here fixes the root cause rather
than just observing the fail-safe absorb it.

Usage:
    cd backend
    python run_full_batch.py --limit 10          # small test run first
    python run_full_batch.py                     # full batch (all rows)
    python run_full_batch.py --fresh              # wipe existing decisions first
"""

import os
import sys

# Must happen before any other imports that might touch stdout/stderr
# encoding or make network calls. See ENCODING NOTE above.
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import argparse
import csv
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.executor import run_batch
from db.db import init_db, load_payments_csv, get_connection, DB_PATH


def main():
    parser = argparse.ArgumentParser(description="Run the full recovery agent pipeline")
    parser.add_argument("--payments-csv", default="data/payments.csv")
    parser.add_argument("--limit", type=int, default=None, help="Process only the first N payments (for testing)")
    parser.add_argument("--fresh", action="store_true", help="Clear existing decisions table before running")
    args = parser.parse_args()

    print(f"Initializing database at {DB_PATH}...")
    init_db()

    if args.fresh:
        conn = get_connection()
        conn.execute("DELETE FROM decisions;")
        conn.commit()
        conn.close()
        print("Cleared existing decisions table.")

    print(f"Loading payments from {args.payments_csv}...")
    count = load_payments_csv(args.payments_csv, replace=True)
    print(f"Loaded {count} payments into the database.")

    with open(args.payments_csv, newline="") as f:
        reader = csv.DictReader(f)
        payments = list(reader)

    if args.limit:
        payments = payments[:args.limit]
        print(f"Limiting run to first {len(payments)} payments (--limit {args.limit}).")

    print(f"\nRunning full pipeline on {len(payments)} payments...")
    print("(this makes real Groq + Gemini API calls -- may take a while for large batches)\n")

    start = time.time()
    records = run_batch(payments)
    elapsed = time.time() - start

    print(f"\nDone in {elapsed:.1f}s ({len(records)} payments processed, "
          f"{elapsed / max(len(records), 1):.2f}s/payment average)\n")

    # Quick summary
    from collections import Counter
    outcome_counts = Counter(r.outcome for r in records)
    action_counts = Counter(r.chosen_action for r in records)
    stopping_rule_counts = Counter(r.stopping_rule_triggered for r in records if r.stopping_rule_triggered)

    print("Outcome distribution:")
    for outcome, cnt in outcome_counts.most_common():
        print(f"  {outcome:20s} {cnt:4d}  ({100*cnt/len(records):.1f}%)")

    print("\nFinal action distribution:")
    for action, cnt in action_counts.most_common():
        print(f"  {action:20s} {cnt:4d}  ({100*cnt/len(records):.1f}%)")

    if stopping_rule_counts:
        print("\nStopping rules triggered:")
        for rule, cnt in stopping_rule_counts.most_common():
            print(f"  {rule:30s} {cnt:4d}")

    print(f"\nFull audit trail written to `decisions` table in {DB_PATH}")
    print("Run Stage 7's evaluate.py next to compute recovery rate against ground truth.")


if __name__ == "__main__":
    main()
