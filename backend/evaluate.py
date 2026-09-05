"""
Stage 7 -- Evaluation pass.

Joins the `decisions` table (agent's actual choices + outcomes)
against backend/data/payments_ground_truth.csv (hidden recoverability
label, never seen by the agent) to compute honest, measured results.

This is the ONLY script permitted to read payments_ground_truth.csv.
No module under backend/core/ should ever import it.

METRIC DEFINITIONS (worth being precise about, since these numbers are
the headline evidence for the whole project):

    Recovery rate %:
        recovered / total decisions, reported overall AND broken down
        by final chosen_action -- a bare overall percentage hides
        whether recovery is concentrated in "easy" actions or spread
        reasonably across the action mix.

    Money recovered:
        sum(amount) where outcome == "recovered", reported alongside
        total batch amount and money still at risk (still_failing +
        escalated), so the headline figure has honest context rather
        than floating alone with no denominator.

    Time-to-recovery:
        IMPORTANT HONESTY NOTE: this is a SIMULATED batch with no real
        elapsed wall-clock recovery process -- there is no actual
        "money arrived" timestamp distinct from when the decision was
        logged. Reporting a fabricated "time to recover money" would
        be dishonest. What IS genuinely measurable and reported here
        instead is "time from payment failure (created_at) to agent
        decision (decisions.timestamp)" -- i.e. how fast the agent
        reacts, not how fast money actually lands. This distinction is
        stated explicitly in the output so it's never misquoted as a
        real-world recovery duration in a pitch or write-up.

    False-retry rate:
        % of decisions where chosen_action is a retry action
        (retry_now/retry_delayed) AND ground truth marks the payment
        NOT recoverable. This is the most important integrity check in
        this whole evaluation -- it should be at or near 0% if
        Stages 2-3's safety design (HARD_DECLINE_FLOOR, stopping
        rules) are doing their job. If this is not near-zero, that is
        a genuine finding to report honestly, not a bug to hide from
        the output.

    Exceptions list:
        every payment where outcome is "still_failing" or "pending",
        with root cause and chosen action included -- reported
        honestly and completely, not cherry-picked, per the project's
        explicit design principle (see PROJECT.md section 9).

Usage:
    cd backend
    python evaluate.py
    python evaluate.py --decisions-db db/recovery_agent.db \\
                        --ground-truth data/payments_ground_truth.csv
"""

import argparse
import csv
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime


RETRY_ACTIONS = {"retry_now", "retry_delayed"}


@dataclass
class EvaluationResult:
    total_decisions: int
    total_batch_amount: float

    recovered_count: int
    recovered_amount: float
    recovery_rate_pct: float

    still_at_risk_amount: float  # still_failing + escalated

    recovery_rate_by_action: dict = field(default_factory=dict)

    avg_time_to_decision_hours: float | None = None
    time_to_decision_note: str = ""

    false_retry_count: int = 0
    false_retry_rate_pct: float = 0.0
    false_retry_examples: list = field(default_factory=list)

    exceptions: list = field(default_factory=list)

    outcome_breakdown: dict = field(default_factory=dict)


def _load_ground_truth(path: str) -> dict:
    """Returns {payment_id: bool_recoverable}."""
    ground_truth = {}
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        if "_ground_truth_recoverable" not in (reader.fieldnames or []):
            raise ValueError(
                f"{path} is missing the '_ground_truth_recoverable' column -- "
                f"is this actually the ground-truth file, not payments.csv?"
            )
        for row in reader:
            ground_truth[row["id"]] = row["_ground_truth_recoverable"].strip().lower() == "true"
    return ground_truth


def _load_decisions(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT d.*, p.amount, p.created_at, p.error_code
            FROM decisions d
            JOIN payments p ON d.payment_id = p.id
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def compute_metrics(decisions_db_path: str, ground_truth_csv_path: str) -> EvaluationResult:
    """
    Join decisions (joined with payments for amount/created_at) against
    the hidden ground-truth recoverability label, and compute all
    Stage 7 metrics.
    """
    ground_truth = _load_ground_truth(ground_truth_csv_path)
    decisions = _load_decisions(decisions_db_path)

    if not decisions:
        raise ValueError(
            f"No rows found in the decisions table at {decisions_db_path}. "
            f"Run run_full_batch.py first to populate it."
        )

    unmatched = [d["payment_id"] for d in decisions if d["payment_id"] not in ground_truth]
    if unmatched:
        raise ValueError(
            f"{len(unmatched)} decision(s) reference payment_id(s) not found in "
            f"the ground-truth file (e.g. {unmatched[:3]}). This should not "
            f"happen if both files came from the same generate_synthetic.py run "
            f"with the same --seed -- check you're comparing matching datasets."
        )

    total_batch_amount = sum(d["amount"] for d in decisions)

    recovered = [d for d in decisions if d["outcome"] == "recovered"]
    recovered_amount = sum(d["amount"] for d in recovered)
    recovery_rate_pct = 100 * len(recovered) / len(decisions)

    still_at_risk = [d for d in decisions if d["outcome"] in ("still_failing", "escalated")]
    still_at_risk_amount = sum(d["amount"] for d in still_at_risk)

    # Recovery rate broken down by final chosen_action
    actions = sorted(set(d["chosen_action"] for d in decisions))
    recovery_rate_by_action = {}
    for action in actions:
        action_decisions = [d for d in decisions if d["chosen_action"] == action]
        action_recovered = [d for d in action_decisions if d["outcome"] == "recovered"]
        recovery_rate_by_action[action] = {
            "count": len(action_decisions),
            "recovered": len(action_recovered),
            "recovery_rate_pct": 100 * len(action_recovered) / len(action_decisions) if action_decisions else 0.0,
        }

    # Time-to-decision (NOT time-to-actual-recovery -- see honesty note in docstring)
    deltas_hours = []
    for d in decisions:
        try:
            created = datetime.fromisoformat(d["created_at"])
            decided = datetime.fromisoformat(d["timestamp"])
            deltas_hours.append((decided - created).total_seconds() / 3600)
        except (ValueError, TypeError, KeyError):
            continue  # skip unparseable timestamps rather than crash the whole eval
    avg_time_to_decision_hours = sum(deltas_hours) / len(deltas_hours) if deltas_hours else None

    # False-retry rate: retried something ground truth says is NOT recoverable
    retry_decisions = [d for d in decisions if d["chosen_action"] in RETRY_ACTIONS]
    false_retries = [d for d in retry_decisions if not ground_truth.get(d["payment_id"], True)]
    false_retry_rate_pct = (
        100 * len(false_retries) / len(retry_decisions) if retry_decisions else 0.0
    )
    false_retry_examples = [
        {
            "payment_id": d["payment_id"],
            "error_code": d["error_code"],
            "chosen_action": d["chosen_action"],
            "root_cause": d["root_cause"],
        }
        for d in false_retries[:10]  # cap examples shown, but false_retry_count below is exact
    ]

    # Exceptions -- every unresolved case, honestly, not cherry-picked
    exceptions = [
        {
            "payment_id": d["payment_id"],
            "error_code": d["error_code"],
            "root_cause": d["root_cause"],
            "chosen_action": d["chosen_action"],
            "outcome": d["outcome"],
            "amount": d["amount"],
            "stopping_rule_triggered": d["stopping_rule_triggered"],
        }
        for d in decisions
        if d["outcome"] in ("still_failing", "pending")
    ]

    outcome_breakdown = {}
    for outcome in sorted(set(d["outcome"] for d in decisions)):
        outcome_breakdown[outcome] = sum(1 for d in decisions if d["outcome"] == outcome)

    return EvaluationResult(
        total_decisions=len(decisions),
        total_batch_amount=total_batch_amount,
        recovered_count=len(recovered),
        recovered_amount=recovered_amount,
        recovery_rate_pct=recovery_rate_pct,
        still_at_risk_amount=still_at_risk_amount,
        recovery_rate_by_action=recovery_rate_by_action,
        avg_time_to_decision_hours=avg_time_to_decision_hours,
        time_to_decision_note=(
            "This is time from payment failure (created_at) to agent decision "
            "(decisions.timestamp) -- NOT actual time-to-money-recovered. This "
            "is a simulated batch with no real elapsed recovery process; "
            "reporting a fabricated recovery duration would be dishonest. This "
            "metric instead honestly reflects how fast the agent reacts."
        ),
        false_retry_count=len(false_retries),
        false_retry_rate_pct=false_retry_rate_pct,
        false_retry_examples=false_retry_examples,
        exceptions=exceptions,
        outcome_breakdown=outcome_breakdown,
    )


def print_report(result: EvaluationResult):
    print("=" * 70)
    print("EVALUATION REPORT")
    print("=" * 70)

    print(f"\nTotal decisions evaluated: {result.total_decisions}")
    print(f"Total batch amount:        \u20b9{result.total_batch_amount:,.2f}")

    print(f"\n--- Recovery ---")
    print(f"Recovered:        {result.recovered_count} ({result.recovery_rate_pct:.1f}%)")
    print(f"Money recovered:  \u20b9{result.recovered_amount:,.2f}")
    print(f"Money still at risk (still_failing + escalated): \u20b9{result.still_at_risk_amount:,.2f}")

    print(f"\n--- Outcome breakdown ---")
    for outcome, count in result.outcome_breakdown.items():
        print(f"  {outcome:20s} {count:4d}  ({100*count/result.total_decisions:.1f}%)")

    print(f"\n--- Recovery rate by chosen action ---")
    for action, stats in result.recovery_rate_by_action.items():
        print(f"  {action:22s} {stats['recovered']:4d}/{stats['count']:<4d}  ({stats['recovery_rate_pct']:.1f}%)")

    print(f"\n--- Time to decision ---")
    if result.avg_time_to_decision_hours is not None:
        print(f"Average: {result.avg_time_to_decision_hours:.2f} hours")
    else:
        print("Could not compute (no parseable timestamps found)")
    print(f"NOTE: {result.time_to_decision_note}")

    print(f"\n--- False-retry rate (integrity check) ---")
    print(f"{result.false_retry_count} false retries out of retry-actioned decisions "
          f"({result.false_retry_rate_pct:.2f}%)")
    if result.false_retry_examples:
        print("Examples (ground truth says NOT recoverable, but a retry was chosen):")
        for ex in result.false_retry_examples:
            print(f"  {ex['payment_id']}: error_code={ex['error_code']}, "
                  f"action={ex['chosen_action']}, root_cause={ex['root_cause']}")
    else:
        print("None -- the classifier + stopping rules correctly avoided retrying "
              "any ground-truth-unrecoverable payment in this batch.")

    print(f"\n--- Exceptions (unresolved cases, reported honestly) ---")
    print(f"{len(result.exceptions)} payment(s) still unresolved after this batch run:")
    for ex in result.exceptions[:20]:  # cap console output; full list is in the returned object
        print(f"  {ex['payment_id']}: {ex['error_code']} -> {ex['chosen_action']} "
              f"-> {ex['outcome']} (\u20b9{ex['amount']:,.2f})")
    if len(result.exceptions) > 20:
        print(f"  ... and {len(result.exceptions) - 20} more (see full result object)")

    print("\n" + "=" * 70)


def main():
    parser = argparse.ArgumentParser(description="Evaluate agent decisions against hidden ground truth")
    parser.add_argument("--decisions-db", default="db/recovery_agent.db")
    parser.add_argument("--ground-truth", default="data/payments_ground_truth.csv")
    args = parser.parse_args()

    result = compute_metrics(args.decisions_db, args.ground_truth)
    print_report(result)


if __name__ == "__main__":
    main()
