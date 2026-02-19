/**
 * Workspace layout visual test — validates panels render correctly.
 *
 * Mocks ALL API calls via page.route before navigation so the auth
 * provider resolves and the workspace page fully renders.
 */
import { test, expect } from "@playwright/test";

const API = "http://localhost:8000";

async function setupAllMocks(page: import("@playwright/test").Page) {
  // Log browser console messages
  page.on("console", (msg) => {
    console.log(`  [BROWSER ${msg.type()}]`, msg.text());
  });

  // Log all requests (Playwright-level)
  page.on("request", (req) => {
    console.log("  [REQ]", req.method(), req.url().substring(0, 120));
  });
  page.on("requestfailed", (req) => {
    console.log("  [FAIL]", req.url().substring(0, 120), req.failure()?.errorText);
  });

  // Intercept ALL requests to the backend API
  await page.route(`${API}/**`, (route) => {
    const url = route.request().url();
    const path = url.replace(API, "");

    console.log("  [MOCK]", path);

    // Auth endpoints — check longer path first
    if (path.startsWith("/api/v1/auth/me/tenants")) {
      return route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
    }
    if (path.startsWith("/api/v1/auth/me")) {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: "u1", email: "t@e.com", full_name: "Test",
          role: "admin", tenant_id: "t1", onboarding_completed_at: "2024-01-01",
        }),
      });
    }

    // Default: return empty array for any API call
    return route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
  });
}

test.describe("Workspace Layout", () => {
  test.beforeEach(async ({ page }) => {
    // Cookie for middleware
    await page.context().addCookies([{
      name: "access_token", value: "test-token", domain: "localhost", path: "/",
    }]);
    // localStorage for auth provider — log to verify it runs
    await page.addInitScript(() => {
      localStorage.setItem("access_token", "test-token");
      console.log("[INIT] localStorage access_token set");
    });
    // Mock all API calls (must await)
    await setupAllMocks(page);
  });

  test("workspace page renders with panels and separators", async ({ page }) => {
    await page.goto("/workspace", { waitUntil: "networkidle" });
    await page.waitForTimeout(2000);

    await page.screenshot({ path: "e2e/screenshots/workspace-full.png" });

    const url = page.url();
    console.log("URL:", url);
    const text = await page.locator("body").innerText();
    console.log("Body:", text.substring(0, 500));

    // Must be on workspace, not redirected
    expect(url).toContain("/workspace");

    // Check separators (resize handles)
    const seps = page.locator('[role="separator"]');
    const sepCount = await seps.count();
    console.log("Separators:", sepCount);
    expect(sepCount).toBeGreaterThanOrEqual(2);

    for (let i = 0; i < sepCount; i++) {
      const box = await seps.nth(i).boundingBox();
      console.log(`  Sep[${i}]:`, JSON.stringify(box));
      expect(box).not.toBeNull();
      expect(Math.max(box!.width, box!.height)).toBeGreaterThanOrEqual(6);
    }

    // Explorer label visible — confirms the file tree panel rendered
    await expect(page.locator("text=Explorer").first()).toBeVisible();

    // The vertical separator should be positioned to give the file tree ≥100px width
    const vSep = seps.first();
    const vSepBox = await vSep.boundingBox();
    console.log("Vertical sep x:", vSepBox?.x);
    // Separator x is the right edge of the file tree; should be > 100px from page left
    expect(vSepBox!.x).toBeGreaterThan(100);
  });

  test("resize handles are interactive with correct ARIA attributes", async ({ page }) => {
    await page.goto("/workspace", { waitUntil: "networkidle" });
    await page.waitForTimeout(2000);

    const seps = page.locator('[role="separator"]');
    const sepCount = await seps.count();
    expect(sepCount).toBe(2);

    // Vertical separator (between file tree and editor)
    const vSep = seps.nth(0);
    await expect(vSep).toHaveAttribute("aria-orientation", "vertical");
    await expect(vSep).toHaveAttribute("tabindex", "0");
    await expect(vSep).toHaveAttribute("aria-controls", "file-tree");
    const vBox = await vSep.boundingBox();
    expect(vBox!.width).toBe(8); // Our flexBasis: 8px is applied

    // Horizontal separator (between editor and bottom panel)
    const hSep = seps.nth(1);
    await expect(hSep).toHaveAttribute("aria-orientation", "horizontal");
    await expect(hSep).toHaveAttribute("tabindex", "0");
    const hBox = await hSep.boundingBox();
    expect(hBox!.height).toBe(8);

    // Separator should be focusable (confirms keyboard interaction is possible)
    await vSep.focus();
    await expect(vSep).toBeFocused();

    console.log("Vertical separator:", JSON.stringify(vBox));
    console.log("Horizontal separator:", JSON.stringify(hBox));
  });
});
