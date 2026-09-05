# Payment Degradation → Root Cause → Recovery Agent

**Track:** AI Revenue Recovery (Track 03) — Razorpay AI Builder Internship 2026
**Sub-direction:** Payment degradation → root cause → recovery action, with Hinglish messaging and mandate retry logic folded in as agent features

---

## 1. Overview

Payment failures are one of the most direct, quantifiable revenue leaks a payments company faces. Most retry systems today are blind — they retry every failure the same number of times regardless of *why* it failed. This wastes money on hopeless retries (annoying customers, incurring gateway fees, risking card-network penalties) and simultaneously under-recovers cases that would have succeeded with the right timing or approach.

This project builds an **agent that treats payment recovery as a governed decision problem**, not a blind loop: diagnose the true cause of a failure, choose the correct bounded intervention for that specific cause, execute it within hard safety limits, and report honest, measured outcomes.

---

## 2. Objectives

| # | Objective | Description |
|---|---|---|
| 1 | **Detect** | Recognize payment degradation and failure events from transaction metadata |
| 2 | **Diagnose** | Classify the true root cause deterministically and explainably |
| 3 | **Decide** | Use an LLM-driven layer to select the correct intervention per cause, from a bounded action set |
| 4 | **Act within limits** | Enforce hard stopping rules — retry caps, cooldown windows, permanent blocks for non-retryable causes |
| 5 | **Recover** | Execute the chosen action (simulated) and track whether revenue was actually recovered |
| 6 | **Measure** | Report recovery rate, money recovered, time-to-recovery, and an honest list of unresolved cases |
| 7 | **Explain** | Maintain a full audit trail — every decision traceable to what was seen, diagnosed, chosen, and why |

### Success bar (per Razorpay's stated criteria)
> "Don't just identify the problem. Show measured money recovered across a batch, with compliant escalation, stopping rules, and an audit trail."

This project is designed to hit every clause of that sentence directly, not just the general theme.

---

## 3. Why This Problem Matters

- Soft declines (insufficient funds, gateway timeouts, velocity limits) account for **70–90% of all card-not-present payment failures** and are usually recoverable if retried correctly.
- Hard declines (expired cards, stolen cards, closed accounts, fraud blocks) are **permanent** — retrying them is not just wasteful but can trigger card-network penalties for the merchant.
- Card network rules (Visa/Mastercard/RBI/UPI mandates) actively **restrict blind retrying** — this makes root-cause-aware, rule-bounded recovery a genuine compliance requirement, not just good engineering practice.
- A cause-aware, governed agent is the difference between "looks like AI" and "a system a real payments company could actually trust with money-moving decisions."

---

## 4. System Architecture

```
Failed payment event
        ↓
Root-cause classifier (deterministic, rule-based, explainable)
        ↓
┌─────────────────────────────────────────────┐
│              Agent core (bounded)            │
│                                               │
│   LLM decision layer  →  Messaging agent      │
│   (picks intervention)   (drafts recovery     │
│                            message, incl.     │
│                            Hinglish)          │
│                                               │
│              Stopping rules                   │
│   (max retries, cooldowns, hard blocks,       │
│    auto-escalation thresholds)                │
└─────────────────────────────────────────────┘
        ↓
Action executor (simulated retry / prompt / escalate)
        ↓
Outcome logger (recovered / still failing / escalated)
        ↓
Dashboard (recovery rate, money recovered, audit trail, exceptions)
```

### Design principle: split deterministic and probabilistic reasoning
- **Root-cause classification is deterministic** (rules/lookup table) — auditable, explainable, non-negotiable for known hard-decline codes.
- **Intervention choice and messaging are LLM-driven** — this is where judgment and natural language generation add real value, constrained to a bounded action set.
- **Stopping rules are hard-coded and override the LLM** — the agent can never retry a risk-blocked or expired-card payment, regardless of what the LLM "decides."

This split is the core defensibility argument for the project: the dangerous decisions are never left to a probabilistic model.

---

## 4a. Folder Structure

```
revenue-recovery-agent/
├── README.md                       Quick-start pointer to this document
├── PROJECT.md                      This file — full specification
├── .gitignore
│
├── backend/
│   ├── main.py                     FastAPI entrypoint, dashboard REST API (Stage 8 backing)
│   ├── evaluate.py                 Stage 7 — evaluation against hidden ground truth
│   ├── requirements.txt
│   ├── .env.example                Template for GROQ_API_KEY / GEMINI_API_KEY
│   │
│   ├── core/
│   │   ├── classifier.py           Stage 2 — deterministic root-cause classifier
│   │   ├── stopping_rules.py       Stage 3 — hard limits, final veto over the LLM
│   │   ├── decision_agent.py       Stage 4 — Groq LLM decision layer
│   │   ├── messaging_agent.py      Stage 5 — Gemini messaging agent (incl. Hinglish)
│   │   └── executor.py             Stage 6 — action execution + outcome logging
│   │
│   ├── db/
│   │   ├── models.py                SQLite schema (payments, decisions)
│   │   └── db.py                    Connection + setup helpers
│   │
│   └── data/
│       ├── generate_synthetic.py   Stage 1 — synthetic dataset generator
│       ├── payments.csv            Agent-facing batch (no ground truth)
│       └── payments_ground_truth.csv   Evaluation-only, hidden label
│
└── frontend/
    ├── package.json
    └── src/
        └── App.jsx                 Stage 8 — dashboard shell
```

**Note on `evaluate.py`**: this lives at `backend/` root rather than inside `core/`, deliberately — it is the *only* script permitted to read `payments_ground_truth.csv`. Keeping it outside `core/` makes that boundary visually obvious and harder to violate by accident.

## 5. Tech Stack

| Layer | Choice | Reasoning |
|---|---|---|
| Backend | Python + FastAPI | Best ecosystem for data generation, classical ML, and agent frameworks |
| Database | SQLite | Zero-setup, file-based, sufficient for batch-scale hackathon data |
| Decision LLM | Groq (Llama 3.3 70B / 3.1 8B) | Free tier, fast, strong at constrained structured decision-making |
| Messaging LLM | Gemini (gemini-2.0-flash) | Free tier, strong multilingual/natural generation, good fit for Hinglish |
| Frontend | React + Vite + Tailwind | Fast to build a clean ops dashboard, calls FastAPI via REST |

---

## 6. Data Model

### `payments` — synthetic batch (agent-facing, no ground truth)
| Column | Type | Notes |
|---|---|---|
| id | TEXT (PK) | e.g. `txn_00001` |
| customer_id | TEXT | |
| amount | REAL | ₹ |
| payment_method | TEXT | card / upi / netbanking |
| error_code | TEXT | e.g. `insufficient_funds`, `expired_card` |
| retry_count | INTEGER | prior attempts |
| created_at | TIMESTAMP | |
| customer_tenure_days | INTEGER | |
| language_pref | TEXT | en / hi-en (Hinglish) |

### `decisions` — one row per agent decision (the audit trail)
| Column | Type | Notes |
|---|---|---|
| id | INTEGER (PK) | |
| payment_id | TEXT (FK) | |
| root_cause | TEXT | classifier output |
| is_retryable | BOOLEAN | classifier output |
| chosen_action | TEXT | retry_now / retry_delayed / prompt_method_switch / send_reminder / escalate / no_action |
| reasoning | TEXT | LLM's stated reasoning |
| message_sent | TEXT | nullable |
| stopping_rule_triggered | TEXT | nullable |
| outcome | TEXT | recovered / still_failing / escalated / pending |
| timestamp | TIMESTAMP | |

### `payments_ground_truth` — evaluation only, never fed to the agent
Same as `payments` plus `_ground_truth_recoverable` (boolean). Used only after the agent has made all decisions, to score accuracy.

---

## 7. Root-Cause → Action Reference Table

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

The LLM applies this table with contextual judgment (amount, tenure, existing retry count) and produces natural-language reasoning and messaging — it does not override the hard "no" cases.

---

## 8. Staged Roadmap

### Stage 0 — Problem framing and track selection ✅ complete
- Selected Track 3: AI Revenue Recovery
- Scoped to "payment degradation → root cause → recovery action," with Hinglish messaging and mandate retry logic as agent features rather than standalone projects

### Stage 1 — Synthetic dataset generation ✅ complete
- Built `generate_synthetic.py`
- Grounded error-code distribution in real-world decline-rate research (soft ~80%, hard ~20%, insufficient funds as largest single category)
- Produced two files: agent-facing (`payments.csv`, no ground truth) and evaluation-only (`payments_ground_truth.csv`, hidden recoverability label)
- Verified distribution matches intended real-world proportions

### Stage 2 — Deterministic root-cause classifier ✅ complete
- Implemented `classifier.py`: rule-based mapping from `error_code` + context → root cause + `is_retryable`
- Fully rule-based, no ML/LLM — every verdict is traceable to a named rule in `reasoning`
- Five override rules layered on top of the static `ROOT_CAUSE_TABLE`:
  1. `GLOBAL_MAX_RETRIES` — retry_count ≥ 3 forces escalation, any category
  2. `SOFT_DECLINE_ESCALATION_THRESHOLD` — soft_decline/transient with retry_count ≥ 2 escalates instead of retrying again
  3. `USER_ERROR_SINGLE_PROMPT` — invalid_cvv escalates after one failed retry rather than re-prompting
  4. `HARD_DECLINE_FLOOR` — hard_decline/compliance_block are never retryable, cannot be overridden upward
  5. `HIGH_VALUE_FLAG` — audit-only flag for soft declines ≥ ₹10,000, does not change the action
- 19 unit tests, all passing (`backend/tests/test_classifier.py`), covering full table coverage, every override rule, boundary conditions, and fail-loud behavior on unrecognized error codes
- Validated against the full 500-record synthetic batch: 35.4% escalate, 33.8% retry_delayed, 16.6% retry_now, 11.0% prompt_method_switch, 3.2% no_action; 136 cases escalated specifically due to retry-count context rather than raw cause

### Stage 3 — Stopping rules engine ✅ complete
- Implemented `stopping_rules.py` as an **independent second gate**, not a re-derivation of the classifier's logic — it validates the LLM decision layer's *actual chosen action* against hard safety limits the classifier cannot see
- Six rules, checked in fixed precedence order:
  1. `PERMANENT_BLOCK_LIST` — explicit payment/customer IDs barred from any retry/prompt action, independent of category (checked first, cannot be bypassed)
  2. `CLASSIFIER_NOT_RETRYABLE` — if Stage 2 says `is_retryable=False`, no retryable action passes regardless of what the LLM proposed
  3. `NEVER_RETRY_CATEGORY` — belt-and-suspenders check on `hard_decline`/`compliance_block`, independent of the `is_retryable` flag
  4. `MAX_RETRIES_EXCEEDED` — hard cap at `retry_count >= 3`, any category
  5. `AUTO_ESCALATE_THRESHOLD` — past `retry_count >= 2`, blocks *customer-facing prompts too*, not just retries — only `escalate`/`no_action` pass
  6. `COOLDOWN_WINDOW_ACTIVE` / `COOLDOWN_UNVERIFIABLE` — enforces `COOLDOWN_MINUTES=30` between attempts using wall-clock timing the classifier never sees; fails safe (blocks) if timing can't be verified rather than assuming it's fine
- 21 unit tests, all passing (`backend/tests/test_stopping_rules.py`), covering every rule independently, boundary conditions, and precedence ordering when multiple rules could fire on the same payment
- Integration-validated against the real 500-record batch with a simulated "misbehaving" LLM (ignoring the classifier's recommendation 15% of the time): the gate caught 100% of unsafe overrides — 28 blocked via `CLASSIFIER_NOT_RETRYABLE`, 12 via `AUTO_ESCALATE_THRESHOLD`, proving the veto holds even when the layer above it fails

### Stage 4 — LLM decision layer ✅ complete
- Implemented `decision_agent.py` using the Groq API (`openai/gpt-oss-20b` — Groq's official recommended migration target after `llama-3.1-8b-instant` was deprecated on 2026-06-17 and shut down around 2026-08-16; same speed/size tier as the original choice, still supports tool calling on the free tier)
- **Structured output via tool/function calling, not text parsing**: the model is given exactly one callable tool (`submit_recovery_decision`) with a JSON schema that enums the six allowed actions — Groq enforces the schema at generation time, so the model cannot emit an out-of-set action through this path
- **Defense in depth on top of schema enforcement**: the returned action is re-validated against `ALLOWED_ACTIONS` in Python regardless, since no single layer is trusted alone
- **Fails safe to `escalate`** on every failure mode: missing API key, no tool call returned, malformed JSON arguments, out-of-set action, or persistent API errors after `MAX_API_RETRIES` retries with linear backoff — a single payment's LLM failure never crashes or corrupts the batch
- The classifier's `is_retryable` verdict is passed in as an **authoritative constraint**, not a suggestion — the system prompt explicitly instructs the model to never choose a retry action when `is_retryable=False`; this is still independently re-checked by Stage 3's stopping rules regardless of what the LLM does
- 11 unit tests, all passing (`backend/tests/test_decision_agent.py`), using a fully mocked Groq client — deliberately weighted toward the fail-safe paths (missing key, malformed output, out-of-set actions, API errors, batch resilience) since those matter more than the happy path for a module whose core promise is "never breaks the batch"
- A separate manual live smoke-test script (`backend/tests/manual_live_smoke_test.py`, not part of the automated suite) is provided to verify real API connectivity once a `GROQ_API_KEY` is available
- **Live-verified against the real Groq API** (model: `openai/gpt-oss-20b`) across three representative cases: a transient failure correctly chose `retry_now` with sound reasoning; a hard-decline expired card correctly refused any retry action and chose `prompt_method_switch`, explicitly citing the classifier's non-retryable verdict; a soft decline already past the escalation threshold correctly chose `escalate`. The model's reasoning strings cite specific transaction context (amount, retry_count, tenure) rather than echoing the classifier verbatim, confirming genuine judgment within the bounded set rather than pass-through behavior
- Integration-validated: chained classifier → decision_agent → stopping_rules against real batch records end-to-end, confirming all three stages compose correctly

### Stage 5 — Messaging agent ✅ complete
- Implemented `messaging_agent.py` using the **current** `google-genai` SDK (`gemini-3.6-flash` — note: `gemini-2.5-flash` was the initial choice, verified against Google's own docs, but was found deprecated for new users during live testing on 2026-08-25; Google's own 404 error explicitly named `gemini-3.6-flash` as the replacement, confirming that even official docs pages can lag behind actual API behavior and live verification remains necessary) — note: the older `google-generativeai` package originally listed in requirements.txt was discovered to be fully deprecated (end-of-life 2025-11-30) during implementation and corrected before writing any code, avoiding a repeat of the Stage 4 model-deprecation issue
- **Structured output via Gemini's native JSON schema mode** (`response_mime_type="application/json"` + `response_schema=MessageOutputSchema`), mirroring Stage 4's tool-calling discipline — the model cannot return unstructured free text through this path, and the parsed output is re-validated in Python regardless
- **Guardrails enforced in code, not just prompted for**: a post-hoc dark-pattern phrase scanner (`DARK_PATTERN_PHRASES`) catches urgency/pressure language even if the system prompt instruction fails to prevent it; a hard length cap (`MAX_MESSAGE_CHARS=320`) trims rather than rejects overlong messages; empty messages trigger fallback rather than sending nothing
- **Hinglish support** via a dedicated language instruction in the system prompt (natural everyday Hinglish in Latin script, not formal Hindi or literal translation) and a distinct Hinglish fallback template
- **Mandate retry-sequence language**: when `payment_method == "upi"` and the chosen action is `retry_delayed`, the system prompt explicitly instructs the model to describe the retry as automatic (part of the mandate cycle) rather than asking the customer to manually retry — matches real UPI/NPCI mandate retry semantics
- **Fails safe to plain, calm, compliant fallback templates** (`FALLBACK_TEMPLATES`, in both English and Hinglish) on every failure mode — missing API key, schema parse failure, persistent API errors, or a detected dark pattern — since a customer-facing pipeline must never produce nothing, and the fallback message is deliberately the safest possible content if the LLM path is unavailable
- 17 unit tests, all passing (`backend/tests/test_messaging_agent.py`), covering the happy path, every fail-safe path, and specifically the dark-pattern and length guardrails since those are the highest-risk behaviors for a module whose output reaches real customers
- A separate manual live smoke-test script (`backend/tests/manual_live_smoke_test_messaging.py`) is provided for real API verification, including a UPI mandate-retry case to specifically check the "automatic retry" framing
- **Live-verified end-to-end**: the full Stage 2→3→4→5 pipeline was run against real batch records with real Groq and Gemini API calls (`backend/tests/verify_pipeline_end_to_end.py`). Groq's decision layer performed correctly across all cases (0 fallbacks, sound reasoning, correctly refused to retry a hard-decline expired card). This run also caught two real, sequential Gemini issues live, each fixed and documented as the actual reasoning behind current config choices:
  1. `gemini-2.5-flash` deprecated for new users → switched to `gemini-3.6-flash` per Google's own error message
  2. `gemini-3.6-flash`, a "thinking" model, was consuming the `max_output_tokens` budget on internal reasoning before writing the JSON response, causing `response.parsed` to come back `None` or truncated with a stray preamble — fixed via `thinking_config=ThinkingConfig(thinking_budget=0)`, since this drafting task needs no multi-step reasoning
  3. `gemini-3.6-flash`'s free tier carries only a 20-requests/day quota (confirmed via live `429 RESOURCE_EXHAUSTED` error, not just docs) — far too low for a hundreds-of-records batch job. Switched to `gemini-flash-lite-latest`, since Flash-Lite tiers consistently carry the most generous free daily quota (typically 1,000+ RPD) across current published limits. **This specific model name should be re-verified against the live model list for your account** (`curl "https://generativelanguage.googleapis.com/v1beta/models?key=$GEMINI_API_KEY"`) before trusting it for a full batch run, since Gemini model names/tiers changed twice already during this project alone
  Throughout all three issues, the fail-safe fallback templates worked exactly as designed — zero broken or empty messages were ever produced, even mid-outage, confirming the defense-in-depth design holds under real, unplanned failure conditions rather than just the failure modes anticipated in advance
- Integration-validated: chained classifier → decision_agent → stopping_rules → messaging_agent against real batch records end-to-end, confirming `ACTIONS_REQUIRING_MESSAGE` correctly gates which final actions trigger message drafting
- **Final clean live run**: after all four Gemini fixes above, `verify_pipeline_end_to_end.py` ran against 5 real payments with zero fallbacks across every stage — hard-decline (`expired_card`) correctly never retried and got a real drafted message prompting a method switch; two soft-decline cases produced real, natural-sounding Hinglish messages; two transient/first-attempt cases correctly skipped messaging entirely since `retry_now` isn't in `ACTIONS_REQUIRING_MESSAGE`. A harmless SDK warning about automatic function calling (AFC) appears in the console (`"Direct use of AFC in Models.generate_content is not recommended"`) — this does not affect correctness for this project's single-turn, stateless use case (no conversation history is needed), so it's left as-is rather than migrating to the SDK's `Chat` interface, but is noted here in case it resurfaces as an actual error in a future SDK version

### Stage 6 — Action executor + outcome logger ✅ complete
- Implemented `executor.py`: chains classifier → decision_agent → stopping_rules → messaging_agent → simulated execution → DB logging into a single `process_payment()` call, plus `run_batch()` for processing a full CSV
- Implemented `db/db.py`: SQLite connection management, table initialization, and CSV bulk-loading, with a safety check that raises a clear error (rather than silently loading garbage) if the CSV is missing expected columns — guards against accidentally pointing at `payments_ground_truth.csv` instead of the agent-facing `payments.csv`
- **Outcome simulation is probabilistic but grounded and reproducible**: `escalate`/`no_action` are always deterministic (`escalated`/`still_failing`); `retry_now`/`retry_delayed`/`prompt_method_switch`/`send_reminder` use base success probabilities (72%/58%/45%/30%) loosely ordered by how directly each intervention addresses the underlying cause, with a small tenure-based bonus for `prompt_method_switch` (long-tenure customers modeled as slightly more likely to respond). Each payment's outcome is seeded deterministically from its `payment_id`, so re-running the same batch reproduces identical results — important for demo reproducibility and debugging
- **`decisions` table has a real foreign-key constraint** on `payments.id`, enforced via `PRAGMA foreign_keys = ON` — the audit trail can never reference a payment that doesn't exist in the batch. This was caught by the test suite itself (8 initial test failures on `IntegrityError`), fixed by ensuring payments are always loaded before decisions are logged against them, matching how the real pipeline works (`load_payments_csv` runs before `run_batch`)
- **`run_batch()` is resilient to individual payment failures**: a malformed record (bad `error_code`) logs a `PROCESSING_ERROR`-tagged, safely-escalated decision record rather than crashing the whole batch — LLM-level failures never reach this path since they're already handled by fail-safe fallbacks inside Stages 4-5
- Combined `reasoning` field in each `DecisionRecord` concatenates classifier, LLM, and (when triggered) stopping-rules reasoning into one auditable string — anyone reading the `decisions` table can see the full chain of "why" without cross-referencing other tables
- 25 unit tests, all passing (9 in `test_db.py`, 16 in `test_executor.py`) — covering outcome-simulation determinism and distribution, FK enforcement, CSV-loading safety checks, and batch resilience to malformed records
- Structural end-to-end test against 50 real records from `payments.csv` (LLM calls mocked to avoid burning API quota during testing) confirmed the full pipeline runs cleanly with a realistic outcome spread (44% recovered / 28% still failing / 28% escalated)
- `run_full_batch.py` provided as the actual "run the real thing" script — loads the full CSV, runs every payment through the live pipeline (real Groq + Gemini calls), and prints outcome/action/stopping-rule distributions. Deliberately supports `--limit N` for a small test run first, since a full 500-record run makes 500-1000+ live API calls and should be quota-checked before running in full

### Stage 7 — Evaluation pass ✅ complete
- Implemented `evaluate.py`: joins `decisions` (itself joined with `payments` for amount/created_at) against the hidden `payments_ground_truth.csv` label — the only script in the whole project permitted to read that file
- **Recovery rate and money recovered** reported both overall and broken down by final `chosen_action`, so the headline percentage has honest context rather than floating alone
- **Time-to-decision, explicitly NOT time-to-recovery**: since this is a simulated batch with no real elapsed recovery process, the metric honestly measures time from payment failure (`created_at`) to agent decision (`decisions.timestamp`) — the code and console output both state this distinction explicitly, to prevent it from ever being misquoted as a real-world recovery duration in a pitch or write-up
- **False-retry rate as the core integrity check**: % of decisions that chose `retry_now`/`retry_delayed` on a payment ground truth marks as NOT recoverable — this is the single most important number in the whole evaluation, since it should sit at or near 0% if Stages 2-3's safety design (hard-decline floor, stopping rules) are genuinely working on real data, not just passing unit tests
- **Exceptions list is complete, never silently capped** — the returned result object contains every `still_failing`/`pending` case (console printing caps display at 20 for readability, but the underlying data is never truncated), directly matching the project's stated principle of reporting unresolved cases honestly rather than cherry-picking wins
- Fails loudly rather than silently on data-integrity problems: an empty decisions table, a decision referencing a `payment_id` absent from the ground-truth file, or accidentally pointing at the wrong CSV (missing the `_ground_truth_recoverable` column) all raise clear, specific errors rather than producing misleading output
- **Correctness verified against a hand-crafted dataset with known answers** before writing the automated test suite — a small 4-payment scenario with a deliberately-injected false retry, computed by hand and confirmed to match the script's output exactly, catching the false-retry detector working correctly on the very first real check
- 13 unit tests, all passing (`backend/tests/test_evaluate.py`), covering every metric against exact expected values (not just "did it run"), plus the three main failure modes (empty DB, mismatched files, wrong ground-truth file)

### Stage 8 — Dashboard ✅ complete (redesigned as an ops console)
- **Backend API layer (`main.py`)**: read-only FastAPI endpoints (`/api/health`, `/api/metrics`, `/api/decisions`, `/api/decisions/{payment_id}`, `/api/exceptions`) backed directly by the `payments`/`decisions` tables and the Stage 7 evaluation. Deliberately has **no endpoint that triggers a live batch run** — given the real Groq/Gemini quota limits discovered during Stage 5, an accidental page refresh hitting such an endpoint could silently burn API quota; running the pipeline stays an explicit CLI action (`run_full_batch.py`), and this API only ever reads what's already been written to the database
- **Design direction (v2, then relit)**: rebuilt from an initial paper-ledger concept into a dark, dense **ops console** — the kind of tool a payments/risk team would actually run daily (closer to Stripe Dashboard/Linear than a document). Later switched to a light palette on request: soft off-white surfaces (`#F7F8FA`/`#FFFFFF`), a confident indigo accent (`#4F46E5`, not generic SaaS blue), deep/muted status colors (green/amber/red kept dark enough to stay legible on white rather than washing out). Same structural design (rail + table + slide-over) carried through unchanged — only the token values changed, confirming the component tree was properly built on semantic color tokens rather than hardcoded values. Type: Inter for UI text, JetBrains Mono for all numeric/ID data
- **Layout**: persistent left stat rail (top-line metrics + click-to-filter by action) + dense sortable data table (main working surface) + slide-over detail panel (full audit trail on row select) + collapsible exceptions bar — the actual shape of a real internal triage tool, not a single scrolling page
- **Signature element**: the detail panel renders the audit trail as a legible causal chain (root cause diagnosis → agent reasoning → stopping-rule override, if any → message sent) in a slide-over rather than inline expansion, keeping the main table dense while still surfacing full reasoning on demand. A stopping-rule override gets a distinct bordered danger-colored block plus a small red dot on the table row itself, so overrides are visible before you even open the detail panel
- **Interactive filtering**: clicking any action in the stat rail filters the table live (verified working via headless-browser testing — caught and fixed a Playwright test-selector ambiguity, not an app bug, while confirming this)
- **Fully responsive**: rail becomes a slide-in overlay with a hamburger toggle below the `lg` breakpoint, detail panel becomes a full-width overlay on mobile, table columns collapse (hiding the action column, shrinking font/padding) rather than squeezing — verified via headless-browser screenshots at both 1440px and 390px (mobile) viewports, including the mobile rail-open interaction
- **Exceptions bar** collapsed by default (dense by default, expand on demand) and explicitly states it shows the full unresolved list, not a curated sample, directly echoing the project's stated design principle
- Frontend built as a real Vite + React + Tailwind project (not an inline artifact), matching the `frontend/` structure already committed to in PROJECT.md's folder layout, so it runs as a genuine local dev server against the FastAPI backend
- **Verified working end-to-end via real headless-browser screenshots** at every stage of the redesign, not just "the build succeeded" — confirmed real API data renders correctly, confirmed the detail panel opens/closes correctly, confirmed action filtering actually filters the table, confirmed mobile responsiveness including the rail overlay interaction, and confirmed the light-mode relit version at both desktop and mobile widths. This process caught a real bug: two `DetailPanel` instances were rendering simultaneously on desktop when a row was selected (the empty-state placeholder wasn't being suppressed), visible as overlapping/sliced text at the panel's right edge — fixed by making the selected/empty states mutually exclusive rather than both always mounted
- 10 unit tests, all passing (`backend/tests/test_main.py`), covering every endpoint's happy path, 404 handling, and the exceptions endpoint's filtering/ordering logic — bringing the full project total to **118 automated tests**

### Stage 9 — Packaging for submission
- Record measured results from Stage 7 with the fixed seed for reproducibility
- Write up the architecture decisions (why deterministic classifier + bounded LLM + hard stopping rules) as your defensibility narrative
- Record 5-minute pitch video: problem → architecture → live demo → measured numbers → what it couldn't fix (exceptions), honestly

---

## 9. Key Differentiators for the Panel

1. **Deterministic/LLM split** — dangerous decisions (never retry a risk-blocked payment) are hard-coded, not left to a probabilistic model. This is the single strongest technical defensibility point.
2. **Hidden ground truth evaluation** — recovery numbers are measured against a held-out label the agent never saw, not self-reported or cherry-picked.
3. **Honest exceptions reporting** — the dashboard surfaces what the agent *couldn't* fix, directly addressing Razorpay's explicit warning against showing only cherry-picked wins.
4. **Real-world grounded data** — synthetic dataset built on actual industry decline-rate research, not arbitrary percentages.
5. **Hinglish and mandate-retry logic as agent features**, not scope-creeping side-projects — keeps the submission coherent and focused on one system done well.

---

## 10. Open Risks / Things to Watch

- **LLM output reliability** — free-tier models can occasionally produce malformed structured output; the decision layer must validate against the bounded action set and fail safe (default to `escalate`) rather than crash or guess.
- **API rate limits** — Groq/Gemini free tiers have request limits; batch processing should include backoff/retry logic (ironic, but necessary) and the batch size should be tuned to what your quota allows before the deadline.
- **Time budget** — Stages 2–6 are the technical core; don't let dashboard polish (Stage 8) eat into evaluation rigor (Stage 7), since measured numbers matter more to the panel than visual polish.
