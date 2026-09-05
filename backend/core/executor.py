"""
Stage 6 -- Action executor + outcome logger.

Simulates executing the final (stopping-rules-approved) action against
a synthetic payment, determines a simulated outcome, and writes the
full decision record to the `decisions` table -- this table IS the
audit trail referenced throughout PROJECT.md.

Flow for one payment:
    1. classifier.classify_payment()
    2. decision_agent.decide_action()
    3. stopping_rules.check_stopping_rules()  <-- can override step 2
    4. messaging_agent.draft_message()        <-- if action requires it
    5. execute_action()                        <-- this module
    6. log_decision()                          <-- this module, writes to DB

OUTCOME SIMULATION DESIGN:
    Outcomes are probabilistic but grounded in the same real-world
    decline-recovery research used for Stage 1's synthetic dataset
    (soft declines/transient failures are usually recoverable if
    retried correctly; hard declines are not). This is NOT meant to
    perfectly model reality -- it exists so Stage 7's evaluation has
    plausible, varied outcomes to measure against, rather than either
    100% or 0% success across the board, which would make the recovery
    rate metric meaningless.

    Deterministic actions (no randomness):
        escalate  -> always "escalated" (handed to a human, not a
                     model output to grade as success/failure)
        no_action -> always "still_failing" (deliberately nothing was
                     attempted)

    Probabilistic actions (randomness seeded per-payment for
    reproducibility -- see _outcome_rng below):
        retry_now            -> high success chance for transient/soft
                                 declines, since immediate retry is the
                                 textbook-correct response to these
        retry_delayed        -> moderate success chance, slightly below
                                 retry_now (the delay exists precisely
                                 because immediate success wasn't
                                 guaranteed, e.g. insufficient funds)
        prompt_method_switch -> depends on customer response; modeled
                                 as a moderate base chance, nudged by
                                 customer_tenure_days as a rough proxy
                                 for engagement/trust with the platform
        send_reminder        -> lower chance still, since it's the
                                 gentlest, most passive intervention

TODO (Stage 6 -- DONE):
    - Implement execute_action(payment, final_action) -> outcome
    - Implement log_decision(...) -> writes one row to `decisions` table
    - Implement run_batch(payments) that chains stages 2-6 end to end
"""

import random
from dataclasses import dataclass, asdict
from datetime import datetime

from core.classifier import classify_payment
from core.decision_agent import decide_action
from core.messaging_agent import draft_message, ACTIONS_REQUIRING_MESSAGE
from core.stopping_rules import check_stopping_rules
from db.db import get_connection

OUTCOME_OPTIONS = {"recovered", "still_failing", "escalated", "pending"}

# Base success probabilities by final action, before any per-payment
# adjustment. Grounded in the same soft/hard decline recovery research
# used in Stage 1 (soft declines ~70-90% recoverable in the real world
# when the RIGHT intervention is used -- these numbers sit deliberately
# below that range since our simulation should reflect an imperfect
# real-world execution, not a best-case ceiling).
BASE_SUCCESS_PROBABILITY = {
    "retry_now": 0.72,
    "retry_delayed": 0.58,
    "prompt_method_switch": 0.45,
    "send_reminder": 0.30,
}

# Deterministic actions -- no randomness, no probability lookup needed.
DETERMINISTIC_OUTCOMES = {
    "escalate": "escalated",
    "no_action": "still_failing",
}

TENURE_BONUS_THRESHOLD_DAYS = 365
TENURE_BONUS = 0.08  # long-tenure customers slightly more likely to respond


@dataclass
class DecisionRecord:
    payment_id: str
    root_cause: str
    is_retryable: bool
    chosen_action: str
    reasoning: str
    message_sent: str | None
    stopping_rule_triggered: str | None
    outcome: str
    timestamp: str


def _outcome_rng(payment_id: str) -> random.Random:
    """
    A Random instance seeded deterministically from the payment_id, so
    re-running the batch on the same data produces the same simulated
    outcomes -- this matters for reproducibility when comparing runs,
    debugging, or re-generating the same demo numbers for a pitch video.
    """
    return random.Random(f"outcome-{payment_id}")


def simulate_outcome(payment: dict, final_action: str) -> str:
    """
    Simulate the outcome of executing final_action against payment.
    Deterministic per payment_id (same payment + action always
    produces the same simulated outcome on repeated runs).
    """
    if final_action in DETERMINISTIC_OUTCOMES:
        return DETERMINISTIC_OUTCOMES[final_action]

    base_prob = BASE_SUCCESS_PROBABILITY.get(final_action)
    if base_prob is None:
        # Unknown/unexpected action reaching execution -- fail safe to
        # still_failing rather than guessing at a success probability
        # for an action this module doesn't recognize.
        return "still_failing"

    prob = base_prob
    if final_action == "prompt_method_switch":
        try:
            tenure = int(payment.get("customer_tenure_days", 0) or 0)
        except (TypeError, ValueError):
            tenure = 0
        if tenure >= TENURE_BONUS_THRESHOLD_DAYS:
            prob += TENURE_BONUS

    prob = min(prob, 0.95)  # never simulate certainty -- always leave room for failure

    rng = _outcome_rng(payment.get("id", ""))
    return "recovered" if rng.random() < prob else "still_failing"


def execute_action(payment: dict, final_action: str) -> str:
    """
    Simulate executing the final action against a synthetic payment.
    Returns one of OUTCOME_OPTIONS.

    This is a SIMULATION -- no real retry, message send, or escalation
    ticket is actually created. It exists to give Stage 7's evaluation
    plausible, varied outcomes to measure recovery rate against.
    """
    outcome = simulate_outcome(payment, final_action)
    if outcome not in OUTCOME_OPTIONS:
        raise ValueError(
            f"simulate_outcome returned '{outcome}', which is not in "
            f"OUTCOME_OPTIONS {OUTCOME_OPTIONS} -- this indicates a bug "
            f"in simulate_outcome, not bad input data."
        )
    return outcome


def log_decision(record: DecisionRecord, db_path=None) -> None:
    """Persist a DecisionRecord to the `decisions` table."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            INSERT INTO decisions (
                payment_id, root_cause, is_retryable, chosen_action,
                reasoning, message_sent, stopping_rule_triggered,
                outcome, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.payment_id,
                record.root_cause,
                int(record.is_retryable),
                record.chosen_action,
                record.reasoning,
                record.message_sent,
                record.stopping_rule_triggered,
                record.outcome,
                record.timestamp,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def process_payment(payment: dict, db_path=None) -> DecisionRecord:
    """
    Run one payment through the full Stage 2-6 pipeline: classify,
    decide, gate, message (if needed), execute, and log. Returns the
    DecisionRecord that was written to the database.

    This function does not raise on expected per-payment issues (LLM
    fallbacks are already handled inside decision_agent/messaging_agent
    with their own fail-safes) -- but a malformed payment record
    (missing/unrecognized error_code) will still raise from
    classify_payment(), by design, since that indicates bad input data
    worth stopping on rather than silently skipping.
    """
    classification = classify_payment(payment)
    c_dict = asdict(classification)

    decision = decide_action(c_dict, payment)

    verdict = check_stopping_rules(
        payment,
        error_category=classification.error_category,
        is_retryable=classification.is_retryable,
        proposed_action=decision.chosen_action,
        retry_count=int(payment.get("retry_count", 0) or 0),
    )
    final_action = decision.chosen_action if verdict.allowed else verdict.forced_action

    message_sent = None
    if final_action in ACTIONS_REQUIRING_MESSAGE:
        msg_result = draft_message(payment, final_action, payment.get("language_pref", "en"))
        message_sent = msg_result.message

    outcome = execute_action(payment, final_action)

    reasoning_parts = [f"[classifier] {classification.reasoning}", f"[llm] {decision.reasoning}"]
    if not verdict.allowed:
        reasoning_parts.append(f"[stopping_rules] {verdict.reasoning}")
    combined_reasoning = " ".join(reasoning_parts)

    record = DecisionRecord(
        payment_id=payment.get("id", "<unknown>"),
        root_cause=classification.root_cause,
        is_retryable=classification.is_retryable,
        chosen_action=final_action,
        reasoning=combined_reasoning,
        message_sent=message_sent,
        stopping_rule_triggered=verdict.rule_triggered,
        outcome=outcome,
        timestamp=datetime.now().isoformat(),
    )

    log_decision(record, db_path)
    return record


def run_batch(payments: list[dict], db_path=None) -> list[DecisionRecord]:
    """
    Chain Stages 2-6 across a full batch of payments.

    Does NOT stop the whole batch if one payment fails unexpectedly
    (e.g. a malformed record) -- logs a warning-style DecisionRecord
    with outcome="still_failing" and a diagnostic note in reasoning,
    rather than crashing a multi-hundred-record run over one bad row.
    LLM-level failures are already handled by fail-safe fallbacks
    inside decision_agent/messaging_agent and do not trigger this path.
    """
    records = []
    for payment in payments:
        try:
            record = process_payment(payment, db_path)
        except Exception as e:
            payment_id = payment.get("id", "<unknown>")
            record = DecisionRecord(
                payment_id=payment_id,
                root_cause="unknown",
                is_retryable=False,
                chosen_action="escalate",
                reasoning=f"[executor] Unexpected error processing payment: {e}. Failing safe to escalate.",
                message_sent=None,
                stopping_rule_triggered="PROCESSING_ERROR",
                outcome="escalated",
                timestamp=datetime.now().isoformat(),
            )
            log_decision(record, db_path)
        records.append(record)
    return records
