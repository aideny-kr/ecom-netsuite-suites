/**
 * E2E golden path for the publishable report renderer (Slice 1).
 *
 * Flow under test (plan Task 14):
 *   1. /reports lists the seeded report (with an in-app link to it).
 *   2. Clicking it lands on /reports/[id].
 *   3. The view renders the saved HTML in a BLOB-URL <iframe> (src starts with
 *      `blob:`) whose document contains the seeded heading + a chart <svg>.
 *   4. A second report whose spec carries an `error` section renders the error
 *      block ("Data unavailable:") — the page does NOT crash.
 *   5. No console errors anywhere in the flow.
 *
 * Setup (self-contained, like onboarding-wizard.spec.ts + deploy-gate.spec.ts):
 *   - Register a fresh tenant+user via the backend → JWT (minted by the SAME
 *     backend we test against, so JWT_SECRET_KEY matches — deploy.md #5).
 *   - Inject the token into localStorage + the access_token cookie
 *     (deploy-gate.spec.ts injectToken pattern).
 *   - Seed two reports DIRECTLY in the local docker DB via a backend script
 *     (deploy-gate.spec.ts execSync pattern). The script renders real HTML via
 *     the production renderer so the iframe assertions exercise the real output.
 *
 * Local-DB RLS note: the local docker Postgres connects as the `postgres`
 * SUPERUSER, which BYPASSES RLS even with FORCE ROW LEVEL SECURITY — so the
 * list endpoint returns rows for ALL local tenants. The authoritative
 * cross-tenant isolation proof is the post-deploy live smoke against uat-smoke
 * (plan Task 15), NOT this spec. We seed unique-per-run titles so the
 * golden-path assertions stay deterministic on a shared local DB.
 *
 * CI gating: the suite runs under the existing `e2e-smoke` job, which executes
 * `npx playwright test || true` against BASE_URL. When no backend is reachable
 * (the default CI state today), `beforeAll` flips a skip flag so every test
 * skips cleanly instead of failing — matching the suite's "needs the full
 * stack" convention. Run locally against a rebuilt backend + dev server:
 *
 *   docker compose up -d --build backend
 *   (cd frontend && npm run dev)            # http://localhost:3000
 *   cd frontend && BASE_URL=http://localhost:3000 \
 *     npx playwright test e2e/reports.spec.ts
 */

import { execSync } from "node:child_process";
import { test, expect, type Page } from "@playwright/test";

const FRONTEND = process.env.BASE_URL || "http://localhost:3000";
const API = process.env.E2E_API_URL || "http://localhost:8000";
const BACKEND_CONTAINER =
  process.env.E2E_BACKEND_CONTAINER || "ecom-netsuite-suites-backend-1";

interface SeedResult {
  chart_report_id: string;
  error_report_id: string;
  chart_heading: string;
  error_title: string;
  error_reason: string;
}

let token = "";
let seed: SeedResult | null = null;
let setupError: string | null = null;

/** Register a fresh tenant via the backend and return its access token + ids. */
async function registerTenant(): Promise<{
  accessToken: string;
  tenantId: string;
  userId: string;
}> {
  const stamp = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const regRes = await fetch(`${API}/api/v1/auth/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      tenant_name: "Reports E2E Co",
      tenant_slug: `reports-e2e-${stamp}`,
      email: `reports-e2e-${stamp}@test.com`,
      password: "TestPass1!",
      full_name: "Reports E2E",
    }),
  });
  if (!regRes.ok) {
    throw new Error(`register failed: ${regRes.status} ${await regRes.text()}`);
  }
  const { access_token } = (await regRes.json()) as { access_token: string };

  const meRes = await fetch(`${API}/api/v1/auth/me`, {
    headers: { Authorization: `Bearer ${access_token}` },
  });
  if (!meRes.ok) {
    throw new Error(`auth/me failed: ${meRes.status}`);
  }
  const me = (await meRes.json()) as { id: string; tenant_id: string };
  return { accessToken: access_token, tenantId: me.tenant_id, userId: me.id };
}

/** Seed two reports into the local docker DB via the backend seed script. */
function seedReports(tenantId: string, userId: string): SeedResult {
  const stamp = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
  const out = execSync(
    `docker exec -e PYTHONPATH=/app ${BACKEND_CONTAINER} ` +
      `python scripts/e2e_reports_seed.py ${tenantId} ${userId} ${stamp}`,
    { encoding: "utf8" },
  );
  // The script prints exactly one JSON line; take the last non-empty line.
  const line = out
    .trim()
    .split("\n")
    .filter(Boolean)
    .pop() as string;
  return JSON.parse(line) as SeedResult;
}

async function injectToken(page: Page) {
  // Set the cookie via the context API (reliable for the Next.js middleware,
  // which gates the (dashboard) routes) for BOTH host forms, and seed
  // localStorage via addInitScript so the api-client bearer is present on every
  // navigation. Doing both before the first goto avoids the login-redirect race
  // that an in-page document.cookie write hits on the first navigation.
  await page.context().addCookies([
    { name: "access_token", value: token, url: FRONTEND },
  ]);
  await page.addInitScript((t) => {
    localStorage.setItem("access_token", t);
  }, token);
}

test.beforeAll(async () => {
  try {
    const { accessToken, tenantId, userId } = await registerTenant();
    token = accessToken;
    seed = seedReports(tenantId, userId);
  } catch (err) {
    // Backend/docker not reachable (e.g. CI without the stack) → skip cleanly.
    setupError = err instanceof Error ? err.message : String(err);
  }
});

test.describe("Publishable report renderer — golden path", () => {
  test.beforeEach(async ({ page }) => {
    test.skip(
      !!setupError,
      `Reports E2E needs a running backend + docker DB at ${API} ` +
        `(rebuild: docker compose up -d --build backend). Setup error: ${setupError}`,
    );
    // Surface console errors; assert none at the end of each test.
    await injectToken(page);
  });

  test("lists the seeded report and links to its detail page", async ({ page }) => {
    await page.goto(`${FRONTEND}/reports`);

    // Wait for the React Query fetch to settle before asserting the row — the
    // page renders skeletons first, then the list, and the local DB carries
    // many rows (superuser RLS bypass returns ALL tenants' reports).
    await expect(page.getByRole("heading", { name: "Reports" })).toBeVisible({
      timeout: 15_000,
    });

    // The seeded chart report is shown as an in-app link to /reports/[id].
    // Target by href (unambiguous): anchor on this run's specific report id.
    const link = page.locator(`a[href="/reports/${seed!.chart_report_id}"]`);
    await expect(link).toBeVisible({ timeout: 15_000 });
    await expect(link).toContainText(seed!.chart_heading);

    // Clicking lands on the detail route.
    await link.scrollIntoViewIfNeeded();
    await link.click();
    await expect(page).toHaveURL(new RegExp(`/reports/${seed!.chart_report_id}$`));
  });

  test("renders the report in a blob-URL iframe with heading + chart svg", async ({
    page,
  }) => {
    const consoleErrors: string[] = [];
    page.on("console", (msg) => {
      if (msg.type() === "error") consoleErrors.push(msg.text());
    });
    page.on("pageerror", (err) => consoleErrors.push(`pageerror: ${err.message}`));

    await page.goto(`${FRONTEND}/reports/${seed!.chart_report_id}`);

    const iframe = page.locator("iframe[title='Report']");
    await expect(iframe).toBeVisible({ timeout: 10_000 });

    // (3) the iframe src is a BLOB url (authed fetch → Blob → object URL).
    await expect
      .poll(async () => (await iframe.getAttribute("src")) ?? "", { timeout: 10_000 })
      .toMatch(/^blob:/);

    // The iframe document contains the seeded heading + a chart <svg>.
    const frame = page.frameLocator("iframe[title='Report']");
    await expect(frame.getByRole("heading", { name: seed!.chart_heading })).toBeVisible({
      timeout: 10_000,
    });
    await expect(frame.locator("svg").first()).toBeVisible();

    // (5) no console errors during the render flow.
    expect(consoleErrors).toEqual([]);
  });

  test("renders the error block for a report with a missing-data section (no crash)", async ({
    page,
  }) => {
    const consoleErrors: string[] = [];
    page.on("console", (msg) => {
      if (msg.type() === "error") consoleErrors.push(msg.text());
    });
    page.on("pageerror", (err) => consoleErrors.push(`pageerror: ${err.message}`));

    await page.goto(`${FRONTEND}/reports/${seed!.error_report_id}`);

    const iframe = page.locator("iframe[title='Report']");
    await expect(iframe).toBeVisible({ timeout: 10_000 });
    await expect
      .poll(async () => (await iframe.getAttribute("src")) ?? "", { timeout: 10_000 })
      .toMatch(/^blob:/);

    const frame = page.frameLocator("iframe[title='Report']");
    // The error section renders the "Data unavailable:" block — page did NOT crash.
    await expect(frame.getByText(/Data unavailable:/i)).toBeVisible({ timeout: 10_000 });
    await expect(frame.getByRole("heading", { name: seed!.error_title })).toBeVisible();

    expect(consoleErrors).toEqual([]);
  });
});
