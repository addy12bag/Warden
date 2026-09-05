"""
Synthetic failed-payment dataset generator for the AI Revenue Recovery agent.

Grounded in real-world decline code research:
- Soft declines make up ~70-90% of all card-not-present failures and are
  generally recoverable (insufficient funds, gateway timeout, velocity limit,
  do-not-honor, generic risk flag).
- Hard declines are permanent and should never be blindly retried
  (expired card, stolen/lost card, closed account, restricted card, fraud block).
- Insufficient funds alone accounts for ~44% of all card declines.

This script generates a batch of realistic failed-payment records with a
HIDDEN ground-truth recoverability label. The label is only used later to
score your agent's decisions -- it must NOT be given to the classifier or
the LLM decision layer, otherwise you're evaluating on leaked information.

Usage:
    python generate_synthetic.py --n 500 --seed 42 --out payments.csv
"""

import argparse
import csv
import random
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta


# ---------------------------------------------------------------------------
# Root-cause taxonomy
# Each entry: (error_code, category, is_recoverable_ground_truth, weight)
# Weights approximate real-world decline distributions from industry data.
# ---------------------------------------------------------------------------

ERROR_TAXONOMY = [
    # code                  category            recoverable   weight
    ("insufficient_funds",  "soft_decline",      True,         28),
    ("gateway_timeout",     "transient",         True,         14),
    ("network_drop",        "transient",         True,         10),
    ("do_not_honor",        "soft_decline",      True,         12),
    ("velocity_limit",      "soft_decline",      True,         6),
    ("invalid_cvv",         "user_error",        True,         8),
    ("expired_card",        "hard_decline",      False,        10),
    ("card_stolen_lost",    "hard_decline",      False,        4),
    ("account_closed",      "hard_decline",      False,        3),
    ("restricted_card",     "hard_decline",      False,        3),
    ("risk_block",          "compliance_block",  False,        2),
]

PAYMENT_METHODS = ["card", "upi", "netbanking"]
LANGUAGE_PREFS = ["en", "hi-en"]  # hi-en = Hinglish

FIRST_NAMES = ["Aarav", "Vivaan", "Ishaan", "Diya", "Ananya", "Kabir", "Sara",
               "Reyansh", "Myra", "Vihaan", "Priya", "Rohan", "Anika", "Arjun",
               "Neha", "Karan", "Pooja", "Aditya", "Sneha", "Rahul"]


@dataclass
class SyntheticPayment:
    id: str
    customer_id: str
    customer_name: str
    amount: float
    payment_method: str
    error_code: str
    error_category: str
    retry_count: int
    created_at: str
    customer_tenure_days: int
    language_pref: str
    # HIDDEN ground truth -- do NOT feed to classifier/agent, evaluation only
    _ground_truth_recoverable: bool


def weighted_choice(rng: random.Random, taxonomy):
    total = sum(w for *_, w in taxonomy)
    r = rng.uniform(0, total)
    upto = 0
    for code, category, recoverable, weight in taxonomy:
        upto += weight
        if upto >= r:
            return code, category, recoverable
    return taxonomy[-1][:3]


def generate_batch(n: int, seed: int = 42) -> list[SyntheticPayment]:
    rng = random.Random(seed)
    records = []
    base_time = datetime(2026, 8, 1, 9, 0, 0)

    for i in range(1, n + 1):
        code, category, recoverable = weighted_choice(rng, ERROR_TAXONOMY)

        amount = round(rng.choice([
            rng.uniform(99, 999),      # small ticket
            rng.uniform(1000, 4999),   # mid ticket
            rng.uniform(5000, 25000),  # large ticket
        ]), 2)

        # Retry count correlates loosely with category:
        # transient/soft declines tend to have been retried a bit already,
        # hard declines should realistically have fewer prior retries
        # (system SHOULD have stopped, but we inject some noise/imperfection)
        if category in ("transient", "soft_decline", "user_error"):
            retry_count = rng.choices([0, 1, 2, 3], weights=[40, 30, 20, 10])[0]
        else:
            retry_count = rng.choices([0, 1, 2], weights=[70, 25, 5])[0]

        tenure = rng.choices(
            [rng.randint(0, 30), rng.randint(31, 365), rng.randint(366, 2000)],
            weights=[20, 45, 35]
        )[0]

        created_at = base_time + timedelta(
            minutes=rng.randint(0, 60 * 24 * 14)  # spread over 14 days
        )

        name = rng.choice(FIRST_NAMES)

        records.append(SyntheticPayment(
            id=f"txn_{i:05d}",
            customer_id=f"cust_{rng.randint(1000, 9999)}",
            customer_name=name,
            amount=amount,
            payment_method=rng.choices(
                PAYMENT_METHODS, weights=[55, 35, 10]
            )[0],
            error_code=code,
            error_category=category,
            retry_count=retry_count,
            created_at=created_at.isoformat(),
            customer_tenure_days=tenure,
            language_pref=rng.choices(LANGUAGE_PREFS, weights=[60, 40])[0],
            _ground_truth_recoverable=recoverable,
        ))

    return records


def write_csv(records: list[SyntheticPayment], path: str, include_ground_truth: bool):
    fields = [f for f in asdict(records[0]).keys()
              if include_ground_truth or not f.startswith("_")]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in records:
            row = asdict(r)
            if not include_ground_truth:
                row.pop("_ground_truth_recoverable", None)
            writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(description="Generate synthetic failed-payment dataset")
    parser.add_argument("--n", type=int, default=500, help="Number of records")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    parser.add_argument("--out", type=str, default="payments.csv", help="Output CSV path (agent-facing, no ground truth)")
    parser.add_argument("--eval-out", type=str, default="payments_ground_truth.csv", help="Eval CSV path (includes ground truth, keep separate)")
    args = parser.parse_args()

    records = generate_batch(args.n, args.seed)

    # Agent-facing file: NO ground truth column. This is what your
    # classifier + LLM decision layer should actually read.
    write_csv(records, args.out, include_ground_truth=False)

    # Evaluation file: includes ground truth. Only used AFTER the agent
    # has made its decisions, to score recovery accuracy.
    write_csv(records, args.eval_out, include_ground_truth=True)

    # Quick sanity summary
    from collections import Counter
    cat_counts = Counter(r.error_category for r in records)
    code_counts = Counter(r.error_code for r in records)
    recoverable_count = sum(1 for r in records if r._ground_truth_recoverable)

    print(f"Generated {len(records)} synthetic payment records")
    print(f"  Agent-facing file (no ground truth): {args.out}")
    print(f"  Evaluation file (with ground truth):  {args.eval_out}")
    print()
    print("Category distribution:")
    for cat, count in cat_counts.most_common():
        print(f"  {cat:20s} {count:4d}  ({100*count/len(records):.1f}%)")
    print()
    print("Error code distribution:")
    for code, count in code_counts.most_common():
        print(f"  {code:20s} {count:4d}  ({100*count/len(records):.1f}%)")
    print()
    print(f"Ground-truth recoverable: {recoverable_count} ({100*recoverable_count/len(records):.1f}%)")
    print(f"Ground-truth unrecoverable: {len(records)-recoverable_count} ({100*(len(records)-recoverable_count)/len(records):.1f}%)")


if __name__ == "__main__":
    main()
