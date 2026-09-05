# Warden — AI Revenue Recovery Agent

**Razorpay AI Builder Internship 2026 — Track 03: AI Revenue Recovery**

Warden is an agent that treats payment failure recovery as a governed decision problem, not a blind retry loop. It diagnoses *why* a payment failed, lets a bounded LLM propose the right response, and enforces hard safety rules that no model — however persuasive — is allowed to override.

> **Result on a real, live-processed batch of 500 payments:** 37.8% recovery rate, ₹13,67,740 recovered, and a **0% false-retry rate** — verified against a hidden ground truth the agent never saw.

---

## The problem

Most payment retry systems are blind: they retry every failure the same number of times, regardless of cause. A network timeout and an expired card look identical as "a failed payment," but they need opposite responses — retrying an expired card wastes money and risks card-network penalties; giving up too early on a timeout abandons genuinely recoverable revenue.

Soft declines (insufficient funds, timeouts, velocity limits) make up 70–90% of card-not-present failures and are usually recoverable with the right timing. Hard declines (expired cards, stolen cards, fraud blocks) are permanent, and card-network rules actively restrict blind retrying — making cause-aware, rule-bounded recovery a genuine compliance requirement, not just good engineering.

Warden diagnoses each failure, chooses a bounded action, and never lets automated judgment override hard safety limits.

## Objectives

1. **Detect** — recognize payment degradation and failure events from transaction metadata
2. **Diagnose** — classify the true root cause deterministically and explainably
3. **Decide** — use a bounded LLM layer to select the correct intervention per cause
4. **Act within limits** — enforce hard stopping rules that no model output can override
5. **Recover** — execute the chosen action and track whether revenue was actually recovered
6. **Measure** — report recovery rate, money recovered, and an honest list of unresolved cases
7. **Explain** — maintain a full audit trail: every decision traceable to what was seen, diagnosed, chosen, and why

## Architecture

```
Failed payment
      │
      ▼
Root-cause classifier   (deterministic — rules, not a model)
      │
      ▼
┌─────────────────────────────────────────┐
│           Agent core (bounded)           │
│                                           │
│  LLM decision layer  →  Messaging agent   │
│  (Groq, tool-calling)   (Gemini, guarded) │
│                                           │
│           Stopping rules                  │
│  (independent second gate — final say)    │
└─────────────────────────────────────────┘
      │
      ▼
Executor → Evaluation → Dashboard
```

**The core design principle:** deterministic where a decision must be safe, probabilistic where judgment adds value, and a hard veto layer in between that neither side can bypass.

- The **classifier** is a rules engine, not a model — retryability for a given error code is never left to chance.
- The **LLM decision layer** only chooses *which* bounded action fits the context (amount, retry history, customer tenure) — it never decides retryability itself.
- **Stopping rules** independently re-check the LLM's actual chosen action against limits the classifier can't see — retry caps, cooldown windows, a permanent block list — and can force an override regardless of how sound the LLM's reasoning sounds.

This was proven, not just designed: in testing, a simulated LLM that ignored the classifier's recommendation 15% of the time was caught 100% of the time by the stopping-rules gate.

### Root-cause → action reference table

| Error code | Category | Retryable? | Primary action |
|---|---|---|---|
| gateway_timeout, network_drop | transient | yes | retry_now (short delay) |
| insufficient_funds | soft_decline | yes (delayed) | retry_delayed + reminder |
| do_not_honor | soft_decline | yes (limited) | retry_delayed once, then escalate |
| velocity_limit | soft_decline | yes (delayed) | retry_delayed |
| invalid_cvv | user_error | limited | prompt_method_switch |
| expired_card | hard_decline | no | prompt_method_switch |
| card_stolen_lost | hard_decline | no | escalate |
| account_closed | hard_decline | no | no_action |
| restricted_card | hard_decline | no | escalate |
| risk_block | compliance_block | no | escalate (never auto-retry) |

The LLM applies this table with contextual judgment — it never overrides the hard "no" cases.

## What it measures — and why the numbers are trustworthy

| Metric | Result (500-payment batch) |
|---|---|
| Recovery rate | 37.8% (189 / 500 decisions) |
| Money recovered | ₹13,67,740.24 of ₹33,85,520.97 |
| **False-retry rate** | **0.0%** |
| Exceptions (unresolved, reported in full) | 140 |
| Stopping-rule overrides fired | 30 |

The **false-retry rate** — how often the agent retried a payment that a hidden ground-truth label says was actually unrecoverable — is the project's core integrity check. It's computed by [`evaluate.py`](backend/evaluate.py), the only script permitted to read the ground-truth file, and held at 0% across independent test runs at 20, 100, and 500 records — consistent across sample sizes, not a lucky single run.

Warden deliberately does **not** report a fabricated "time to recovery" metric — since this is a simulated batch with no real elapsed recovery process, doing so would be dishonest. It reports "time to decision" instead, and says so explicitly in the code and output.

## Tech stack

| Layer | Choice |
|---|---|
| Backend | Python, FastAPI |
| Decision LLM | Groq (`openai/gpt-oss-20b`, with automatic fallback to alternate models on quota exhaustion) |
| Messaging LLM | Gemini (`gemini-flash-lite-latest`), with dark-pattern-phrase guardrails checked in code |
| Database | SQLite, with a real foreign-key constraint enforcing that every decision references a valid payment |
| Frontend | React, Vite, Tailwind — a dense, sortable ops-console dashboard, not a static report |
| Testing | pytest — **118 automated tests** across every module |

## Repository structure

```
backend/
├── core/
│   ├── classifier.py       # deterministic root-cause classification
│   ├── stopping_rules.py   # independent safety gate, final say
│   ├── decision_agent.py   # Groq LLM decision layer, tool-calling
│   ├── messaging_agent.py  # Gemini messaging agent, guarded output
│   └── executor.py         # orchestrates the full pipeline per payment
├── db/                      # SQLite schema + connection management
├── data/                    # synthetic dataset generator + payments
├── tests/                   # 118 tests across every module
├── main.py                  # FastAPI (read-only) API layer
├── evaluate.py               # ground-truth evaluation
└── run_full_batch.py         # runs the full pipeline end to end

frontend/
└── src/
    ├── components/           # StatRail, DataTable, DetailPanel, ExceptionsBar
    └── App.jsx
```

## Running it locally

### Backend

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# fill in GROQ_API_KEY and GEMINI_API_KEY
```

```bash
# verify everything works (no API calls, ~1 second)
python -m pytest tests/ -v
# expect: 118 passed

# run the full pipeline (makes real Groq + Gemini API calls)
python run_full_batch.py --limit 20 --fresh   # small test first
python run_full_batch.py --fresh              # full 500-record batch

# compute final numbers against hidden ground truth
python evaluate.py

# start the API server
python -m uvicorn main:app --reload
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open **http://localhost:5173**. The dashboard is read-only against `backend/db/recovery_agent.db` — it never triggers a live batch run itself, so it's safe to leave open without consuming API quota.

## Design notes worth knowing

- **The API is deliberately read-only for pipeline execution.** There is no endpoint that triggers a live batch run — an accidental page refresh must never silently burn API quota. Running the agent stays an explicit CLI action.
- **Structured LLM output, not text parsing.** The Groq decision layer uses tool calling with a schema that hard-enums the six allowed actions; the Gemini messaging layer uses native JSON schema mode. Both are re-validated in code regardless, since no single layer is trusted alone.
- **Every fail-safe path is tested, not assumed.** Missing API keys, malformed model output, out-of-set actions, and persistent API errors all have dedicated tests proving the system degrades to a safe `escalate` rather than crashing or producing broken output.
- **Hinglish and UPI-mandate-aware messaging are agent features, not side projects** — folded into the core messaging agent rather than built as separate, scope-creeping deliverables.

## Real incidents encountered and resolved

Built and stress-tested against genuine, live conditions rather than a clean happy path:

- **Model deprecation mid-project** — the original Groq model (`llama-3.1-8b-instant`) was deprecated during development; switched to `openai/gpt-oss-20b`, and added an automatic multi-model fallback chain so a future deprecation or quota exhaustion doesn't stop the pipeline.
- **Daily quota exhaustion mid-batch** — hit Groq's per-model daily token limit partway through a 500-record run; the fallback chain (querying the account's *actual* live model list, not guessed names) resolved it without manual intervention.
- **Gemini API evolution** — encountered a deprecated model, a parameter-scheme change between model generations (`thinking_budget` → `thinking_level`), and a free-tier quota wall, all within Stage 5 alone. Every one of these was absorbed by the fail-safe fallback-template design — the system kept producing safe, real output throughout, never a crash or a broken message.

In every case, the fail-safe design did exactly its job: degrade to a safe `escalate` rather than break, while the underlying cause was fixed.

## License

MIT
