"""
Unit tests for backend/core/messaging_agent.py (Stage 5).

Mocks the Gemini client entirely -- no real API key or network call
required. As with test_decision_agent.py, weighted toward fail-safe
behavior and guardrail enforcement (dark-pattern detection, length
limits, fallback templates) since those are the highest-risk parts of
a module whose output gets shown directly to real customers.

Run with:
    cd backend && python -m pytest tests/test_messaging_agent.py -v
"""

import sys
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.messaging_agent import (
    draft_message,
    draft_batch,
    MessageOutputSchema,
    FALLBACK_TEMPLATES,
    DARK_PATTERN_PHRASES,
    MAX_MESSAGE_CHARS,
    MAX_API_RETRIES,
    _contains_dark_pattern,
    _format_amount,
)


def make_payment(payment_id="txn_test", amount=500.0, payment_method="card"):
    return {"id": payment_id, "amount": amount, "payment_method": payment_method}


def make_mock_response(message="Your payment of ₹500 didn't go through. Please retry.",
                        language_used="en", parsed_override=None, raw_text=None):
    parsed = parsed_override if parsed_override is not None else MessageOutputSchema(
        message=message, language_used=language_used
    )
    return SimpleNamespace(parsed=parsed, text=raw_text or message)


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------

@patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"})
@patch("core.messaging_agent._get_client")
def test_valid_response_returns_message(mock_get_client):
    mock_client = mock_get_client.return_value
    mock_client.models.generate_content.return_value = make_mock_response(
        message="Your payment of ₹500.00 didn't go through. Please try again.",
        language_used="en",
    )

    result = draft_message(make_payment(), "send_reminder", "en")

    assert result.fallback_used is False
    assert "didn't go through" in result.message
    assert result.language_used == "en"
    assert result.payment_id == "txn_test"


@patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"})
@patch("core.messaging_agent._get_client")
def test_hinglish_response_passes_through(mock_get_client):
    mock_client = mock_get_client.return_value
    mock_client.models.generate_content.return_value = make_mock_response(
        message="Aapka ₹500 ka payment nahi hua. Please dobara try karein.",
        language_used="hi-en",
    )

    result = draft_message(make_payment(), "send_reminder", "hi-en")

    assert result.fallback_used is False
    assert result.language_used == "hi-en"
    assert "Aapka" in result.message


# ---------------------------------------------------------------------------
# 2. Missing API key -- fails safe to fallback template, never raises,
#    never returns empty
# ---------------------------------------------------------------------------

@patch.dict(os.environ, {}, clear=True)
def test_missing_api_key_falls_back_to_template():
    result = draft_message(make_payment(payment_id="txn_no_key", amount=750.0), "send_reminder", "en")

    assert result.fallback_used is True
    assert result.payment_id == "txn_no_key"
    assert result.message  # never empty
    assert "750" in result.message  # amount correctly interpolated into fallback


@patch.dict(os.environ, {}, clear=True)
def test_missing_api_key_hinglish_uses_hinglish_fallback():
    result = draft_message(make_payment(), "send_reminder", "hi-en")
    assert result.fallback_used is True
    assert result.language_used == "hi-en"
    assert "nahi ho paya" in result.message  # from the Hinglish fallback template


@patch.dict(os.environ, {}, clear=True)
def test_unrecognized_language_pref_falls_back_to_english_template():
    # defensive: if language_pref is somehow neither "en" nor "hi-en"
    result = draft_message(make_payment(), "send_reminder", "fr")
    assert result.fallback_used is True
    assert result.language_used == "en"  # defaults safely rather than crashing


# ---------------------------------------------------------------------------
# 3. Dark pattern detection -- the core safety guardrail for this module.
#    Even if the system prompt fails to prevent it, this must catch it.
# ---------------------------------------------------------------------------

def test_contains_dark_pattern_detects_known_phrases():
    for phrase in DARK_PATTERN_PHRASES:
        text = f"Some message with {phrase} embedded in it."
        assert _contains_dark_pattern(text) == phrase


def test_contains_dark_pattern_case_insensitive():
    assert _contains_dark_pattern("LAST CHANCE to fix your payment!") == "last chance"


def test_contains_dark_pattern_returns_none_for_clean_text():
    assert _contains_dark_pattern("Your payment didn't go through. Please retry when convenient.") is None


@patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"})
@patch("core.messaging_agent._get_client")
def test_dark_pattern_in_model_output_triggers_fallback(mock_get_client):
    mock_client = mock_get_client.return_value
    mock_client.models.generate_content.return_value = make_mock_response(
        message="Last chance! Act now or your account may be affected.",
        language_used="en",
    )

    result = draft_message(make_payment(), "send_reminder", "en")

    assert result.fallback_used is True
    assert "pressure language" in result.fallback_reason.lower()
    assert "last chance" not in result.message.lower()  # fallback template used instead


# ---------------------------------------------------------------------------
# 4. Length guardrail -- trims rather than rejects
# ---------------------------------------------------------------------------

@patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"})
@patch("core.messaging_agent._get_client")
def test_overlong_message_gets_trimmed_not_rejected(mock_get_client):
    mock_client = mock_get_client.return_value
    long_message = "This payment reminder is unnecessarily verbose. " * 20  # way over limit
    mock_client.models.generate_content.return_value = make_mock_response(
        message=long_message, language_used="en",
    )

    result = draft_message(make_payment(), "send_reminder", "en")

    assert result.fallback_used is False  # trimmed, not treated as a failure
    assert len(result.message) <= MAX_MESSAGE_CHARS + 3  # +3 for "..." suffix slack
    assert result.message.endswith("...")


# ---------------------------------------------------------------------------
# 5. Empty message from model -- fails safe
# ---------------------------------------------------------------------------

@patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"})
@patch("core.messaging_agent._get_client")
def test_empty_message_falls_back(mock_get_client):
    mock_client = mock_get_client.return_value
    mock_client.models.generate_content.return_value = make_mock_response(
        message="   ", language_used="en",  # whitespace-only
    )

    result = draft_message(make_payment(), "send_reminder", "en")

    assert result.fallback_used is True
    assert result.message.strip()  # fallback still gives real content


# ---------------------------------------------------------------------------
# 6. Schema validation failure (response.parsed is None)
# ---------------------------------------------------------------------------

@patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"})
@patch("core.messaging_agent._get_client")
def test_unparseable_response_falls_back(mock_get_client):
    mock_client = mock_get_client.return_value
    mock_client.models.generate_content.return_value = SimpleNamespace(
        parsed=None, text="not valid json at all"
    )

    result = draft_message(make_payment(), "send_reminder", "en")

    assert result.fallback_used is True
    assert "did not parse" in result.fallback_reason.lower()


# ---------------------------------------------------------------------------
# 7. API errors -- retried, then fail safe
# ---------------------------------------------------------------------------

@patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"})
@patch("core.messaging_agent.time.sleep", return_value=None)
@patch("core.messaging_agent._get_client")
def test_persistent_api_error_retries_then_falls_back(mock_get_client, mock_sleep):
    mock_client = mock_get_client.return_value
    mock_client.models.generate_content.side_effect = Exception("quota exceeded")

    result = draft_message(make_payment(), "send_reminder", "en")

    assert result.fallback_used is True
    assert "quota exceeded" in result.fallback_reason
    assert mock_client.models.generate_content.call_count == MAX_API_RETRIES


@patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"})
@patch("core.messaging_agent.time.sleep", return_value=None)
@patch("core.messaging_agent._get_client")
def test_transient_error_then_success_recovers(mock_get_client, mock_sleep):
    mock_client = mock_get_client.return_value
    mock_client.models.generate_content.side_effect = [
        Exception("temporary blip"),
        make_mock_response(message="Recovered message after retry.", language_used="en"),
    ]

    result = draft_message(make_payment(), "send_reminder", "en")

    assert result.fallback_used is False
    assert "Recovered message" in result.message


# ---------------------------------------------------------------------------
# 8. Amount formatting helper
# ---------------------------------------------------------------------------

def test_format_amount_formats_with_rupee_symbol_and_commas():
    assert _format_amount(15000) == "\u20b915,000.00"
    assert _format_amount(99.5) == "\u20b999.50"


def test_format_amount_handles_invalid_input_gracefully():
    assert _format_amount(None) == "the payment amount"
    assert _format_amount("not a number") == "the payment amount"


# ---------------------------------------------------------------------------
# 9. Batch processing
# ---------------------------------------------------------------------------

@patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"})
@patch("core.messaging_agent._get_client")
def test_batch_matches_lists_by_position(mock_get_client):
    mock_client = mock_get_client.return_value
    mock_client.models.generate_content.return_value = make_mock_response()

    payments = [make_payment(payment_id="txn_1"), make_payment(payment_id="txn_2")]
    actions = ["send_reminder", "prompt_method_switch"]
    langs = ["en", "hi-en"]

    results = draft_batch(payments, actions, langs)

    assert len(results) == 2
    assert results[0].payment_id == "txn_1"
    assert results[1].payment_id == "txn_2"


# ---------------------------------------------------------------------------
# Test runner (in case pytest isn't installed in this environment)
# ---------------------------------------------------------------------------

def _run_all():
    import traceback

    tests = [obj for name, obj in list(globals().items())
              if name.startswith("test_") and callable(obj)]

    passed, failed = 0, 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {test.__name__}: {e}")
            failed += 1
        except Exception:
            print(f"  ERROR {test.__name__}:")
            traceback.print_exc()
            failed += 1

    print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
    return failed == 0


if __name__ == "__main__":
    success = _run_all()
    sys.exit(0 if success else 1)
