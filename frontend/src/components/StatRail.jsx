function formatCurrency(amount) {
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 0,
  }).format(amount);
}

function StatTile({ label, value, sublabel, tone }) {
  const toneClass = {
    success: "text-success",
    warning: "text-warning",
    danger: "text-danger",
    default: "text-text",
  }[tone || "default"];

  return (
    <div className="px-4 py-3.5 border-b border-border last:border-b-0">
      <div className="text-[11px] font-medium uppercase tracking-wider text-text-faint mb-1.5">
        {label}
      </div>
      <div className={`font-mono text-[26px] font-semibold leading-none ${toneClass}`}>
        {value}
      </div>
      {sublabel && (
        <div className="text-xs text-text-muted mt-1.5">{sublabel}</div>
      )}
    </div>
  );
}

const ACTION_LABELS = {
  retry_now: "Retry now",
  retry_delayed: "Retry delayed",
  prompt_method_switch: "Method switch",
  send_reminder: "Reminder",
  escalate: "Escalate",
  no_action: "No action",
};

export default function StatRail({ metrics, activeFilter, onFilterChange }) {
  if (!metrics) return null;

  const actionEntries = Object.entries(metrics.recovery_rate_by_action || {});

  return (
    <aside className="w-72 shrink-0 border-r border-border bg-surface h-full overflow-y-auto">
      <div className="px-4 py-4 border-b border-border">
        <div className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-success animate-pulse" />
          <span className="text-xs font-medium text-text-muted">Live batch results</span>
        </div>
      </div>

      <StatTile
        label="Recovery rate"
        value={`${metrics.recovery_rate_pct.toFixed(1)}%`}
        sublabel={`${metrics.recovered_count} / ${metrics.total_decisions} decisions`}
        tone="success"
      />
      <StatTile
        label="Recovered"
        value={formatCurrency(metrics.recovered_amount)}
        sublabel={`of ${formatCurrency(metrics.total_batch_amount)} total`}
        tone="success"
      />
      <StatTile
        label="At risk"
        value={formatCurrency(metrics.still_at_risk_amount)}
        sublabel="failing + escalated"
        tone="warning"
      />
      <StatTile
        label="False-retry rate"
        value={`${metrics.false_retry_rate_pct.toFixed(1)}%`}
        sublabel="retried an unrecoverable case"
        tone={metrics.false_retry_count > 0 ? "danger" : "success"}
      />
      <StatTile
        label="Exceptions"
        value={metrics.exceptions_count}
        sublabel="unresolved, shown in full"
        tone="warning"
      />

      {actionEntries.length > 0 && (
        <div className="px-4 py-3.5">
          <div className="text-[11px] font-medium uppercase tracking-wider text-text-faint mb-2.5">
            Filter by action
          </div>
          <div className="space-y-0.5">
            <button
              onClick={() => onFilterChange(null)}
              className={`w-full flex items-center justify-between px-2.5 py-1.5 rounded text-sm transition-colors focus-ring ${
                activeFilter === null
                  ? "bg-accent-bg text-accent"
                  : "text-text-muted hover:bg-surface-hover hover:text-text"
              }`}
            >
              <span>All actions</span>
              <span className="font-mono text-xs">{metrics.total_decisions}</span>
            </button>
            {actionEntries.map(([action, stats]) => (
              <button
                key={action}
                onClick={() => onFilterChange(action)}
                className={`w-full flex items-center justify-between px-2.5 py-1.5 rounded text-sm transition-colors focus-ring ${
                  activeFilter === action
                    ? "bg-accent-bg text-accent"
                    : "text-text-muted hover:bg-surface-hover hover:text-text"
                }`}
              >
                <span>{ACTION_LABELS[action] || action}</span>
                <span className="font-mono text-xs">{stats.count}</span>
              </button>
            ))}
          </div>
        </div>
      )}
    </aside>
  );
}
