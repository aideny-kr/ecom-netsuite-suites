/**
 * Live Electron e2e — the B0 rich-pipe `data_table` render smoke, automated.
 *
 * Launches the real Electron app (unpackaged → dev path → spawns
 * `desktop/.venv/bin/python runtime/sidecar.py --serve`, which authenticates
 * off the Claude Code Keychain per ADR-008/009), types the demo prompt, and
 * asserts that the live Hermes agent autonomously calls `sample_dataset`, the
 * sidecar streams a `data_table` event, and the reused `data-frame-table` card
 * renders the tool's exact rows in the chat history.
 *
 * Gated behind RUN_DESKTOP_E2E=1 — it makes a real, subscription-billed call.
 */
import {
  test,
  expect,
  _electron as electron,
  type ElectronApplication,
  type Page,
} from "@playwright/test";
import path from "node:path";
import os from "node:os";
import fs from "node:fs";

const RENDERER_URL = "http://127.0.0.1:3123";
const APP_ROOT = path.resolve(__dirname, ".."); // desktop/electron (has package.json main = dist/main.js)
const LIVE = process.env.RUN_DESKTOP_E2E === "1";

test.describe("rich-pipe data_table render (live, off Keychain)", () => {
  test.skip(
    !LIVE,
    "live e2e — set RUN_DESKTOP_E2E=1 (needs a Keychain/OAuth credential; bills the subscription)",
  );

  let app: ElectronApplication;
  let win: Page;

  test.beforeAll(async () => {
    // Isolate vault writes to a throwaway home so the smoke never touches the
    // operator's real ~/SuiteStudio.
    const home = fs.mkdtempSync(path.join(os.tmpdir(), "suitestudio-e2e-"));
    app = await electron.launch({
      args: [APP_ROOT],
      cwd: APP_ROOT,
      env: {
        ...process.env,
        SUITE_STUDIO_RENDERER_URL: RENDERER_URL,
        SUITE_STUDIO_HOME: path.join(home, "SuiteStudio"),
      },
    });
    win = await app.firstWindow();
    await win.waitForLoadState("domcontentloaded");
  });

  test.afterAll(async () => {
    await app?.close();
  });

  test("demo prompt streams a data-frame-table card from the live agent", async () => {
    // The composer ships `disabled` in the static HTML and is enabled only after
    // React hydrates — so this also asserts the packaged renderer hydrated.
    const input = win.getByRole("textbox", { name: "Chat query" });
    await expect(input).toBeEnabled({ timeout: 30_000 });

    await input.fill("show me the demo table");
    await win.getByRole("button", { name: "Send" }).click();

    // The live Hermes agent decides to call `sample_dataset`; the sidecar
    // intercepts its {columns, rows} into a typed `data_table` event; the reused
    // card renders the exact tool rows (NOT LLM-retyped text — the no-LLM-numbers
    // invariant). Assert on the card header + deterministic sample rows.
    await expect(win.getByText("Query Results")).toBeVisible({ timeout: 90_000 });
    await expect(win.getByText("Cash & Equivalents")).toBeVisible();
    await expect(win.getByText("Retained Earnings")).toBeVisible();

    fs.mkdirSync(path.join(APP_ROOT, "test-results"), { recursive: true });
    await win.screenshot({
      path: path.join(APP_ROOT, "test-results", "data-table-render.png"),
      fullPage: true,
    });
  });
});
