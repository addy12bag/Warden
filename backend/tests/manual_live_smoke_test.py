"""
Manual live smoke test for decision_agent.py against the REAL Groq API.

This is intentionally NOT part of the automated pytest suite (test_decision_agent.py
mocks everything and requires no network/API key). Run this file directly,
by hand, once you have a real GROQ_API_KEY set, to confirm actual
end-to-end connectivity and see real model output before trusting it
against your full batch.

Usage:
    cd backend
    cp .env.example .env   # fill in your real GROQ_API_KEY first
    export $(cat .env | xargs)   # or use python-dotenv / your shell's method
    python tests/manual_live_smoke_test.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.decision_agent import decide_action

SAMPLE_CASES = [
    {
        "name": "Clear-cut transient failure, first attempt",
        "classification": {
            "root_cause": "gateway_timeout",
            "error_category": "transient",
            "is_retryable": True,
            "recommended_primary_action": "retry_now",
            "reasoning": "error_code='gateway_timeout' maps to category='transient'.",
        },
        "payment": {
            "id": "txn_smoke_1",
            "amount": 799.0,
            "payment_method": "card",
            "retry_count": 0,
            "customer_tenure_days": 45,
            "language_pref": "en",
        },
    },
    {
        "name": "Hard decline -- expired card, must NOT retry",
        "classification": {
            "root_cause": "expired_card",
            "error_category": "hard_decline",
            "is_retryable": False,
            "recommended_primary_action": "prompt_method_switch",
            "reasoning": "HARD_DECLINE_FLOOR: category 'hard_decline' is never retryable.",
        },
        "payment": {
            "id": "txn_smoke_2",
            "amount": 2500.0,
            "payment_method": "card",
            "retry_count": 0,
            "customer_tenure_days": 800,
            "language_pref": "hi-en",
        },
    },
    {
        "name": "Soft decline already escalated by classifier (retry_count=2)",
        "classification": {
            "root_cause": "insufficient_funds",
            "error_category": "soft_decline",
            "is_retryable": True,
            "recommended_primary_action": "escalate",
            "reasoning": "SOFT_DECLINE_ESCALATION_THRESHOLD triggered.",
        },
        "payment": {
            "id": "txn_smoke_3",
            "amount": 15000.0,
            "payment_method": "upi",
            "retry_count": 2,
            "customer_tenure_days": 200,
            "language_pref": "en",
        },
    },
]


def main():
    print("Running live Groq API smoke test...\n")
    print(f"{'Case':<55} {'Chosen action':<25} {'Fallback?'}")
    print("-" * 95)

    for case in SAMPLE_CASES:
        result = decide_action(case["classification"], case["payment"])
        fallback_flag = "YES (check reasoning!)" if result.fallback_used else "no"
        print(f"{case['name']:<55} {result.chosen_action:<25} {fallback_flag}")
        print(f"   reasoning: {result.reasoning}\n")

    print("\nSanity checks to eyeball:")
    print("  - Case 2 (expired card) should NEVER return retry_now or retry_delayed.")
    print("  - Case 3 (already escalated) should generally lean toward escalate,")
    print("    though the LLM has some latitude here -- if it strongly disagrees,")
    print("    review the reasoning before trusting it, since Stage 3's stopping")
    print("    rules will override an unsafe choice regardless.")


if __name__ == "__main__":
    main()
