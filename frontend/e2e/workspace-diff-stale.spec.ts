import { test, expect, type Page } from "@playwright/test";
import { registerAndLogin } from "./helpers";

/**
 * E2E smoke for the stale-patch diff banner.
 *
 * Staging bug 2026-05-18: the Sakura mainboards changeset's side-by-side
 * diff rendered identical panes because the patch's baseline drifted away
 * from the live file. The backend swallowed the apply failure and the
 * viewer showed two identical strings.
 *
 * After the fix, the API returns `diff_status: "stale"` + a fallback
 * before/after view. This smoke pins the UI contract: when the API says
 * stale, the workspace IDE renders a "stale" badge and an explainer banner
 * pointing the user at the re-create flow.
 *
 * Strategy: route-intercept the diff API instead of trying to drive a real
 * propose_patch (no HTTP API — MCP-only). We POST a real (empty) changeset
 * so the changeset panel has something to render, then mock its diff
 * response.
 */

const baseUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

async function getAuthHeaders(page: Page) {
  const token = await page.evaluate(() => localStorage.getItem("access_token") || "");
  return {
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
  };
}

async function createWorkspaceViaApi(page: Page, name: string): Promise<string> {
  const headers = await getAuthHeaders(page);
  const res = await page.request.post(`${baseUrl}/api/v1/workspaces`, {
    headers,
    data: { name, description: "stale-diff smoke" },
  });
  expect(res.ok()).toBeTruthy();
  return (await res.json()).id as string;
}

async function createChangesetViaApi(
  page: Page,
  workspaceId: string,
  title: string,
): Promise<string> {
  const headers = await getAuthHeaders(page);
  const res = await page.request.post(
    `${baseUrl}/api/v1/workspaces/${workspaceId}/changesets`,
    { headers, data: { title, description: "smoke test" } },
  );
  expect(res.ok()).toBeTruthy();
  return (await res.json()).id as string;
}

async function selectWorkspace(page: Page, workspaceName: string) {
  await page.getByTestId("workspace-selector").click();
  await page.getByTestId("workspace-option").filter({ hasText: workspaceName }).first().click();
}

test.describe("Workspace IDE — stale-patch diff banner", () => {
  test.beforeEach(async ({ page }) => {
    await registerAndLogin(page);
  });

  test("renders a 'stale' badge and banner when the diff API reports drift", async ({ page }) => {
    const workspaceName = `Stale Diff WS ${Date.now()}`;
    const workspaceId = await createWorkspaceViaApi(page, workspaceName);
    const changesetId = await createChangesetViaApi(page, workspaceId, "Drifted patch");

    // Intercept the diff endpoint AFTER the changeset exists but BEFORE the
    // user clicks View Diff. The mocked payload is the shape our backend
    // emits when baseline_sha256 mismatch was detected.
    await page.route(
      `**/api/v1/changesets/${changesetId}/diff`,
      async (route) => {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            changeset_id: changesetId,
            title: "Drifted patch",
            files: [
              {
                file_path: "SuiteScripts/user_event/Framework_SalesOrder_UE.js",
                operation: "modify",
                original_content: "const LILAC = ['4592'];\n",
                modified_content: "const LILAC = ['4592'];\nconst SAKURA = ['9399'];\n",
                unified_diff:
                  "--- a/SuiteScripts/user_event/Framework_SalesOrder_UE.js\n" +
                  "+++ b/SuiteScripts/user_event/Framework_SalesOrder_UE.js\n" +
                  "@@ -1 +1,2 @@\n const LILAC = ['4592'];\n+const SAKURA = ['9399'];\n",
                diff_status: "stale",
                baseline_drift: true,
              },
            ],
          }),
        });
      },
    );

    await page.goto("/workspace");
    await selectWorkspace(page, workspaceName);

    // Open the changeset's diff
    await page.getByRole("button", { name: /View Diff/i }).first().click();

    // The stale badge appears in the file header
    await expect(page.getByTestId("diff-stale-badge")).toBeVisible();
    await expect(page.getByTestId("diff-stale-badge")).toHaveText(/stale/i);

    // The explainer banner is rendered
    await expect(
      page.getByText(/File has changed since this patch was created/i),
    ).toBeVisible();
    // And it tells the user how to recover
    await expect(
      page.getByText(/Re-create the patch from the current file before applying/i),
    ).toBeVisible();
  });

  test("does NOT render the stale banner for a clean diff", async ({ page }) => {
    const workspaceName = `Clean Diff WS ${Date.now()}`;
    const workspaceId = await createWorkspaceViaApi(page, workspaceName);
    const changesetId = await createChangesetViaApi(page, workspaceId, "Clean patch");

    await page.route(
      `**/api/v1/changesets/${changesetId}/diff`,
      async (route) => {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            changeset_id: changesetId,
            title: "Clean patch",
            files: [
              {
                file_path: "SuiteScripts/hello.js",
                operation: "modify",
                original_content: "console.log('old');\n",
                modified_content: "console.log('new');\n",
                unified_diff:
                  "--- a/SuiteScripts/hello.js\n" +
                  "+++ b/SuiteScripts/hello.js\n" +
                  "@@ -1 +1 @@\n-console.log('old');\n+console.log('new');\n",
                diff_status: "clean",
                baseline_drift: false,
              },
            ],
          }),
        });
      },
    );

    await page.goto("/workspace");
    await selectWorkspace(page, workspaceName);
    await page.getByRole("button", { name: /View Diff/i }).first().click();

    // Banner must NOT render on clean diffs
    await expect(page.getByTestId("diff-stale-badge")).toBeHidden();
    await expect(
      page.getByText(/File has changed since this patch was created/i),
    ).toBeHidden();
  });
});
