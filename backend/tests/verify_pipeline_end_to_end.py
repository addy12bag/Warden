"""
Manual end-to-end verification script -- run this locally to confirm
Stages 2-5 genuinely work together against REAL Groq + Gemini APIs,
not mocks.

This is a one-off checking script, not part of the automated test
suite. Delete it once you're confident everything works, or keep it
around as a quick health check before demo day.

Usage:
    cd backend
    python tests/verify_pipeline_end_to_end.py
"""

import sys
import csv
from pathlib import Path
from dataclasses import asdict

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.classifier import classify_payment
from core.stopping_rules import check_stopping_rules
from core.decision_agent import decide_action
from core.messaging_agent import draft_message, ACTIONS_REQUIRING_MESSAGE


def main():
    csv_path = Path(__file__).parent.parent / "data" / "payments.csv"
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        payments = list(reader)[:5]  # small sample -- keeps API usage light

    print(f"Running end-to-end check on {len(payments)} real payments...\n")
    print(f"{'ID':<12} {'error_code':<20} {'classifier':<18} {'LLM chose':<18} {'gate?':<8} {'final':<18} {'message?'}")
    print("-" * 120)

    fallback_details = []  # (payment_id, stage, reason)

    for p in payments:
        classification = classify_payment(p)
        c_dict = asdict(classification)

        decision = decide_action(c_dict, p)
        if decision.fallback_used:
            fallback_details.append((p["id"], "decision_agent (Groq)", decision.reasoning))

        verdict = check_stopping_rules(
            p, error_category=classification.error_category,
            is_retryable=classification.is_retryable,
            proposed_action=decision.chosen_action,
            retry_count=int(p["retry_count"]),
        )
        final_action = decision.chosen_action if verdict.allowed else verdict.forced_action

        message_preview = "-"
        if final_action in ACTIONS_REQUIRING_MESSAGE:
            msg_result = draft_message(p, final_action, p.get("language_pref", "en"))
            if msg_result.fallback_used:
                fallback_details.append((p["id"], "messaging_agent (Gemini)", msg_result.fallback_reason))
            message_preview = msg_result.message[:40] + ("..." if len(msg_result.message) > 40 else "")

        gate_flag = "OK" if verdict.allowed else f"BLOCKED({verdict.rule_triggered})"

        print(f"{p['id']:<12} {p['error_code']:<20} {classification.recommended_primary_action:<18} "
              f"{decision.chosen_action:<18} {gate_flag:<8} {final_action:<18} {message_preview}")

    print()
    if fallback_details:
        print(f"⚠️  {len(fallback_details)} fallback(s) triggered:\n")
        for payment_id, stage, reason in fallback_details:
            print(f"  [{payment_id}] {stage}:")
            print(f"    {reason}\n")
    else:
        print("✅ All LLM calls succeeded live -- no fallbacks triggered.")
        print("   Stages 2 through 5 are confirmed working together end-to-end.")


if __name__ == "__main__":
    main()
