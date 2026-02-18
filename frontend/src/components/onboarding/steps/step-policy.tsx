"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { apiClient } from "@/lib/api-client";

interface StepPolicyProps {
  onStepComplete: () => void;
}

const TOOL_OPTIONS = [
  "netsuite.suiteql",
  "netsuite.connectivity",
  "workspace.list_files",
  "workspace.read_file",
  "workspace.search",
  "workspace.propose_patch",
];

export function StepPolicy({ onStepComplete }: StepPolicyProps) {
  const [readOnlyMode, setReadOnlyMode] = useState(true);
  const [sensitivityDefault, setSensitivityDefault] = useState("financial");
  const [enforceToolAllowlist, setEnforceToolAllowlist] = useState(false);
  const [toolAllowlist, setToolAllowlist] = useState<string[]>([]);
  const [maxRows, setMaxRows] = useState(1000);
  const [requireRowLimit, setRequireRowLimit] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const toggleTool = (tool: string) => {
    setToolAllowlist((prev) =>
      prev.includes(tool) ? prev.filter((t) => t !== tool) : [...prev, tool],
    );
  };

  const handleSubmit = async () => {
    setIsSaving(true);
    setError(null);
    try {
      await apiClient.post("/api/v1/onboarding/setup-policy", {
        read_only_mode: readOnlyMode,
        sensitivity_default: sensitivityDefault,
        tool_allowlist: enforceToolAllowlist ? toolAllowlist : null,
        max_rows_per_query: maxRows,
        require_row_limit: requireRowLimit,
      });
      onStepComplete();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to save policy");
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <div className="space-y-6 p-6">
      <div className="space-y-4">
        <div className="flex items-center justify-between rounded-lg border p-4">
          <div>
            <h3 className="text-sm font-medium">Read-only Mode</h3>
            <p className="text-xs text-muted-foreground mt-0.5">
              Only allow read operations on NetSuite data
            </p>
          </div>
          <button
            onClick={() => setReadOnlyMode(!readOnlyMode)}
            className={`relative h-6 w-11 rounded-full transition-colors ${
              readOnlyMode ? "bg-primary" : "bg-muted-foreground/20"
            }`}
          >
            <span
              className={`absolute top-0.5 left-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform ${
                readOnlyMode ? "translate-x-5" : "translate-x-0"
              }`}
            />
          </button>
        </div>

        <div className="flex items-center justify-between rounded-lg border p-4">
          <div>
            <h3 className="text-sm font-medium">Require Row Limit</h3>
            <p className="text-xs text-muted-foreground mt-0.5">
              All queries must include a row limit
            </p>
          </div>
          <button
            onClick={() => setRequireRowLimit(!requireRowLimit)}
            className={`relative h-6 w-11 rounded-full transition-colors ${
              requireRowLimit ? "bg-primary" : "bg-muted-foreground/20"
            }`}
          >
            <span
              className={`absolute top-0.5 left-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform ${
                requireRowLimit ? "translate-x-5" : "translate-x-0"
              }`}
            />
          </button>
        </div>

        <div className="rounded-lg border p-4">
          <h3 className="text-sm font-medium">Default Sensitivity</h3>
          <p className="text-xs text-muted-foreground mt-0.5 mb-3">
            Classify default output sensitivity for policy-aware retrieval
          </p>
          <select
            value={sensitivityDefault}
            onChange={(e) => setSensitivityDefault(e.target.value)}
            className="w-full rounded-lg border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-ring"
          >
            <option value="financial">Financial</option>
            <option value="non_financial">Non-financial</option>
            <option value="mixed">Mixed</option>
          </select>
        </div>

        <div className="rounded-lg border p-4">
          <div className="flex items-center justify-between">
            <div>
              <h3 className="text-sm font-medium">Tool Allowlist</h3>
              <p className="text-xs text-muted-foreground mt-0.5">
                Optionally restrict the AI to specific tools
              </p>
            </div>
            <button
              onClick={() => setEnforceToolAllowlist(!enforceToolAllowlist)}
              className={`relative h-6 w-11 rounded-full transition-colors ${
                enforceToolAllowlist ? "bg-primary" : "bg-muted-foreground/20"
              }`}
            >
              <span
                className={`absolute top-0.5 left-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform ${
                  enforceToolAllowlist ? "translate-x-5" : "translate-x-0"
                }`}
              />
            </button>
          </div>
          {enforceToolAllowlist && (
            <div className="mt-3 grid grid-cols-2 gap-2">
              {TOOL_OPTIONS.map((tool) => (
                <label
                  key={tool}
                  className="flex items-center gap-2 text-xs text-muted-foreground"
                >
                  <input
                    type="checkbox"
                    checked={toolAllowlist.includes(tool)}
                    onChange={() => toggleTool(tool)}
                  />
                  <span className="font-mono">{tool}</span>
                </label>
              ))}
            </div>
          )}
        </div>

        <div className="rounded-lg border p-4">
          <h3 className="text-sm font-medium">Max Rows per Query</h3>
          <p className="text-xs text-muted-foreground mt-0.5 mb-3">
            Maximum number of rows that can be returned by a single query
          </p>
          <input
            type="number"
            value={maxRows}
            onChange={(e) => setMaxRows(Number(e.target.value))}
            min={1}
            max={10000}
            className="w-full rounded-lg border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-ring"
          />
        </div>
      </div>

      {error && <p className="text-sm text-destructive">{error}</p>}

      <Button onClick={handleSubmit} disabled={isSaving} className="w-full">
        {isSaving ? "Saving..." : "Save Policy Settings"}
      </Button>
    </div>
  );
}
