function formatCurrency(amount) {
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 2,
  }).format(amount);
}

const OUTCOME_STYLES = {
  recovered: "bg-success-bg text-success",
  escalated: "bg-warning-bg text-warning",
  still_failing: "bg-danger-bg text-danger",
  pending: "bg-warning-bg text-warning",
};

function parseReasoningChain(reasoning) {
  const parts = {};
  const matches = (reasoning || "").matchAll(/\[(\w+)\]\s*(.*?)(?=\s*\[\w+\]|$)/gs);
  for (const match of matches) {
    parts[match[1]] = match[2].trim();
  }
  return parts;
}

export default function DetailPanel({ decision, onClose }) {
  if (!decision) {
    return (
      <div className="w-full sm:w-96 shrink-0 border-l border-border bg-surface h-full flex items-center justify-center px-8">
        <p className="text-sm text-text-faint text-center">
          Select a row to see the full decision trail — root cause, agent reasoning, and any safety override.
        </p>
      </div>
    );
  }

  const chain = parseReasoningChain(decision.reasoning);
  const wasOverridden = Boolean(decision.stopping_rule_triggered);

  return (
    <div className="w-full sm:w-96 shrink-0 border-l border-border bg-surface h-full overflow-y-auto">
      <div className="sticky top-0 bg-surface border-b border-border px-5 py-4 flex items-start justify-between">
        <div>
          <div className="font-mono text-xs text-text-muted">{decision.payment_id}</div>
          <div className="text-lg font-semibold mt-0.5">{decision.root_cause}</div>
        </div>
        <button
          onClick={onClose}
          className="text-text-faint hover:text-text transition-colors focus-ring rounded p-1 -mr-1 -mt-1"
          aria-label="Close detail panel"
        >
          ✕
        </button>
      </div>

      <div className="px-5 py-4 space-y-4">
        <div className="flex items-center gap-3">
          <span
            className={`text-xs font-medium px-2.5 py-1 rounded ${
              OUTCOME_STYLES[decision.outcome] || "bg-surface-raised text-text-muted"
            }`}
          >
            {decision.outcome.replace("_", " ")}
          </span>
          <span className="font-mono text-sm text-text-muted">
            {formatCurrency(decision.amount)}
          </span>
        </div>

        {chain.classifier && (
          <div>
            <div className="text-[11px] font-medium uppercase tracking-wider text-text-faint mb-1.5">
              Root cause diagnosis
            </div>
            <p className="text-sm text-text-muted leading-relaxed">{chain.classifier}</p>
          </div>
        )}

        {chain.llm && (
          <div>
            <div className="text-[11px] font-medium uppercase tracking-wider text-text-faint mb-1.5">
              Agent reasoning
            </div>
            <p className="text-sm text-text-muted leading-relaxed">{chain.llm}</p>
          </div>
        )}

        {wasOverridden && chain.stopping_rules && (
          <div className="border border-danger/30 bg-danger-bg rounded-md px-3.5 py-3">
            <div className="text-[11px] font-medium uppercase tracking-wider text-danger mb-1.5 flex items-center gap-1.5">
              <span className="w-1.5 h-1.5 rounded-full bg-danger" />
              Safety override applied
            </div>
            <p className="text-sm text-text leading-relaxed">{chain.stopping_rules}</p>
          </div>
        )}

        {chain.executor && (
          <div className="border border-danger/30 bg-danger-bg rounded-md px-3.5 py-3">
            <div className="text-[11px] font-medium uppercase tracking-wider text-danger mb-1.5">
              Processing error
            </div>
            <p className="text-sm text-text leading-relaxed">{chain.executor}</p>
          </div>
        )}

        {decision.message_sent && (
          <div>
            <div className="text-[11px] font-medium uppercase tracking-wider text-text-faint mb-1.5">
              Message sent · {decision.language_pref}
            </div>
            <p className="text-sm text-text italic leading-relaxed bg-surface-raised rounded-md px-3.5 py-3 border border-border">
              "{decision.message_sent}"
            </p>
          </div>
        )}

        <div className="pt-2 border-t border-border">
          <div className="grid grid-cols-2 gap-3 text-xs">
            <div>
              <div className="text-text-faint mb-0.5">Payment method</div>
              <div className="text-text-muted">{decision.payment_method}</div>
            </div>
            <div>
              <div className="text-text-faint mb-0.5">Customer tenure</div>
              <div className="text-text-muted">{decision.customer_tenure_days} days</div>
            </div>
            <div>
              <div className="text-text-faint mb-0.5">Decided at</div>
              <div className="text-text-muted font-mono">
                {new Date(decision.timestamp).toLocaleString()}
              </div>
            </div>
            <div>
              <div className="text-text-faint mb-0.5">Retryable</div>
              <div className="text-text-muted">{decision.is_retryable ? "Yes" : "No"}</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
