"""
Manual live smoke test for messaging_agent.py against the REAL Gemini API.

Like manual_live_smoke_test.py for Groq, this is NOT part of the
automated pytest suite -- run it directly, by hand, once you have a
real GEMINI_API_KEY set.

Usage:
    cd backend
    # .env should already have GEMINI_API_KEY filled in from Stage 4 setup
    python tests/manual_live_smoke_test_messaging.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.messaging_agent import draft_message

SAMPLE_CASES = [
    {
        "name": "English reminder, card payment",
        "payment": {"id": "txn_msg_1", "amount": 799.0, "payment_method": "card"},
        "chosen_action": "send_reminder",
        "language_pref": "en",
    },
    {
        "name": "Hinglish reminder, card payment",
        "payment": {"id": "txn_msg_2", "amount": 2500.0, "payment_method": "card"},
        "chosen_action": "send_reminder",
        "language_pref": "hi-en",
    },
    {
        "name": "Expired card -- prompt method switch, English",
        "payment": {"id": "txn_msg_3", "amount": 1200.0, "payment_method": "card"},
        "chosen_action": "prompt_method_switch",
        "language_pref": "en",
    },
    {
        "name": "UPI mandate retry -- should mention automatic retry, not manual action",
        "payment": {"id": "txn_msg_4", "amount": 5000.0, "payment_method": "upi"},
        "chosen_action": "retry_delayed",
        "language_pref": "en",
    },
]


def main():
    print("Running live Gemini API smoke test...\n")

    for case in SAMPLE_CASES:
        result = draft_message(case["payment"], case["chosen_action"], case["language_pref"])
        fallback_flag = "YES (check reasoning!)" if result.fallback_used else "no"
        print(f"--- {case['name']} ---")
        print(f"  fallback used: {fallback_flag}")
        if result.fallback_used:
            print(f"  fallback reason: {result.fallback_reason}")
        print(f"  language_used: {result.language_used}")
        print(f"  message ({len(result.message)} chars): {result.message}")
        print()

    print("Sanity checks to eyeball:")
    print("  - Hinglish case should read like natural Hinglish, not formal Hindi")
    print("    or a stiff word-for-word English translation.")
    print("  - UPI mandate retry case should say the retry happens automatically,")
    print("    NOT ask the customer to manually retry the payment themselves.")
    print("  - None of the messages should contain urgency/pressure language")
    print("    ('last chance', 'act now', etc) -- if any do, the dark-pattern")
    print("    guardrail should have already caught it and used a fallback instead,")
    print("    so seeing pressure language here would indicate a guardrail bug.")


if __name__ == "__main__":
    main()
