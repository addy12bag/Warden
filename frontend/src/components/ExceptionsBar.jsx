import { useState } from "react";

function formatCurrency(amount) {
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 2,
  }).format(amount);
}

export default function ExceptionsBar({ exceptions, onSelectRow }) {
  const [open, setOpen] = useState(false);

  if (exceptions.length === 0) return null;

  return (
    <div className="border-t border-border bg-surface shrink-0">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-3 px-4 py-2.5 text-left hover:bg-surface-hover transition-colors focus-ring"
      >
        <span className="w-1.5 h-1.5 rounded-full bg-warning shrink-0" />
        <span className="text-sm font-medium text-warning">
          {exceptions.length} unresolved exception{exceptions.length === 1 ? "" : "s"}
        </span>
        <span className="text-xs text-text-faint">shown in full, not a curated sample</span>
        <span className="ml-auto text-text-faint text-xs">{open ? "\u2212" : "+"}</span>
      </button>

      {open && (
        <div className="max-h-56 overflow-y-auto border-t border-border">
          {exceptions.map((e) => (
            <button
              key={e.payment_id}
              onClick={() => onSelectRow(e)}
              className="w-full flex items-center gap-4 px-4 py-2 border-b border-border last:border-b-0 hover:bg-surface-hover transition-colors text-left focus-ring"
            >
              <span className="font-mono text-xs text-text-muted w-28 shrink-0">
                {e.payment_id}
              </span>
              <span className="text-sm w-40 shrink-0 truncate">{e.root_cause}</span>
              <span className="text-sm text-text-muted flex-1 truncate">
                {e.chosen_action.replace(/_/g, " ")}
              </span>
              <span className="font-mono text-sm text-warning">
                {formatCurrency(e.amount)}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
