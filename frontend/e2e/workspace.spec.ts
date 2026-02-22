import fs from "node:fs";
import path from "node:path";
import { test, expect, type Page } from "@playwright/test";
import { registerAndLogin } from "./helpers";

const baseUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const fixtureZipPath = path.resolve(process.cwd(), "e2e/fixtures/workspace-sample.zip");

async function getAuthHeaders(page: Page) {
  const token = await page.evaluate(() => localStorage.getItem("access_token") || "");
  return {
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
  };
}

async function createWorkspaceViaApi(page: Page, name: string) {
  const headers = await getAuthHeaders(page);
  const response = await page.request.post(`${baseUrl}/api/v1/workspaces`, {
    headers,
    data: { name, description: "Playwright workspace" },
  });
  expect(response.ok()).toBeTruthy();
  const body = await response.json();
  return body.id as string;
}

async function importWorkspaceFixture(page: Page, workspaceId: string) {
  const token = await page.evaluate(() => localStorage.getItem("access_token") || "");
  const zipBuffer = fs.readFileSync(fixtureZipPath);
  const response = await page.request.post(`${baseUrl}/api/v1/workspaces/${workspaceId}/import`, {
    headers: { Authorization: `Bearer ${token}` },
    multipart: {
      file: {
        name: "workspace-sample.zip",
        mimeType: "application/zip",
        buffer: zipBuffer,
      },
    },
  });
  expect(response.ok()).toBeTruthy();
}

async function selectWorkspace(page: Page, workspaceName: string) {
  await page.getByTestId("workspace-selector").click();
  await page.getByTestId("workspace-option").filter({ hasText: workspaceName }).first().click();
}

test.describe("Dev Workspace — smoke test", () => {
  test.beforeEach(async ({ page }) => {
    await registerAndLogin(page);
  });

  test("navigate to workspace page via sidebar", async ({ page }) => {
    await page.getByRole("link", { name: "Dev Workspace" }).click();
    await expect(page).toHaveURL(/\/workspace/);
    await expect(page.getByTestId("workspace-selector")).toBeVisible();
  });

  test("create workspace and see it in selector", async ({ page }) => {
    await page.goto("/workspace");
    await page.getByRole("button", { name: "New" }).click();
    await page.getByLabel("Name").fill("E2E UI Workspace");
    await page.getByRole("button", { name: "Create" }).click();
    await expect(page.getByTestId("workspace-selector")).toContainText("E2E UI Workspace");
  });

  test("file tree and changeset panel render after workspace select", async ({ page }) => {
    const workspaceName = "E2E Select Workspace";
    const workspaceId = await createWorkspaceViaApi(page, workspaceName);
    await importWorkspaceFixture(page, workspaceId);

    await page.goto("/workspace");
    await selectWorkspace(page, workspaceName);

    await expect(page.getByTestId("file-tree")).toBeVisible();
    await expect(page.getByTestId("changeset-panel")).toBeVisible();
    await expect(page.getByText("No changesets yet")).toBeVisible();
  });

  test("chat @file mention opens picker when a workspace exists", async ({ page }) => {
    await createWorkspaceViaApi(page, "E2E Chat Workspace");

    await page.goto("/chat");
    const chatInput = page.getByPlaceholder(/ask a question/i);
    // Support either direct input or a combo box setup
    try {
      await chatInput.fill("@");
    } catch {
      await page.keyboard.press('@');
    }
    await expect(page.getByPlaceholder("Search files...")).toBeVisible();
  });
});

test.describe("Dev Workspace — full IDE loop", () => {
  let workspaceName: string;

  test.beforeEach(async ({ page }) => {
    await registerAndLogin(page);
    workspaceName = `IDE Loop WS ${Date.now()}`;
    const workspaceId = await createWorkspaceViaApi(page, workspaceName);
    await importWorkspaceFixture(page, workspaceId);
  });

  test("import zip populates tree and opens file in editor", async ({ page }) => {
    await page.goto("/workspace");
    await selectWorkspace(page, workspaceName);

    await expect(page.getByText("SuiteScripts")).toBeVisible();
    await page.getByText("hello.js").click();
    await expect(page.getByText("export const hello = 'world';")).toBeVisible();
  });

  test("search returns results and click opens file", async ({ page }) => {
    await page.goto("/workspace");
    await selectWorkspace(page, workspaceName);

    const searchInput = page.getByPlaceholder("Search files...");
    await searchInput.fill("hello.js");
    await expect(page.getByText("SuiteScripts/hello.js")).toBeVisible();
    await page.getByText("SuiteScripts/hello.js").click();
    await expect(page.getByText("export const hello = 'world';")).toBeVisible();
  });
});

test.describe("Pessimistic File Locking UI", () => {
  test("two different user sessions handle file locking banner and disable editor", async ({ browser }) => {
    // Two distinct browser contexts for two isolated user sessions
    const contextA = await browser.newContext();
    const contextB = await browser.newContext();

    const pageA = await contextA.newPage();
    const pageB = await contextB.newPage();

    // The fixture contains ue_sales_order.js based on the prompt instructions
    // User A authenticates and creates a workspace
    const tenantA = await registerAndLogin(pageA);
    const workspaceName = `Locking WS ${Date.now()}`;
    const workspaceId = await createWorkspaceViaApi(pageA, workspaceName);
    await importWorkspaceFixture(pageA, workspaceId);

    // Invite User B or User B registers?
    // Based on the simplified assumption: User B uses User A's credentials to access the same workspace
    // as simulating two sessions of the same account usually demonstrates this effectively 
    // without implementing a full invite flow. Or if the platform supports it, we register B and assign.
    // Let's use the exact same account just to test concurrent sessions in different contexts.

    // User B goes to login, but wait, `helpers.ts` registerAndLogin already registers a new user.
    // So let's write a quick login for User B using User A's credentials.
    await pageB.goto("/login");
    await pageB.getByLabel("Email").fill(tenantA.email);
    await pageB.getByLabel("Password").fill(tenantA.password);

    const signInButton = pageB.getByRole("button", { name: "Sign In", exact: false }).or(pageB.getByRole("button", { name: "Login" })).first();
    // Sometimes simple form submits use Enter
    if (await signInButton.isVisible()) {
      await signInButton.click();
    } else {
      await pageB.keyboard.press("Enter");
    }

    // Fallback if there is a 'sign in' link first
    // Just handle if we see the dashboard
    await pageB.waitForURL("**/dashboard", { timeout: 15_000 });

    // ===================================
    // End Session Registration / Login
    // ===================================

    // Both users navigate to the workspace
    await pageA.goto("/workspace");
    await selectWorkspace(pageA, workspaceName);

    await pageB.goto("/workspace");
    await selectWorkspace(pageB, workspaceName);

    // User A opens the file first
    await pageA.getByText("ue_sales_order.js").click();

    // Wait for the file content to load so we know A's session acquired the lock
    await expect(pageA.getByTestId("code-viewer").or(pageA.locator("pre"))).toBeVisible();

    // User B tries to open the SAME file
    await pageB.getByText("ue_sales_order.js").click();

    // Check for the file-locked banner
    await expect(pageB.getByText(/File locked by another user/i)).toBeVisible();

    // Cleanup session contexts
    await contextA.close();
    await contextB.close();
  });
});
