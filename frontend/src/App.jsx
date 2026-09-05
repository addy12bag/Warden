import { useEffect, useMemo, useState } from "react";
import { api } from "./api.js";
import StatRail from "./components/StatRail.jsx";
import DataTable from "./components/DataTable.jsx";
import DetailPanel from "./components/DetailPanel.jsx";
import ExceptionsBar from "./components/ExceptionsBar.jsx";

export default function App() {
  const [metrics, setMetrics] = useState(null);
  const [decisions, setDecisions] = useState([]);
  const [exceptions, setExceptions] = useState([]);
  const [status, setStatus] = useState("loading"); // loading | ready | empty | error
  const [errorMessage, setErrorMessage] = useState("");
  const [actionFilter, setActionFilter] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [railOpen, setRailOpen] = useState(false);

  useEffect(() => {
    async function load() {
      try {
        const health = await api.health();
        if (!health.database_initialized) {
          setStatus("empty");
          return;
        }
        const [metricsData, decisionsData, exceptionsData] = await Promise.all([
          api.metrics(),
          api.decisions(),
          api.exceptions(),
        ]);
        setMetrics(metricsData);
        setDecisions(decisionsData);
        setExceptions(exceptionsData);
        setStatus("ready");
      } catch (err) {
        setErrorMessage(err.message || "Could not reach the API.");
        setStatus("error");
      }
    }
    load();
  }, []);

  const filteredDecisions = useMemo(() => {
    if (!actionFilter) return decisions;
    return decisions.filter((d) => d.chosen_action === actionFilter);
  }, [decisions, actionFilter]);

  const selectedDecision = useMemo(
    () => decisions.find((d) => d.payment_id === selectedId) || null,
    [decisions, selectedId]
  );

  if (status === "loading") {
    return (
      <div className="h-screen bg-bg flex items-center justify-center">
        <p className="text-sm text-text-faint">Loading console…</p>
      </div>
    );
  }

  if (status === "error") {
    return (
      <div className="h-screen bg-bg flex items-center justify-center px-8">
        <div className="max-w-md border border-danger/30 bg-danger-bg rounded-lg px-6 py-5">
          <h2 className="text-base font-semibold text-danger mb-1.5">Can't reach the API</h2>
          <p className="text-sm text-text-muted">{errorMessage}</p>
          <p className="text-sm text-text-muted mt-3">
            Make sure the backend is running:{" "}
            <code className="font-mono text-xs bg-surface-raised px-1.5 py-0.5 rounded">
              uvicorn main:app --reload
            </code>
          </p>
        </div>
      </div>
    );
  }

  if (status === "empty") {
    return (
      <div className="h-screen bg-bg flex items-center justify-center px-8">
        <div className="max-w-md border border-border bg-surface rounded-lg px-6 py-5 text-center">
          <h2 className="text-base font-semibold mb-1.5">No batch has been run yet</h2>
          <p className="text-sm text-text-muted">
            Run{" "}
            <code className="font-mono text-xs bg-surface-raised px-1.5 py-0.5 rounded">
              python run_full_batch.py
            </code>{" "}
            from the backend directory, then reload this page.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="h-screen bg-bg flex flex-col overflow-hidden">
      <header className="shrink-0 border-b border-border bg-surface px-4 sm:px-5 py-3 flex items-center gap-3">
        <button
          onClick={() => setRailOpen(!railOpen)}
          className="lg:hidden text-text-muted hover:text-text transition-colors focus-ring rounded p-1"
          aria-label="Toggle stats panel"
        >
          <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
            <path d="M2 5h14M2 9h14M2 13h14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
        </button>
        <div className="w-6 h-6 rounded bg-accent-bg flex items-center justify-center shrink-0">
          <span className="w-2 h-2 rounded-sm bg-accent" />
        </div>
        <div className="min-w-0">
          <h1 className="text-sm font-semibold leading-none">Recovery Console</h1>
          <p className="text-xs text-text-faint mt-0.5 truncate">
            AI Revenue Recovery Agent — measured against hidden ground truth
          </p>
        </div>
      </header>

      <div className="flex-1 flex min-h-0 relative">
        {/* Mobile rail: overlay, toggled */}
        <div
          className={`${
            railOpen ? "fixed inset-0 z-20 flex" : "hidden"
          } lg:static lg:z-auto lg:flex`}
        >
          {railOpen && (
            <div
              className="fixed inset-0 bg-black/50 lg:hidden"
              onClick={() => setRailOpen(false)}
            />
          )}
          <div className="relative z-10 h-full">
            <StatRail
              metrics={metrics}
              activeFilter={actionFilter}
              onFilterChange={(a) => {
                setActionFilter(a);
                setRailOpen(false);
              }}
            />
          </div>
        </div>

        <div className="flex-1 flex flex-col min-h-0">
          <DataTable
            decisions={filteredDecisions}
            onSelectRow={(d) => setSelectedId(d.payment_id)}
            selectedId={selectedId}
          />
          <ExceptionsBar
            exceptions={exceptions}
            onSelectRow={(e) => setSelectedId(e.payment_id)}
          />
        </div>

        {/* Detail panel: overlay on mobile, static on desktop.
            Exactly one DetailPanel renders at a time -- selected state
            takes priority; the empty-state placeholder only shows when
            nothing is selected, and only on desktop (mobile shows
            nothing until a row is tapped, to avoid an empty overlay). */}
        {selectedDecision ? (
          <div className="fixed inset-0 z-20 flex justify-end lg:static lg:z-auto">
            <div
              className="fixed inset-0 bg-black/50 lg:hidden"
              onClick={() => setSelectedId(null)}
            />
            <div className="relative z-10 h-full">
              <DetailPanel decision={selectedDecision} onClose={() => setSelectedId(null)} />
            </div>
          </div>
        ) : (
          <div className="hidden lg:block">
            <DetailPanel decision={null} onClose={() => setSelectedId(null)} />
          </div>
        )}
      </div>
    </div>
  );
}

