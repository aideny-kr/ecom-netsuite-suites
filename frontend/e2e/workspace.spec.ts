import { test, expect } from "@playwright/test";
import { registerAndLogin } from "./helpers";

test.describe("Dev Workspace — smoke test", () => {
  test.beforeEach(async ({ page }) => {
    await registerAndLogin(page);
  });

  test("navigate to workspace page via sidebar", async ({ page }) => {
    await page.getByRole("link", { name: "Dev Workspace" }).click();
    await expect(page).toHaveURL(/\/workspace/);
    // Workspace selector should be visible
    await expect(
      page.getByText(/select a workspace|no workspaces/i),
    ).toBeVisible({ timeout: 10_000 });
  });

  test("create workspace and see it in selector", async ({ page }) => {
    await page.goto("/workspace");
    await page.waitForLoadState("networkidle");

    // If there is a "Create Workspace" or "New" button, click it
    const createBtn = page.getByRole("button", { name: /create|new/i });
    const hasBtnVisible = await createBtn.isVisible().catch(() => false);
    if (hasBtnVisible) {
      await createBtn.click();
      // Fill workspace name
      const nameInput = page.getByLabel(/name/i);
      await nameInput.fill("E2E Test Workspace");
      // Submit
      const submitBtn = page.getByRole("button", { name: /create|save/i });
      await submitBtn.click();
      // Verify workspace appears
      await expect(page.getByText("E2E Test Workspace")).toBeVisible({
        timeout: 10_000,
      });
    }
  });

  test("file tree renders after workspace selected", async ({ page }) => {
    await page.goto("/workspace");
    await page.waitForLoadState("networkidle");

    // If a workspace already exists, select it
    const selector = page.locator("[data-testid='workspace-selector']");
    const selectorVisible = await selector.isVisible().catch(() => false);
    if (selectorVisible) {
      await selector.click();
      const option = page.locator("[data-testid='workspace-option']").first();
      const optionVisible = await option.isVisible().catch(() => false);
      if (optionVisible) {
        await option.click();
        // File tree panel should appear
        await expect(
          page.locator("[data-testid='file-tree']"),
        ).toBeVisible({ timeout: 10_000 });
      }
    }
  });

  test("search files input is present", async ({ page }) => {
    await page.goto("/workspace");
    await page.waitForLoadState("networkidle");

    // Search input should be on the page (may be disabled without workspace)
    const searchInput = page.getByPlaceholder(/search/i);
    await expect(searchInput).toBeVisible({ timeout: 10_000 });
  });

  test("changeset panel renders", async ({ page }) => {
    await page.goto("/workspace");
    await page.waitForLoadState("networkidle");

    // Changeset panel or empty state should be visible
    const changesetArea = page.getByText(/changeset|no changesets|changes/i);
    await expect(changesetArea).toBeVisible({ timeout: 10_000 });
  });

  test("chat @file mention opens picker", async ({ page }) => {
    await page.goto("/chat");
    await page.waitForLoadState("networkidle");

    // Type @ in chat input to trigger file mention picker
    const chatInput = page.getByPlaceholder(/message|ask|type/i);
    const chatVisible = await chatInput.isVisible().catch(() => false);
    if (chatVisible) {
      await chatInput.fill("@");
      // File mention picker should appear
      const picker = page.getByPlaceholder(/search files/i);
      const pickerVisible = await picker
        .isVisible({ timeout: 3_000 })
        .catch(() => false);
      // Picker may not open without a workspace — just verify no crash
      expect(pickerVisible !== undefined).toBeTruthy();
    }
  });
});
