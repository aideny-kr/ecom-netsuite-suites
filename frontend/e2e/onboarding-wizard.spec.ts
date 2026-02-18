import { test, expect } from "@playwright/test";

test.describe("Onboarding Wizard", () => {
  test("wizard appears for new user and step navigation works", async ({
    page,
  }) => {
    // Register a new user
    const slug = `e2e-wiz-${Date.now()}`;
    const email = `wizard-${Date.now()}@test.com`;

    const regRes = await page.request.post(
      "http://localhost:8000/api/v1/auth/register",
      {
        data: {
          tenant_name: "Wizard Test Co",
          tenant_slug: slug,
          email,
          password: "TestPass1!",
          full_name: "Wizard Tester",
        },
      },
    );
    expect(regRes.ok()).toBeTruthy();
    const { access_token } = await regRes.json();

    // Set auth token for both API client (localStorage) and Next middleware (cookie)
    await page.context().addCookies([
      {
        name: "access_token",
        value: access_token,
        url: "http://localhost:3000",
      },
      {
        name: "access_token",
        value: access_token,
        url: "http://127.0.0.1:3000",
      },
    ]);
    await page.goto("/login");
    await page.evaluate((token: string) => {
      localStorage.setItem("access_token", token);
    }, access_token);
    await page.goto("/");

    // Wizard should appear (overlay with step content)
    await expect(page.locator("text=Step 1 of 5")).toBeVisible({
      timeout: 10000,
    });
    await expect(
      page.getByRole("heading", { name: "Business Profile" }),
    ).toBeVisible();

    // Click Skip to skip first step
    await page.getByRole("button", { name: "Skip" }).click();

    // Should advance to step 2
    await expect(page.locator("text=Step 2 of 5")).toBeVisible({
      timeout: 5000,
    });

    // Click "Set up later" to dismiss
    await page.getByText("Set up later").click();

    // Wizard should close
    await expect(page.locator("text=Step 2 of 5")).not.toBeVisible({
      timeout: 5000,
    });
  });
});
