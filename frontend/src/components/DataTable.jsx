import { useMemo, useState } from "react";

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

const ACTION_LABELS = {
  retry_now: "Retry now",
  retry_delayed: "Retry delayed",
  prompt_method_switch: "Method switch",
  send_reminder: "Reminder",
  escalate: "Escalate",
  no_action: "No action",
};

// Grid layout shared by header and rows so columns always line up and
// fill the available width instead of leaving dead space on wide
// screens. Action column collapses out below md, matching before.
const GRID_COLS = "grid-cols-[6.5rem_1fr_8rem_7rem_7rem] md:grid-cols-[7rem_1fr_9rem_8rem_7.5rem]";

const COLUMNS = [
  { key: "payment_id", label: "Payment" },
  { key: "root_cause", label: "Root cause" },
  { key: "chosen_action", label: "Action", hideBelowMd: true },
  { key: "amount", label: "Amount", align: "right" },
  { key: "outcome", label: "Outcome" },
];

export default function DataTable({ decisions, onSelectRow, selectedId }) {
  const [sortKey, setSortKey] = useState("payment_id");
  const [sortDir, setSortDir] = useState("asc");

  const sorted = useMemo(() => {
    const copy = [...decisions];
    copy.sort((a, b) => {
      let av = a[sortKey];
      let bv = b[sortKey];

      // payment_id is like "txn_00001" -- compare the numeric part so
      // ordering is 1, 2, 3 ... 500, not a lexicographic string sort
      // (which would still happen to work for same-length zero-padded
      // IDs, but breaks the moment that assumption doesn't hold).
      if (sortKey === "payment_id") {
        const an = parseInt(String(av).replace(/\D/g, ""), 10);
        const bn = parseInt(String(bv).replace(/\D/g, ""), 10);
        if (!Number.isNaN(an) && !Number.isNaN(bn)) {
          av = an;
          bv = bn;
        }
      } else if (typeof av === "string") {
        av = av.toLowerCase();
        bv = bv.toLowerCase();
      }

      if (av < bv) return sortDir === "asc" ? -1 : 1;
      if (av > bv) return sortDir === "asc" ? 1 : -1;
      return 0;
    });
    return copy;
  }, [decisions, sortKey, sortDir]);

  function handleSort(key) {
    if (sortKey === key) {
      setSortDir(sortDir === "asc" ? "desc" : "asc");
    } else {
      setSortKey(key);
      // Payment IDs and root cause read naturally low-to-high /
      // A-to-Z first; amount reads naturally highest-first.
      setSortDir(key === "amount" ? "desc" : "asc");
    }
  }

  return (
    <div className="flex-1 flex flex-col min-h-0">
      <div className={`grid ${GRID_COLS} gap-3 items-center px-4 py-2 border-b border-border bg-surface text-[11px] font-medium uppercase tracking-wider text-text-faint sticky top-0 z-10`}>
        {COLUMNS.map((col) => (
          <button
            key={col.key}
            onClick={() => handleSort(col.key)}
            className={`${col.hideBelowMd ? "hidden md:flex" : "flex"} items-center gap-1 hover:text-text-muted transition-colors focus-ring rounded whitespace-nowrap ${
              col.align === "right" ? "justify-end text-right" : "text-left"
            }`}
          >
            {col.label}
            {sortKey === col.key && (
              <span className="text-accent">{sortDir === "asc" ? "\u2191" : "\u2193"}</span>
            )}
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-y-auto">
        {sorted.map((d) => (
          <button
            key={d.payment_id}
            onClick={() => onSelectRow(d)}
            className={`w-full grid ${GRID_COLS} gap-3 items-center px-4 py-2.5 border-b border-border text-left transition-colors focus-ring ${
              selectedId === d.payment_id
                ? "bg-accent-bg"
                : "hover:bg-surface-hover"
            }`}
          >
            <span className="font-mono text-xs text-text-muted truncate">
              {d.payment_id}
            </span>
            <span className="text-sm truncate">{d.root_cause}</span>
            <span className="hidden md:flex text-sm text-text-muted items-center gap-1.5 truncate">
              {ACTION_LABELS[d.chosen_action] || d.chosen_action}
              {d.stopping_rule_triggered && (
                <span
                  className="w-1.5 h-1.5 rounded-full bg-danger shrink-0"
                  title="Overridden by stopping rules"
                />
              )}
            </span>
            <span className="font-mono text-xs sm:text-sm text-right">
              {formatCurrency(d.amount)}
            </span>
            <span>
              <span
                className={`inline-block text-[10px] sm:text-xs font-medium px-1.5 sm:px-2 py-0.5 rounded ${
                  OUTCOME_STYLES[d.outcome] || "bg-surface-raised text-text-muted"
                }`}
              >
                {d.outcome.replace("_", " ")}
              </span>
            </span>
          </button>
        ))}
        {sorted.length === 0 && (
          <div className="px-4 py-12 text-center text-text-muted text-sm">
            No decisions match this filter.
          </div>
        )}
      </div>
    </div>
  );
}

