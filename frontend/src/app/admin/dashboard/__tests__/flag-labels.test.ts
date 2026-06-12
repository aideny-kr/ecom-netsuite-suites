import { describe, expect, it } from "vitest";

import { FLAG_LABELS } from "../flag-labels";

// Must stay in sync with backend DEFAULT_FLAGS (feature_flag_service.py) —
// a flag missing here is silently un-togglable in the admin panel.
const BACKEND_DEFAULT_FLAGS = [
  "chat",
  "mcp_tools",
  "workspace",
  "reconciliation",
  "byok_ai",
  "custom_branding",
  "custom_domain",
  "analytics_export",
  "drive_rag",
  "plan_mode_enabled",
  "recon_scheduled_runs",
  "autonomous_recon",
];

describe("admin FLAG_LABELS", () => {
  it("covers every backend DEFAULT_FLAGS key", () => {
    for (const key of BACKEND_DEFAULT_FLAGS) {
      expect(FLAG_LABELS[key], `missing FLAG_LABELS entry for "${key}"`).toBeDefined();
      expect(FLAG_LABELS[key].label).toBeTruthy();
      expect(FLAG_LABELS[key].description).toBeTruthy();
    }
  });

  it("has no entries the backend does not know", () => {
    for (const key of Object.keys(FLAG_LABELS)) {
      expect(BACKEND_DEFAULT_FLAGS, `unknown flag key "${key}" in FLAG_LABELS`).toContain(key);
    }
  });
});
