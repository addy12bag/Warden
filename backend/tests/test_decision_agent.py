"""
Unit tests for backend/core/decision_agent.py (Stage 4).

These tests mock the Groq client entirely -- no real API key or network
call is required to run them. This is intentional: the highest-risk
part of this module is its FAIL-SAFE behavior (malformed output,
out-of-set actions, API errors, missing key), not the happy path,
since a live model call is inherently non-deterministic and can't be
unit-tested for exact output anyway.

A separate manual/live smoke test script (see manual_live_smoke_test.py
in this same directory) is provided for verifying real Groq
connectivity once a GROQ_API_KEY is available -- run that separately,
outside the automated suite.

Run with:
    cd backend && python -m pytest tests/test_decision_agent.py -v

Or without pytest installed:
    cd backend && python tests/test_decision_agent.py
"""

import sys
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.decision_agent import (
    decide_action,
    decide_batch,
    ALLOWED_ACTIONS,
    DECISION_TOOL_SCHEMA,
    MAX_API_RETRIES,
    GROQ_MODEL_CHAIN,
)


def make_classification(root_cause="gateway_timeout", error_category="transient",
                         is_retryable=True, recommended_action="retry_now"):
    return {
        "root_cause": root_cause,
        "error_category": error_category,
        "is_retryable": is_retryable,
        "recommended_primary_action": recommended_action,
        "reasoning": "test reasoning from classifier",
    }


def make_payment(payment_id="txn_test", amount=500.0, retry_count=0):
    return {
        "id": payment_id,
        "amount": amount,
        "payment_method": "card",
        "retry_count": retry_count,
        "customer_tenure_days": 100,
        "language_pref": "en",
    }


def make_mock_response(chosen_action="retry_now", reasoning="looks transient, retry"):
    """Builds a fake Groq response object matching the OpenAI-compatible
    tool-calling response shape, so decide_action() can parse it exactly
    as it would a real one."""
    tool_call = SimpleNamespace(
        function=SimpleNamespace(
            name="submit_recovery_decision",
            arguments=json.dumps({"chosen_action": chosen_action, "reasoning": reasoning}),
        )
    )
    message = SimpleNamespace(tool_calls=[tool_call])
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


# ---------------------------------------------------------------------------
# 1. Happy path -- valid tool call, valid action
# ---------------------------------------------------------------------------

@patch.dict(os.environ, {"GROQ_API_KEY": "fake-key-for-test"})
@patch("core.decision_agent._get_client")
def test_valid_response_returns_chosen_action_and_reasoning(mock_get_client):
    mock_client = mock_get_client.return_value
    mock_client.chat.completions.create.return_value = make_mock_response(
        chosen_action="retry_now", reasoning="transient failure, safe to retry immediately"
    )

    classification = make_classification()
    payment = make_payment()
    result = decide_action(classification, payment)

    assert result.chosen_action == "retry_now"
    assert "transient" in result.reasoning
    assert result.fallback_used is False
    assert result.payment_id == "txn_test"


@patch.dict(os.environ, {"GROQ_API_KEY": "fake-key-for-test"})
@patch("core.decision_agent._get_client")
def test_all_allowed_actions_accepted_when_returned(mock_get_client):
    mock_client = mock_get_client.return_value
    for action in ALLOWED_ACTIONS:
        mock_client.chat.completions.create.return_value = make_mock_response(chosen_action=action)
        result = decide_action(make_classification(), make_payment())
        assert result.chosen_action == action
        assert result.fallback_used is False


# ---------------------------------------------------------------------------
# 2. Missing API key -- fails safe without ever attempting a call
# ---------------------------------------------------------------------------

@patch.dict(os.environ, {}, clear=True)
def test_missing_api_key_fails_safe_to_escalate():
    result = decide_action(make_classification(), make_payment(payment_id="txn_no_key"))
    assert result.chosen_action == "escalate"
    assert result.fallback_used is True
    assert result.payment_id == "txn_no_key"
    assert "GROQ_API_KEY" in result.reasoning


# ---------------------------------------------------------------------------
# 3. Model returns an out-of-set action -- must not be trusted even
#    though it came back as valid JSON from a tool call. This is the
#    critical defense-in-depth check: schema enforcement at the API
#    layer is not assumed to be perfect, so Python re-validates.
# ---------------------------------------------------------------------------

@patch.dict(os.environ, {"GROQ_API_KEY": "fake-key-for-test"})
@patch("core.decision_agent._get_client")
def test_out_of_set_action_fails_safe_to_escalate(mock_get_client):
    mock_client = mock_get_client.return_value
    mock_client.chat.completions.create.return_value = make_mock_response(
        chosen_action="refund_immediately",  # not in ALLOWED_ACTIONS
        reasoning="hallucinated action",
    )

    result = decide_action(make_classification(), make_payment())
    assert result.chosen_action == "escalate"
    assert result.fallback_used is True
    assert "refund_immediately" in result.reasoning


# ---------------------------------------------------------------------------
# 4. Model does not call the tool at all (plain text response instead)
# ---------------------------------------------------------------------------

@patch.dict(os.environ, {"GROQ_API_KEY": "fake-key-for-test"})
@patch("core.decision_agent._get_client")
def test_no_tool_call_fails_safe_to_escalate(mock_get_client):
    mock_client = mock_get_client.return_value
    message = SimpleNamespace(tool_calls=None)
    choice = SimpleNamespace(message=message)
    mock_client.chat.completions.create.return_value = SimpleNamespace(choices=[choice])

    result = decide_action(make_classification(), make_payment())
    assert result.chosen_action == "escalate"
    assert result.fallback_used is True


# ---------------------------------------------------------------------------
# 5. Malformed JSON in tool call arguments
# ---------------------------------------------------------------------------

@patch.dict(os.environ, {"GROQ_API_KEY": "fake-key-for-test"})
@patch("core.decision_agent._get_client")
def test_malformed_json_fails_safe_to_escalate(mock_get_client):
    mock_client = mock_get_client.return_value
    tool_call = SimpleNamespace(
        function=SimpleNamespace(name="submit_recovery_decision", arguments="{not valid json")
    )
    message = SimpleNamespace(tool_calls=[tool_call])
    choice = SimpleNamespace(message=message)
    mock_client.chat.completions.create.return_value = SimpleNamespace(choices=[choice])

    result = decide_action(make_classification(), make_payment())
    assert result.chosen_action == "escalate"
    assert result.fallback_used is True


# ---------------------------------------------------------------------------
# 6. API errors -- retried up to MAX_API_RETRIES, then fail safe
# ---------------------------------------------------------------------------

@patch.dict(os.environ, {"GROQ_API_KEY": "fake-key-for-test"})
@patch("core.decision_agent.time.sleep", return_value=None)
@patch("core.decision_agent._get_client")
def test_first_model_rate_limited_falls_back_to_second_model_successfully(mock_get_client, mock_sleep):
    mock_client = mock_get_client.return_value
    # First model: rate limited. Second model: succeeds.
    mock_client.chat.completions.create.side_effect = [
        Exception("Rate limit reached for model `openai/gpt-oss-20b`... 429"),
        make_mock_response(chosen_action="retry_now", reasoning="recovered via fallback model"),
    ]

    result = decide_action(make_classification(), make_payment())

    assert result.chosen_action == "retry_now"
    assert result.fallback_used is False  # this IS a real model success, not the escalate fallback
    assert "recovered via fallback model" in result.reasoning
    assert mock_client.chat.completions.create.call_count == 2

    # Confirm the second call actually used the second model in the chain
    second_call_kwargs = mock_client.chat.completions.create.call_args_list[1].kwargs
    assert second_call_kwargs["model"] == GROQ_MODEL_CHAIN[1]


@patch.dict(os.environ, {"GROQ_API_KEY": "fake-key-for-test"})
@patch("core.decision_agent.time.sleep", return_value=None)
@patch("core.decision_agent._get_client")
def test_persistent_rate_limit_tries_every_model_in_chain_then_fails_safe(mock_get_client, mock_sleep):
    mock_client = mock_get_client.return_value
    mock_client.chat.completions.create.side_effect = Exception("rate limit exceeded")

    result = decide_action(make_classification(), make_payment())

    assert result.chosen_action == "escalate"
    assert result.fallback_used is True
    assert "rate limit" in result.reasoning
    # rate-limit errors should NOT burn all MAX_API_RETRIES against one
    # exhausted model -- they should move to the next model in the
    # chain after a single failed attempt per model.
    assert mock_client.chat.completions.create.call_count == len(GROQ_MODEL_CHAIN)


@patch.dict(os.environ, {"GROQ_API_KEY": "fake-key-for-test"})
@patch("core.decision_agent.time.sleep", return_value=None)
@patch("core.decision_agent._get_client")
def test_persistent_non_rate_limit_error_retries_within_model_then_moves_on(mock_get_client, mock_sleep):
    mock_client = mock_get_client.return_value
    mock_client.chat.completions.create.side_effect = Exception("connection timeout")

    result = decide_action(make_classification(), make_payment())

    assert result.chosen_action == "escalate"
    assert result.fallback_used is True
    # non-rate-limit errors DO retry MAX_API_RETRIES times per model
    # before moving on, since they may be transient and worth retrying
    # against the same model.
    assert mock_client.chat.completions.create.call_count == MAX_API_RETRIES * len(GROQ_MODEL_CHAIN)


@patch.dict(os.environ, {"GROQ_API_KEY": "fake-key-for-test"})
@patch("core.decision_agent.time.sleep", return_value=None)
@patch("core.decision_agent._get_client")
def test_transient_error_then_success_recovers_without_fallback(mock_get_client, mock_sleep):
    mock_client = mock_get_client.return_value
    mock_client.chat.completions.create.side_effect = [
        Exception("temporary network blip"),
        make_mock_response(chosen_action="escalate", reasoning="recovered on retry"),
    ]

    result = decide_action(make_classification(), make_payment())

    assert result.chosen_action == "escalate"
    assert result.fallback_used is False  # recovered, not a fallback
    assert mock_client.chat.completions.create.call_count == 2


# ---------------------------------------------------------------------------
# 7. Tool schema sanity -- catches accidental drift between the schema
#    and ALLOWED_ACTIONS if someone edits one without the other
# ---------------------------------------------------------------------------

def test_tool_schema_enum_matches_allowed_actions_exactly():
    schema_enum = DECISION_TOOL_SCHEMA["function"]["parameters"]["properties"]["chosen_action"]["enum"]
    assert set(schema_enum) == set(ALLOWED_ACTIONS)


def test_tool_schema_requires_both_fields():
    required = DECISION_TOOL_SCHEMA["function"]["parameters"]["required"]
    assert "chosen_action" in required
    assert "reasoning" in required


# ---------------------------------------------------------------------------
# 8. Batch processing -- one failure doesn't stop the batch
# ---------------------------------------------------------------------------

@patch.dict(os.environ, {"GROQ_API_KEY": "fake-key-for-test"})
@patch("core.decision_agent._get_client")
def test_batch_continues_past_individual_failures(mock_get_client):
    mock_client = mock_get_client.return_value
    mock_client.chat.completions.create.side_effect = [
        make_mock_response(chosen_action="retry_now"),
        Exception("boom"),  # this one fails all retries internally... but only 1 call here
    ]
    # Note: to keep this test fast/deterministic we only give 2 responses
    # total, relying on MAX_API_RETRIES retry loop for the second payment
    # to exhaust quickly against repeated the same exception via side_effect
    # cycling isn't supported by Mock, so instead verify partial-batch
    # behavior structurally: batch calls decide_action per item, and a
    # single item's internal failure surfaces as a fallback result, not
    # a raised exception that would kill the whole batch.

    classifications = [make_classification(), make_classification()]
    payments = [make_payment(payment_id="txn_1"), make_payment(payment_id="txn_2")]

    with patch("core.decision_agent.time.sleep", return_value=None):
        results = decide_batch(classifications, payments)

    assert len(results) == 2
    assert results[0].payment_id == "txn_1"
    assert results[1].payment_id == "txn_2"
    # first succeeded, second exhausted retries and fell back -- batch
    # itself did not raise
    assert results[0].chosen_action == "retry_now"
    assert results[1].fallback_used is True


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
