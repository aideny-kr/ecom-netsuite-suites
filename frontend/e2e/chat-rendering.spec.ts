import { test, expect, type Page } from "@playwright/test";

const API = "http://localhost:8000";
const TOKEN =
  "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjQ3NDA4NDQ4MDB9.signature";
const NOW = "2026-03-09T12:00:00.000Z";

const WORKSPACE = {
  id: "ws-1",
  tenant_id: "t1",
  name: "Workspace One",
  description: null,
  status: "active",
  created_by: "u1",
  created_at: NOW,
  updated_at: NOW,
};

function buildSseResponse(events: unknown[]) {
  return events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("");
}

async function seedAuth(page: Page) {
  await page.context().addCookies([
    {
      name: "access_token",
      value: TOKEN,
      url: "http://localhost:3002",
    },
  ]);
  await page.addInitScript((token: string) => {
    localStorage.setItem("access_token", token);
  }, TOKEN);
}

async function assertViewportIsStable(page: Page) {
  const metrics = await page.evaluate(() => {
    const doc = document.documentElement;
    return {
      scrollWidth: doc.scrollWidth,
      innerWidth: window.innerWidth,
    };
  });

  expect(metrics.scrollWidth).toBeLessThanOrEqual(metrics.innerWidth + 1);
}

async function assertRichBlockOwnsScroll(page: Page) {
  const richBlock = page.getByTestId("assistant-rich-block").first();
  await expect(richBlock).toBeVisible();

  const hasInternalOverflow = await richBlock.evaluate((node) => {
    const scroller = node.firstElementChild as HTMLElement | null;
    if (!scroller) return false;
    return (
      scroller.scrollWidth > scroller.clientWidth ||
      scroller.scrollHeight > scroller.clientHeight
    );
  });

  expect(hasInternalOverflow).toBeTruthy();
}

async function setupMainChatMocks(page: Page, finalMessage: Record<string, unknown>) {
  const sessionId = "chat-session-1";
  let sessionCreated = false;
  let persistedMessages: Record<string, unknown>[] = [];

  await page.route(`${API}/**`, async (route) => {
    const url = new URL(route.request().url());
    const path = `${url.pathname}${url.search}`;

    if (path === "/api/v1/auth/me") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: "u1",
          tenant_id: "t1",
          tenant_name: "Test Tenant",
          email: "user@test.local",
          full_name: "Test User",
          role: "admin",
          onboarding_completed_at: NOW,
          created_at: NOW,
          updated_at: NOW,
        }),
      });
    }

    if (path === "/api/v1/auth/me/tenants") {
      return route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
    }

    if (path === "/api/v1/onboarding/checklist/connection/validate") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ valid: true }),
      });
    }

    if (path === "/api/v1/workspaces") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([WORKSPACE]),
      });
    }

    if (path === "/api/v1/chat/sessions" && route.request().method() === "GET") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          sessionCreated
            ? [
                {
                  id: sessionId,
                  title: "New chat",
                  workspace_id: null,
                  session_type: "chat",
                  is_archived: false,
                  created_at: NOW,
                  updated_at: NOW,
                },
              ]
            : [],
        ),
      });
    }

    if (path === "/api/v1/chat/sessions" && route.request().method() === "POST") {
      sessionCreated = true;
      return route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify({
          id: sessionId,
          title: null,
          workspace_id: null,
          session_type: "chat",
          is_archived: false,
          created_at: NOW,
          updated_at: NOW,
        }),
      });
    }

    if (path === `/api/v1/chat/sessions/${sessionId}`) {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: sessionId,
          title: "New chat",
          is_archived: false,
          created_at: NOW,
          updated_at: NOW,
          messages: persistedMessages,
        }),
      });
    }

    if (
      path === `/api/v1/chat/sessions/${sessionId}/messages` &&
      route.request().method() === "POST"
    ) {
      const body = route.request().postDataJSON() as { content?: string } | null;
      const userContent = body?.content || "Prompt";
      const assistantMessage = {
        id: "assistant-1",
        role: "assistant",
        content: String(finalMessage.content ?? ""),
        tool_calls: (finalMessage.tool_calls as unknown[]) ?? null,
        citations: (finalMessage.citations as unknown[]) ?? null,
        created_at: NOW,
      };

      persistedMessages = [
        {
          id: "user-1",
          role: "user",
          content: userContent,
          tool_calls: null,
          citations: null,
          created_at: NOW,
        },
        assistantMessage,
      ];

      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: buildSseResponse([
          { type: "text", content: "Streaming..." },
          { type: "message", message: assistantMessage },
        ]),
      });
    }

    return route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
  });
}

async function setupWorkspaceMocks(page: Page, finalMessage: Record<string, unknown>) {
  const sessionId = "workspace-chat-session-1";
  let sessionCreated = false;
  let persistedMessages: Record<string, unknown>[] = [];

  await page.route(`${API}/**`, async (route) => {
    const url = new URL(route.request().url());
    const path = `${url.pathname}${url.search}`;

    if (path === "/api/v1/auth/me") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: "u1",
          tenant_id: "t1",
          tenant_name: "Test Tenant",
          email: "user@test.local",
          full_name: "Test User",
          role: "admin",
          onboarding_completed_at: NOW,
          created_at: NOW,
          updated_at: NOW,
        }),
      });
    }

    if (path === "/api/v1/auth/me/tenants") {
      return route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
    }

    if (path === "/api/v1/onboarding/checklist/connection/validate") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ valid: true }),
      });
    }

    if (path === "/api/v1/workspaces") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([WORKSPACE]),
      });
    }

    if (path === `/api/v1/workspaces/${WORKSPACE.id}`) {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(WORKSPACE),
      });
    }

    if (path.startsWith(`/api/v1/workspaces/${WORKSPACE.id}/files?`)) {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            id: "file-1",
            name: "main.js",
            path: "SuiteScripts/main.js",
            is_directory: false,
          },
        ]),
      });
    }

    if (path === `/api/v1/workspaces/${WORKSPACE.id}/files/file-1`) {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: "file-1",
          path: "SuiteScripts/main.js",
          file_name: "main.js",
          content: "console.log('hello');",
          total_lines: 1,
          truncated: false,
        }),
      });
    }

    if (path === `/api/v1/chat/sessions?workspace_id=${WORKSPACE.id}`) {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          sessionCreated
            ? [
                {
                  id: sessionId,
                  title: "Workspace chat",
                  workspace_id: WORKSPACE.id,
                  session_type: "workspace",
                  is_archived: false,
                  created_at: NOW,
                  updated_at: NOW,
                },
              ]
            : [],
        ),
      });
    }

    if (path === "/api/v1/chat/sessions" && route.request().method() === "POST") {
      sessionCreated = true;
      return route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify({
          id: sessionId,
          title: null,
          workspace_id: WORKSPACE.id,
          session_type: "workspace",
          is_archived: false,
          created_at: NOW,
          updated_at: NOW,
        }),
      });
    }

    if (path === `/api/v1/chat/sessions/${sessionId}`) {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: sessionId,
          title: "Workspace chat",
          is_archived: false,
          created_at: NOW,
          updated_at: NOW,
          messages: persistedMessages,
        }),
      });
    }

    if (
      path === `/api/v1/chat/sessions/${sessionId}/messages` &&
      route.request().method() === "POST"
    ) {
      const body = route.request().postDataJSON() as { content?: string } | null;
      const assistantMessage = {
        id: "assistant-workspace-1",
        role: "assistant",
        content: String(finalMessage.content ?? ""),
        tool_calls: (finalMessage.tool_calls as unknown[]) ?? null,
        citations: null,
        created_at: NOW,
      };

      persistedMessages = [
        {
          id: "user-workspace-1",
          role: "user",
          content: body?.content || "Prompt",
          tool_calls: null,
          citations: null,
          created_at: NOW,
        },
        assistantMessage,
      ];

      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: buildSseResponse([{ type: "message", message: assistantMessage }]),
      });
    }

    return route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
  });
}

async function setupOnboardingMocks(page: Page, greeting: string) {
  await page.route(`${API}/**`, async (route) => {
    const url = new URL(route.request().url());
    const path = `${url.pathname}${url.search}`;

    if (path === "/api/v1/auth/me") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: "u1",
          tenant_id: "t1",
          tenant_name: "Test Tenant",
          email: "user@test.local",
          full_name: "Test User",
          role: "admin",
          onboarding_completed_at: null,
          created_at: NOW,
          updated_at: NOW,
        }),
      });
    }

    if (path === "/api/v1/auth/me/tenants") {
      return route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
    }

    if (path === "/api/v1/onboarding/checklist/connection/validate") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ valid: false }),
      });
    }

    if (path === "/api/v1/onboarding/checklist") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [
            { step_key: "profile", status: "pending" },
            { step_key: "connection", status: "pending" },
            { step_key: "policy", status: "pending" },
            { step_key: "workspace", status: "pending" },
            { step_key: "first_success", status: "pending" },
          ],
        }),
      });
    }

    if (path === "/api/v1/onboarding/chat/start") {
      return route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify({
          session_id: "onboarding-session-1",
          message: {
            id: "onboarding-assistant-1",
            role: "assistant",
            content: greeting,
            created_at: NOW,
          },
        }),
      });
    }

    if (path === "/api/v1/workspaces") {
      return route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
    }

    return route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
  });
}

test.describe("Chat Rendering", () => {
  test.beforeEach(async ({ page }) => {
    await seedAuth(page);
  });

  test("main chat contains wide markdown tables and code without page overflow", async ({ page }) => {
    const richMarkdown = [
      "Revenue summary for the selected period.",
      "",
      "| Region | Channel | Week 1 | Week 2 | Week 3 | Week 4 | Week 5 | Week 6 | Week 7 | Week 8 |",
      "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
      "| North America | Marketplace | 12345 | 23456 | 34567 | 45678 | 56789 | 67890 | 78901 | 89012 |",
      "",
      "```json",
      '{ "veryLongKeyNameThatShouldScroll": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" }',
      "```",
    ].join("\n");

    await setupMainChatMocks(page, {
      content: richMarkdown,
      tool_calls: null,
      citations: null,
    });

    await page.goto("/chat");
    await page.getByRole("textbox").fill("Show me the data");
    await page.getByRole("button", { name: /send message/i }).click();

    await assertRichBlockOwnsScroll(page);
    await assertViewportIsStable(page);
    await expect(page.getByRole("textbox")).toBeVisible();
  });

  test("main chat consumes final message events and renders structured SuiteQL results", async ({ page }) => {
    await setupMainChatMocks(page, {
      content: "Found 2 matching sales orders.",
      tool_calls: [
        {
          tool: "netsuite_suiteql",
          params: { query: "SELECT id, tranid, entity FROM transaction" },
          result_summary: "Returned 2 rows",
          result_payload: {
            kind: "table",
            columns: ["id", "tranid", "entity"],
            rows: [
              ["1", "SO1001", "Acme"],
              ["2", "SO1002", "Globex"],
            ],
            row_count: 2,
            truncated: false,
            query: "SELECT id, tranid, entity FROM transaction",
            limit: 100,
          },
          duration_ms: 27,
        },
      ],
      citations: null,
    });

    await page.goto("/chat");
    await page.getByRole("textbox").fill("Find sales orders");
    await page.getByRole("button", { name: /send message/i }).click();

    await expect(page.getByTestId("suiteql-result-card")).toBeVisible();
    await expect(page.getByText("SO1001")).toBeVisible();
    await assertViewportIsStable(page);
  });

  test("workspace chat keeps rich output contained without breaking panel layout", async ({ page }) => {
    const richMarkdown = [
      "Current workspace findings.",
      "",
      "| File | Issue | Severity |",
      "| --- | --- | --- |",
      "| SuiteScripts/main.js | Very wide diagnostic payload that should stay inside the chat panel | High |",
      "",
      "```ts",
      "const payload = '" + "x".repeat(180) + "';",
      "```",
    ].join("\n");

    await setupWorkspaceMocks(page, {
      content: richMarkdown,
      tool_calls: null,
    });

    await page.goto("/workspace?workspace=ws-1&file=SuiteScripts/main.js", {
      waitUntil: "networkidle",
    });
    await page.locator("button").filter({ hasText: "AI Chat" }).click();
    await page.locator("textarea").last().fill("Review the workspace");
    await page.getByRole("button", { name: /send message/i }).last().click();

    await expect(page.getByTestId("workspace-chat-panel")).toBeVisible();
    await assertRichBlockOwnsScroll(page);
    await assertViewportIsStable(page);
    await expect(page.locator('[role="separator"]')).toHaveCount(2);
  });

  test("onboarding chat renders long data output without expanding the page", async ({ page }) => {
    const greeting = [
      "Welcome to setup.",
      "",
      "| Step | Description | Status |",
      "| --- | --- | --- |",
      "| Business Profile | Extremely detailed onboarding text that should scroll inside the panel instead of resizing the overlay | Pending |",
      "",
      "```json",
      '{ "sample": "' + "x".repeat(200) + '" }',
      "```",
    ].join("\n");

    await setupOnboardingMocks(page, greeting);

    await page.goto("/dashboard");

    await expect(page.getByRole("heading", { name: "Business Profile" })).toBeVisible();
    await assertRichBlockOwnsScroll(page);
    await assertViewportIsStable(page);
  });
});
