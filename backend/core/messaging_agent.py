"""
Stage 5 -- Messaging agent (Gemini API).

Drafts the customer-facing recovery message once decision_agent.py has
chosen an action that requires customer communication (e.g.
send_reminder, prompt_method_switch). Supports English and Hinglish
based on the payment's language_pref field -- this is the Hinglish
messaging feature folded into the core agent per PROJECT.md.

Also handles mandate-retry-sequence language for UPI/e-mandate
payment methods where retry timing must reference the mandate cycle.

Env vars required:
    GEMINI_API_KEY

TODO (Stage 5):
    - Implement draft_message(payment, chosen_action, language_pref) -> str
    - Add tone/length guardrails (keep messages short, no dark patterns)
    - Add Hinglish prompt template distinct from the English template
    - Add mandate-specific phrasing for payment_method == "upi" with
      retry_delayed action
"""

"""
Stage 5 -- Messaging agent (Gemini API).

Drafts the customer-facing recovery message once decision_agent.py has
chosen an action that requires customer communication (e.g.
send_reminder, prompt_method_switch, retry_delayed). Supports English
and Hinglish based on the payment's language_pref field -- this is the
Hinglish messaging feature folded into the core agent per PROJECT.md.

Also handles mandate-retry-sequence language for UPI payment methods
where a retry_delayed action should reference the mandate/NPCI retry
cycle rather than reading like a generic "please try again" message.

SDK NOTE: uses the current `google-genai` package (import as
`from google import genai`), NOT the deprecated `google-generativeai`
package. The old package reached end-of-life on 2025-11-30 -- see
https://ai.google.dev/gemini-api/docs/migrate. If this module ever
starts failing with import errors, check requirements.txt has
`google-genai`, not `google-generativeai`.

CONSTRAINED OUTPUT DESIGN:
    Mirrors the discipline in decision_agent.py: this module does NOT
    let the model return free-form text that we then regex/string-hunt
    through. Gemini's response_mime_type="application/json" +
    response_schema forces the model to return a JSON object matching
    MessageOutputSchema, which we then validate again in Python before
    returning -- same defense-in-depth philosophy as Stage 4.

    On any failure (missing key, malformed JSON, empty message, API
    error after retries), this module fails safe by returning a
    pre-written FALLBACK_TEMPLATES message rather than raising or
    returning empty content -- a customer-facing message pipeline must
    never silently produce nothing, since executor.py (Stage 6) will
    actually "send" whatever this returns.

Guardrails enforced (per Stage 5 brief -- "no dark patterns"):
    - Messages must be short (enforced via max_output_tokens AND a
      post-hoc length check)
    - No fake urgency/scarcity language ("last chance", "act now or
      lose access") -- enforced via explicit system prompt instruction
    - No guilt-tripping or shaming language
    - Always states the actual amount and gives a clear, single next
      step -- never vague

Env vars required:
    GEMINI_API_KEY

TODO (Stage 5 -- DONE):
    - Implement draft_message(payment, chosen_action, language_pref) -> str
    - Add tone/length guardrails (keep messages short, no dark patterns)
    - Add Hinglish prompt template distinct from the English template
    - Add mandate-specific phrasing for payment_method == "upi" with
      retry_delayed action
"""

import os
import time
from dataclasses import dataclass

from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()  # loads backend/.env into os.environ if present; no-op if already set

# gemini-3.6-flash was live-tested and worked correctly once the
# thinking_budget=0 fix was applied (see comment on GenerateContentConfig
# below), BUT its free tier quota was found to be only 20 requests/day
# per project -- nowhere near enough for a batch of hundreds of payment
# records. Flash-Lite variants consistently carry the most generous free
# daily quota (typically 1,000+ RPD vs Flash's much lower and frequently
# shrinking allowance), so that's the right tier for this batch workload.
#
# IMPORTANT: verify the exact current Flash-Lite model name for YOUR
# account before trusting this blindly -- Gemini model names/tiers have
# churned at least twice already during this project (2.5-flash, then
# 3.6-flash, now this). Run this once with your real GEMINI_API_KEY to
# see the live list and exact free-tier limits for your account:
#
#   curl "https://generativelanguage.googleapis.com/v1beta/models?key=$GEMINI_API_KEY"
#
# and check https://ai.google.dev/gemini-api/docs/rate-limits for current
# published quotas per model. Update GEMINI_MODEL below if the name has
# changed again by the time you read this.
GEMINI_MODEL = "gemini-flash-lite-latest"

ACTIONS_REQUIRING_MESSAGE = {"send_reminder", "prompt_method_switch", "retry_delayed"}

MAX_API_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2
MAX_MESSAGE_CHARS = 320  # roughly 2 SMS segments' worth -- keeps messages genuinely short

DARK_PATTERN_PHRASES = [
    # Post-hoc guardrail check: if the model slips one of these in
    # despite the system prompt instruction, we catch it here rather
    # than trusting the instruction alone (same "don't trust one layer"
    # philosophy as the rest of the project).
    "last chance", "act now", "expires today", "don't miss out",
    "limited time", "hurry", "final notice", "urgent action required",
]

# Fail-safe fallback messages, used when the LLM path fails entirely.
# These are deliberately plain, calm, and compliant -- no urgency, no
# dark patterns -- since they may be the ONLY message a customer sees
# if Gemini is unreachable.
FALLBACK_TEMPLATES = {
    "en": (
        "Hi, we noticed your recent payment of {amount_display} didn't go through. "
        "You can try again from your account, or update your payment method if needed. "
        "Reply to this message if you'd like help."
    ),
    "hi-en": (
        "Hi, aapka {amount_display} ka payment complete nahi ho paya. "
        "Aap apne account se dobara try kar sakte hain, ya payment method update kar sakte hain. "
        "Madad chahiye toh reply karein."
    ),
}


class MessageOutputSchema(BaseModel):
    """Schema Gemini's structured output is constrained to."""
    message: str
    language_used: str  # "en" or "hi-en", model's own confirmation of what it produced


@dataclass
class MessageResult:
    payment_id: str
    message: str
    language_used: str
    fallback_used: bool = False
    fallback_reason: str | None = None


def _format_amount(amount) -> str:
    try:
        return f"\u20b9{float(amount):,.2f}"
    except (TypeError, ValueError):
        return "the payment amount"


def _build_system_prompt(payment: dict, chosen_action: str, language_pref: str) -> str:
    is_upi_mandate_retry = (
        payment.get("payment_method") == "upi" and chosen_action == "retry_delayed"
    )

    language_instruction = (
        "Write the message in natural, everyday Hinglish (Hindi mixed with English, "
        "written in Latin/Roman script, the way Indian customers actually text each "
        "other) -- not formal Hindi, not a literal translation of an English message."
        if language_pref == "hi-en"
        else "Write the message in clear, simple English."
    )

    mandate_instruction = ""
    if is_upi_mandate_retry:
        mandate_instruction = (
            "\n\nThis is a UPI payment being retried under the mandate retry cycle. "
            "Reference that the retry will happen automatically as part of the mandate "
            "schedule (do not ask the customer to manually retry) -- e.g. mention that "
            "the payment will be attempted again automatically, and they don't need to "
            "do anything unless they want to update their payment method."
        )

    action_instruction = {
        "send_reminder": "Gently remind the customer their payment didn't go through and invite them to retry when convenient.",
        "prompt_method_switch": "Let the customer know this payment method didn't work and ask them to try a different payment method or update their card details.",
        "retry_delayed": "Let the customer know we'll automatically retry the payment shortly, and they don't need to do anything unless they'd like to update their payment details.",
    }.get(chosen_action, "Inform the customer about the payment issue and the next step.")

    return f"""You are drafting a short customer-facing payment recovery message for a
fintech platform. {language_instruction}

Context: {action_instruction}{mandate_instruction}

STRICT RULES:
- Keep it under {MAX_MESSAGE_CHARS} characters total.
- State the actual amount clearly.
- Give exactly ONE clear next step.
- NEVER use urgency, scarcity, or pressure language (no "last chance", "act now",
  "hurry", "limited time", "expires today", or similar). This must read as a calm,
  helpful, respectful notice -- not a marketing message.
- NEVER guilt-trip, shame, or imply the customer did something wrong.
- NEVER invent details not given to you (no fake order numbers, fake deadlines, etc).
- Sign off simply, no aggressive calls to action.

You MUST respond with a JSON object matching the required schema: a "message" field
with the drafted text, and a "language_used" field confirming "en" or "hi-en"."""


def _contains_dark_pattern(text: str) -> str | None:
    lowered = text.lower()
    for phrase in DARK_PATTERN_PHRASES:
        if phrase in lowered:
            return phrase
    return None


def _get_client():
    """Lazily construct the Gemini client so importing this module never
    requires the google-genai package or an API key unless
    draft_message is actually called."""
    from google import genai

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "GEMINI_API_KEY not set. Copy backend/.env.example to backend/.env "
            "and fill in your free Gemini API key from aistudio.google.com/apikey."
        )
    return genai.Client(api_key=api_key)


def _fallback_result(payment_id: str, amount, language_pref: str, reason: str) -> MessageResult:
    lang = language_pref if language_pref in FALLBACK_TEMPLATES else "en"
    template = FALLBACK_TEMPLATES[lang]
    message = template.format(amount_display=_format_amount(amount))
    return MessageResult(
        payment_id=payment_id,
        message=message,
        language_used=lang,
        fallback_used=True,
        fallback_reason=reason,
    )


def draft_message(payment: dict, chosen_action: str, language_pref: str) -> MessageResult:
    """
    Generate the recovery message text for a customer.

    Args:
        payment: raw payment dict (id, amount, payment_method, etc.)
        chosen_action: the action decision_agent.py selected -- should
                       be one of ACTIONS_REQUIRING_MESSAGE, though this
                       function will still draft something reasonable
                       for other actions if called.
        language_pref: "en" or "hi-en" (Hinglish)

    Returns:
        MessageResult. On any failure, returns a fallback result built
        from FALLBACK_TEMPLATES rather than raising or returning an
        empty message -- a customer-facing pipeline must always have
        SOMETHING to send.
    """
    payment_id = payment.get("id", "<unknown>")
    amount = payment.get("amount")

    try:
        client = _get_client()
    except EnvironmentError as e:
        return _fallback_result(payment_id, amount, language_pref, str(e))

    system_prompt = _build_system_prompt(payment, chosen_action, language_pref)

    from google.genai import types

    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        response_mime_type="application/json",
        response_schema=MessageOutputSchema,
        temperature=0.4,  # a little creative latitude for natural phrasing, still low
        max_output_tokens=500,  # headroom above the raw message length in case
                                 # thinking isn't fully suppressed
        thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL),
        # IMPORTANT: Gemini 3.x models use thinking_level (a string level:
        # MINIMAL/LOW/MEDIUM/HIGH), NOT the legacy thinking_budget (integer
        # token count) used by Gemini 2.5 models. Passing thinking_budget to
        # a Gemini 3.x model returns a generic, detail-free 400
        # INVALID_ARGUMENT with no indication of which field is wrong -- this
        # was live-verified against gemini-flash-lite-latest. Passing BOTH
        # fields in the same request is also invalid and 400s regardless of
        # model generation. If GEMINI_MODEL is ever changed back to a 2.5-era
        # model, this must revert to thinking_budget=0 instead.
        # This is a short, bounded drafting task with a fixed schema and no
        # multi-step reasoning required, so MINIMAL is the right setting --
        # it keeps latency/cost down without needing full thinking disabled
        # (which isn't a supported concept on Gemini 3.x's thinking_level API).
    )

    user_content = (
        f"Payment ID: {payment_id}\n"
        f"Amount: {_format_amount(amount)}\n"
        f"Payment method: {payment.get('payment_method')}\n"
        f"Chosen action: {chosen_action}\n"
        "Draft the message now."
    )

    last_error = None
    for attempt in range(1, MAX_API_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=user_content,
                config=config,
            )

            parsed: MessageOutputSchema = response.parsed
            if parsed is None:
                # response.parsed is None if schema validation failed
                # server-side or the model didn't return valid JSON.
                last_error = f"Model response did not parse against schema. Raw text: {response.text!r}"
                break  # not transient, retrying won't fix a schema mismatch

            message_text = parsed.message.strip()

            if not message_text:
                return _fallback_result(payment_id, amount, language_pref, "Model returned empty message")

            if len(message_text) > MAX_MESSAGE_CHARS:
                # Trim rather than fully reject -- a slightly-too-long
                # but otherwise good message is still usable, unlike a
                # dark pattern or hallucinated action.
                message_text = message_text[:MAX_MESSAGE_CHARS].rsplit(" ", 1)[0] + "..."

            dark_pattern_hit = _contains_dark_pattern(message_text)
            if dark_pattern_hit:
                return _fallback_result(
                    payment_id, amount, language_pref,
                    f"Model output contained disallowed pressure language: '{dark_pattern_hit}'",
                )

            return MessageResult(
                payment_id=payment_id,
                message=message_text,
                language_used=parsed.language_used,
                fallback_used=False,
            )

        except Exception as e:
            last_error = str(e)
            if attempt < MAX_API_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            break

    return _fallback_result(payment_id, amount, language_pref, last_error or "Unknown error")


def draft_batch(payments: list[dict], actions: list[str], language_prefs: list[str]) -> list[MessageResult]:
    """
    Draft messages across a batch. Lists are matched by position --
    caller must ensure payments[i]/actions[i]/language_prefs[i] all
    correspond to the same record. Does not stop on individual
    failures since draft_message() already fails safe per-payment.
    """
    return [
        draft_message(p, a, lp) for p, a, lp in zip(payments, actions, language_prefs)
    ]
