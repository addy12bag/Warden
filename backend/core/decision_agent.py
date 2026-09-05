"""
Stage 4 -- LLM decision layer (Groq API).

Given a classified payment (root_cause, is_retryable) plus context
(amount, retry_count, customer_tenure_days), this layer selects ONE
action from the bounded action set below and produces a short
reasoning string for the audit trail.

Bounded action set (the LLM may ONLY choose from these):
    - retry_now
    - retry_delayed
    - prompt_method_switch
    - send_reminder
    - escalate
    - no_action

The LLM does not decide retryability from scratch -- it receives the
classifier's verdict as a constraint and reasons over WHICH bounded
action fits best given amount, customer tenure, and retry history.

CONSTRAINED OUTPUT DESIGN:
    This module uses Groq's OpenAI-compatible tool-calling API, NOT
    free-form text generation parsed with regex/string matching. The
    model is given exactly one tool ("submit_recovery_decision") with
    a JSON schema that enums the six allowed actions. Groq's inference
    engine enforces the schema at generation time -- the model
    literally cannot emit an action string outside ALLOWED_ACTIONS
    through this path, the way it could if we just asked it to reply
    with the action name in prose and hoped.

    We ALSO validate the returned action against ALLOWED_ACTIONS in
    Python before returning (defense in depth -- never trust a single
    layer, matches the whole project's philosophy). If the model
    fails to call the tool at all, returns malformed arguments, or the
    API call itself fails after retries, we fail safe to "escalate"
    rather than guessing or crashing the batch.

    Note: this module proposes an action. It does NOT have final say --
    every proposal still passes through stopping_rules.check_stopping_rules()
    (Stage 3) before anything executes. The LLM's judgment is advisory
    within the bounded set; the stopping rules engine is the actual gate.

Env vars required:
    GROQ_API_KEY

TODO (Stage 4 -- DONE):
    - Implement decide_action(classification, payment_context) -> DecisionResult
    - Use structured/JSON-mode output, not free-text parsing
    - Validate output against ALLOWED_ACTIONS; fail safe to "escalate"
    - Add retry/backoff for API rate limits
"""

import json
import os
import time
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()  # loads backend/.env into os.environ if present; no-op if already set

ALLOWED_ACTIONS = [
    "retry_now",
    "retry_delayed",
    "prompt_method_switch",
    "send_reminder",
    "escalate",
    "no_action",
]

# GROQ_MODEL_CHAIN: models tried in order, each with its OWN separate
# free-tier quota bucket. Rate limits on Groq are tracked per-model, not
# shared across the account -- so when the primary model's daily token
# quota is exhausted (a real 429 hit during live batch testing on
# 2026-08-26: "openai/gpt-oss-20b" TPD limit of 200,000 exceeded), or a
# model is unavailable on this account entirely (a real 404 hit on
# 2026-08-27: "llama-3.3-70b-versatile" returned model_not_found --
# confirmed via `curl https://api.groq.com/openai/v1/models` that it
# does not appear in this account's live model list at all), falling
# back to a DIFFERENT model is the correct fix.
#
# IMPORTANT: this chain was built by querying the account's ACTUAL live
# model list directly (see command above), not by guessing model names
# from docs/memory -- Groq's available models and names have already
# churned multiple times during this project. If this chain ever fails
# entirely, re-run that curl command and rebuild the chain from
# whatever it currently returns, filtering for entries whose
# supported_features includes "tools" (required for our structured
# decision output).
#
# Chain order rationale: prioritizes different model owners/providers
# (OpenAI-family vs Qwen/Alibaba) since that further reduces the odds
# of two chain entries sharing an underlying quota pool, on top of
# Groq's own per-model tracking.
#   1. openai/gpt-oss-20b        -- primary, best quality/speed/cost tradeoff
#   2. openai/gpt-oss-120b       -- same family, separate quota, larger model
#   3. qwen/qwen3.8-27b          -- different provider entirely (Alibaba Cloud)
#
# If ALL models in the chain are exhausted/unavailable, decide_action()
# still fails safe to "escalate" rather than crash the batch -- this
# chain reduces how often that fallback fires, it doesn't replace the
# fail-safe design.
GROQ_MODEL_CHAIN = [
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
    "qwen/qwen3.8-27b",
]

# Kept for backward compatibility with any code/tests referencing the
# single-model constant directly; always the first model in the chain.
GROQ_MODEL = GROQ_MODEL_CHAIN[0]

MAX_API_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2

DECISION_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "submit_recovery_decision",
        "description": (
            "Submit the chosen recovery action for this failed payment, "
            "along with the reasoning behind the choice."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chosen_action": {
                    "type": "string",
                    "enum": ALLOWED_ACTIONS,
                    "description": "The single recovery action to take, from the bounded action set.",
                },
                "reasoning": {
                    "type": "string",
                    "description": (
                        "One or two sentences explaining why this action was "
                        "chosen over the alternatives, referencing the "
                        "specific context (amount, retry history, tenure) "
                        "that informed the choice."
                    ),
                },
            },
            "required": ["chosen_action", "reasoning"],
        },
    },
}

SYSTEM_PROMPT = """You are a payment recovery decision agent for a fintech platform.

You will be given a failed payment that has ALREADY been classified by a
deterministic rules engine. That classification includes an is_retryable
verdict which you must treat as authoritative -- do not second-guess it.

Your job is narrower: given the classification and the transaction's
context (amount, retry_count, customer_tenure_days, payment_method),
choose the SINGLE most appropriate action from the bounded set, using
judgment about the specific context. For example:
- A high-value transaction with a first-time transient failure might
  warrant retry_now over waiting for a delayed retry.
- A long-tenure customer with a soft decline might warrant a gentler
  send_reminder over an immediate retry, to avoid over-messaging a
  loyal customer.
- If is_retryable is false, you must choose a non-retry action
  (prompt_method_switch, escalate, or no_action) -- never retry_now or
  retry_delayed in that case.

You MUST call the submit_recovery_decision tool with your choice. Do not
respond in plain text."""


@dataclass
class DecisionResult:
    payment_id: str
    chosen_action: str
    reasoning: str
    raw_model_output: str
    fallback_used: bool = False


def _build_user_prompt(classification: dict, payment_context: dict) -> str:
    return (
        f"Payment ID: {payment_context.get('id', '<unknown>')}\n"
        f"Root cause (from classifier): {classification.get('root_cause')}\n"
        f"Error category: {classification.get('error_category')}\n"
        f"Is retryable (classifier verdict, authoritative): {classification.get('is_retryable')}\n"
        f"Classifier's recommended action: {classification.get('recommended_primary_action')}\n"
        f"Classifier reasoning: {classification.get('reasoning')}\n"
        "\n"
        f"Amount: {payment_context.get('amount')}\n"
        f"Payment method: {payment_context.get('payment_method')}\n"
        f"Retry count so far: {payment_context.get('retry_count')}\n"
        f"Customer tenure (days): {payment_context.get('customer_tenure_days')}\n"
        f"Language preference: {payment_context.get('language_pref')}\n"
        "\n"
        "Choose the single best action from the bounded set and explain why."
    )


def _get_client():
    """Lazily construct the Groq client so importing this module never
    requires the groq package or an API key unless decide_action is
    actually called."""
    from groq import Groq

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "GROQ_API_KEY not set. Copy backend/.env.example to backend/.env "
            "and fill in your free Groq API key from console.groq.com."
        )
    return Groq(api_key=api_key)


def _fallback_result(payment_id: str, reason: str) -> DecisionResult:
    return DecisionResult(
        payment_id=payment_id,
        chosen_action="escalate",
        reasoning=f"FALLBACK (LLM path failed): {reason}. Failing safe to escalate.",
        raw_model_output="",
        fallback_used=True,
    )


def decide_action(classification: dict, payment_context: dict) -> DecisionResult:
    """
    Call Groq API to select a bounded action for one payment.

    Args:
        classification: dict form of a classifier.ClassificationResult
                         (root_cause, error_category, is_retryable,
                         recommended_primary_action, reasoning).
        payment_context: raw payment dict (id, amount, payment_method,
                          retry_count, customer_tenure_days, language_pref).

    Returns:
        DecisionResult. On any failure (missing key, API error after
        retries, malformed tool call, out-of-set action), returns a
        fallback DecisionResult with chosen_action="escalate" and
        fallback_used=True rather than raising -- a single payment's
        LLM failure must never crash a batch run.
    """
    payment_id = payment_context.get("id", "<unknown>")

    try:
        client = _get_client()
    except EnvironmentError as e:
        return _fallback_result(payment_id, str(e))

    user_prompt = _build_user_prompt(classification, payment_context)

    last_error = None
    for model in GROQ_MODEL_CHAIN:
        for attempt in range(1, MAX_API_RETRIES + 1):
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    tools=[DECISION_TOOL_SCHEMA],
                    tool_choice={"type": "function", "function": {"name": "submit_recovery_decision"}},
                    temperature=0.2,  # low temperature: this is a judgment call, not creative writing
                )

                message = response.choices[0].message
                tool_calls = getattr(message, "tool_calls", None)

                if not tool_calls:
                    last_error = f"Model {model} did not call the required tool"
                    break  # not transient -- try the next model in the chain

                raw_args = tool_calls[0].function.arguments
                parsed = json.loads(raw_args)  # can raise json.JSONDecodeError

                chosen_action = parsed.get("chosen_action")
                reasoning = parsed.get("reasoning", "")

                if chosen_action not in ALLOWED_ACTIONS:
                    return _fallback_result(
                        payment_id,
                        f"Model {model} returned out-of-set action '{chosen_action}'",
                    )

                return DecisionResult(
                    payment_id=payment_id,
                    chosen_action=chosen_action,
                    reasoning=reasoning,
                    raw_model_output=raw_args,
                    fallback_used=False,
                )

            except json.JSONDecodeError as e:
                last_error = f"Model {model}: malformed tool call arguments: {e}"
                break  # malformed JSON won't fix itself on retry OR model swap -- try next model

            except Exception as e:
                last_error = str(e)
                last_error_lower = last_error.lower()
                is_rate_limit = (
                    "rate_limit" in last_error_lower
                    or "rate limit" in last_error_lower
                    or "429" in last_error
                )
                if is_rate_limit:
                    # This model's quota is exhausted for the day -- no
                    # amount of retrying THIS model will help. Break out
                    # to the outer loop, which moves to the next model.
                    break
                if attempt < MAX_API_RETRIES:
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt)  # linear backoff
                    continue
                break  # exhausted retries on a non-rate-limit error -- try next model

    return _fallback_result(payment_id, last_error or "Unknown error")


def decide_batch(classifications: list[dict], payment_contexts: list[dict]) -> list[DecisionResult]:
    """
    Run decide_action across a batch. Payments are matched by list
    position -- caller must ensure classifications[i] corresponds to
    payment_contexts[i]. Does not stop on individual failures since
    decide_action() already fails safe per-payment.
    """
    return [
        decide_action(c, p) for c, p in zip(classifications, payment_contexts)
    ]
