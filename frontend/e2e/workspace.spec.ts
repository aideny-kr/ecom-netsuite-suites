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
    await chatInput.fill("@");
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
