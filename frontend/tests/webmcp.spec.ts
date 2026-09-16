import { test, expect, type Page } from "@playwright/test";

const user = {
  id: "webmcp-test-user", tenant_id: "webmcp-test-tenant", tenant_name: "WebMCP Test",
  email: "webmcp@example.invalid", full_name: "WebMCP Tester", role: "owner",
  is_active: true, onboarding_completed_at: "2026-09-15T00:00:00Z",
};

// Native API shape in Chrome 152. This suite intentionally uses the real browser
// implementation, rather than installing a fake tool registry in the page.
interface NativeTool { name: string }
interface NativeContext {
  getTools(): Promise<NativeTool[]>;
  executeTool(tool: NativeTool, input: string): Promise<string>;
}

async function listTools(page: Page) {
  return page.evaluate(async () => {
    const context = (document as Document & { modelContext: NativeContext }).modelContext;
    return (await context.getTools()).map((tool) => tool.name).filter((name) => name.startsWith("suitestudio_"));
  });
}

async function callTool(page: Page, name: string, input = {}) {
  return page.evaluate(async ({ name, input }) => {
    const context = (document as Document & { modelContext: NativeContext }).modelContext;
    const tool = (await context.getTools()).find((entry) => entry.name === `suitestudio_${name}`);
    if (!tool) throw new Error(`Tool missing: ${name}`);
    return context.executeTool(tool, JSON.stringify(input));
  }, { name, input });
}

test.beforeEach(async ({ page, context, baseURL }) => {
  await context.addCookies([{ name: "access_token", value: "webmcp-test-token", url: baseURL! }]);
  await page.addInitScript(() => localStorage.setItem("access_token", "webmcp-test-token"));
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const fixtures: Record<string, unknown> = {
      "/api/v1/auth/me": user,
      "/api/v1/auth/me/tenants": [],
      "/api/v1/settings/features": { flags: { chat: true, workspace: false, reconciliation: false } },
      "/api/v1/settings/branding": {},
      "/api/v1/agents": [],
      "/api/v1/connection-alerts": [],
      "/api/v1/onboarding/checklist/connection/validate": { valid: true },
      "/api/v1/connections/health": {
        connections: [{ id: "demo", label: "Demo NetSuite", provider: "netsuite", status: "active", token_expired: false, tool_count: null, client_id: "must-not-expose", restlet_url: "https://private.invalid" }],
        mcp_connectors: [],
      },
      "/api/v1/audit-events": { items: [], total: 0, page: 1, page_size: 25, total_pages: 0 },
      "/api/v1/dashboard": { active: null, published: [] },
    };
    if (path === "/api/v1/auth/logout") return route.fulfill({ json: {} });
    if (route.request().method() !== "GET") return route.fulfill({ status: 405, json: { detail: "Test is read-only" } });
    return route.fulfill({ json: fixtures[path] ?? [] });
  });
});

test("native discovery, structured reads, SPA navigation, and logout cleanup", async ({ page }) => {
  await page.goto("/dashboard?private_query=must-not-expose");
  await expect.poll(() => listTools(page)).toHaveLength(3);
  const initial = JSON.parse(await callTool(page, "get_page_context"));
  expect(initial.pathname).toBe("/dashboard");
  expect(initial.tenant.id).toBe(user.tenant_id);
  expect(initial.navigation_targets).not.toContainEqual(expect.objectContaining({ path: "/workspace" }));
  expect(JSON.stringify(initial)).not.toContain("must-not-expose");

  const health = await callTool(page, "get_connection_status");
  expect(JSON.parse(health).connections[0].status).toBe("active");
  expect(health).not.toMatch(/must-not-expose|private.invalid|webmcp-test-token/);

  await callTool(page, "navigate", { path: "/audit" });
  await expect(page).toHaveURL(/\/audit$/);
  await expect(page.getByRole("heading", { name: "Audit Log" })).toBeVisible();
  await expect.poll(async () => JSON.parse(await callTool(page, "get_page_context")).pathname).toBe("/audit");
  expect(await listTools(page)).toHaveLength(3);

  await page.getByRole("button", { name: /sign out/i }).click();
  await expect(page).toHaveURL(/\/login$/);
  await expect.poll(() => listTools(page)).toHaveLength(0);
});

test("backend permission denial is preserved", async ({ page }) => {
  await page.route("**/api/v1/connections/health", (route) => route.fulfill({ status: 403, json: { detail: "private reason" } }));
  await page.goto("/dashboard");
  await expect.poll(() => listTools(page)).toHaveLength(3);
  const denied = page.waitForResponse((response) => response.url().endsWith("/api/v1/connections/health"));
  // Chrome deliberately normalizes thrown tool errors to UnknownError rather
  // than propagating private exception messages to the calling agent.
  await expect(callTool(page, "get_connection_status")).rejects.toThrow(/invocation failed/);
  expect((await denied).status()).toBe(403);
});

test("the app still renders and navigates when WebMCP is unavailable", async ({ page }) => {
  await page.addInitScript(() => Object.defineProperty(document, "modelContext", { value: undefined }));
  await page.goto("/dashboard");
  await page.getByRole("link", { name: "Audit Log", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Audit Log" })).toBeVisible();
});

const sessionId = "10000000-0000-4000-8000-000000000001";
const runId = "10000000-0000-4000-8000-000000000002";
const requestId = "10000000-0000-4000-8000-000000000003";

test("native chat uses UI submission, receipts, structured output and route cleanup", async ({ page }) => {
  const messages: Record<string, unknown>[] = [];
  let submissions = 0;
  const receipts = new Set<string>();
  const session = { id: sessionId, title: "WebMCP chat fixture", is_archived: false, status: "idle", created_at: "2026-09-15T00:00:00Z", updated_at: "2026-09-15T00:00:00Z" };
  await page.route("**/api/v1/chat/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/health")) return route.fulfill({ json: { status: "ok", max_input_chars: 32000, request_id_supported: true } });
    if (path.endsWith("/sessions")) return route.fulfill({ json: [session] });
    if (path.endsWith(`/sessions/${sessionId}`)) return route.fulfill({ json: { ...session, messages } });
    if (path.endsWith("/messages")) {
      const body = route.request().postDataJSON();
      const replayed = receipts.has(body.request_id);
      if (!replayed) {
        submissions++;
        receipts.add(body.request_id);
        messages.push({ id: requestId, role: "user", content: body.content, tool_calls: null, citations: null, created_at: session.created_at });
        messages.push({ id: runId, role: "assistant", content: "Fixture response: 42 orders.", tool_calls: null, citations: null, created_at: session.created_at,
          structured_output: { type: "data_table", data: { columns: ["orders"], rows: [[42]], row_count: 1 } } });
      }
      return route.fulfill({ status: 202, json: { session_id: sessionId, run_id: runId, request_id: body.request_id, replayed } });
    }
    if (path.endsWith("/stream")) return route.fulfill({ contentType: "text/event-stream", body: `data: ${JSON.stringify({ type: "message", message: messages.at(-1) })}\n\ndata: {"type":"run_status","status":"complete"}\n\n` });
    if (path.endsWith(`/runs/${runId}`)) return route.fulfill({ json: { run_id: runId, session_id: sessionId, status: "complete", terminal: true, outcome: "complete" } });
    return route.fulfill({ json: [] });
  });
  await page.goto("/chat");
  await expect.poll(async () => (await listTools(page)).includes("suitestudio_chat_send_message")).toBe(true);
  await expect.poll(async () => JSON.parse(await callTool(page, "chat_get_state")).session_id).toBe(sessionId);
  const input = { session_id: sessionId, request_id: requestId, content: "Count fixture orders" };
  const receipt = JSON.parse(await callTool(page, "chat_send_message", input));
  expect(receipt.run_id).toBe(runId);
  await expect(page.getByText("Fixture response: 42 orders.", { exact: true })).toBeVisible();
  const retry = JSON.parse(await callTool(page, "chat_send_message", input));
  expect(retry.replayed).toBe(true);
  expect(submissions).toBe(1);
  const output = JSON.parse(await callTool(page, "chat_get_messages", { session_id: sessionId }));
  expect(output.data[1].structured_output.data.rows).toEqual([[42]]);
  const status = JSON.parse(await callTool(page, "chat_get_run", { run_id: runId }));
  expect(status.terminal).toBe(true);
  const invalid = JSON.parse(await callTool(page, "chat_send_message", { ...input, write_confirm: { action: "approve" } }));
  expect(invalid.error.message).toContain("Invalid tool arguments");
  expect(submissions).toBe(1);
  await callTool(page, "chat_select_session", { session_id: null });
  await expect.poll(async () => JSON.parse(await callTool(page, "chat_get_state")).session_id).toBe(null);
  await callTool(page, "navigate", { path: "/audit" });
  await expect.poll(() => listTools(page)).toHaveLength(3);
});

test("native table search changes the visible UI and reports fresh rows", async ({ page }) => {
  await page.route("**/api/v1/tables/payments?**", async (route) => {
    const params = new URL(route.request().url()).searchParams;
    const search = params.get("search") || "";
    return route.fulfill({ json: { items: [{ id: "row-1", source_id: search || "all payments" }], page: Number(params.get("page")), page_size: Number(params.get("page_size")), total: 1, pages: 1 } });
  });
  await page.goto("/tables/payments");
  await expect.poll(async () => (await listTools(page)).includes("suitestudio_table_get_state")).toBe(true);
  await expect.poll(async () => JSON.parse(await callTool(page, "table_get_state")).ready).toBe(true);
  await callTool(page, "table_set_query", { search: "webmcp fixture", page_size: 10 });
  await expect(page.getByPlaceholder(/search/i).first()).toHaveValue("webmcp fixture");
  await expect.poll(async () => {
    const state = JSON.parse(await callTool(page, "table_get_state"));
    return state.ready && state.data[0]?.source_id;
  }).toBe("webmcp fixture");
  await expect(page.getByRole("cell", { name: "webmcp fixture" })).toBeVisible();
});

test("native workspace inspects files and drafts and runs context-bound chat", async ({ page }) => {
  const fileId = "20000000-0000-4000-8000-000000000001";
  const workspaceId = "20000000-0000-4000-8000-000000000002";
  const draftId = "20000000-0000-4000-8000-000000000004";
  const draft = { id: draftId, workspace_id: workspaceId, title: "Review fixture change", status: "draft", created_at: "2026-09-15T00:00:00Z", updated_at: "2026-09-15T00:00:00Z" };
  const messages: Record<string, unknown>[] = [];
  let posts = 0;
  let createBody: unknown;
  const session = { id: sessionId, workspace_id: workspaceId, title: "Workspace fixture chat", is_archived: false, status: "idle", created_at: draft.created_at, updated_at: draft.updated_at };
  await page.route("**/api/v1/chat/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/health")) return route.fulfill({ json: { max_input_chars: 32000, request_id_supported: true } });
    if (path.endsWith("/sessions")) {
      if (route.request().method() === "POST") { createBody = route.request().postDataJSON(); return route.fulfill({ json: session }); }
      return route.fulfill({ json: createBody ? [session] : [] });
    }
    if (path.endsWith(`/sessions/${sessionId}`)) return route.fulfill({ json: { ...session, messages } });
    if (path.endsWith("/messages")) {
      posts++;
      const body = route.request().postDataJSON();
      if (!messages.length) messages.push({ id: requestId, role: "user", content: body.content, created_at: draft.created_at }, { id: runId, role: "assistant", content: "Workspace response ready.", created_at: draft.created_at });
      return route.fulfill({ status: 202, json: { session_id: sessionId, run_id: runId, request_id: body.request_id, replayed: posts > 1 } });
    }
    if (path.endsWith("/stream")) return route.fulfill({ contentType: "text/event-stream", body: `data: ${JSON.stringify({ type: "message", message: messages.at(-1) })}\n\ndata: {"type":"run_status","status":"complete"}\n\n` });
    return route.fulfill({ json: [] });
  });
  let draftWrites = 0;
  await page.route("**/api/v1/changesets/**", (route) => {
    if (route.request().method() !== "GET") { draftWrites++; return route.fulfill({ status: 405, json: {} }); }
    return route.fulfill({ json: { changeset_id: draftId, title: draft.title, files: [{ file_path: "fixture.js", operation: "modify", original_content: "const count = 42;", modified_content: "const count = 43;", unified_diff: "-const count = 42;\n+const count = 43;", diff_status: "stale", baseline_drift: true }] } });
  });
  await page.route("**/api/v1/settings/features", (route) => route.fulfill({ json: { flags: { chat: true, workspace: true, celigo: false } } }));
  await page.route("**/api/v1/workspaces**", (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/workspaces")) return route.fulfill({ json: [{ id: workspaceId, name: "WebMCP fixture", created_at: "2026-09-15T00:00:00Z" }] });
    if (url.pathname.endsWith(`/files/${fileId}`)) return route.fulfill({ json: { id: fileId, path: "fixture.js", file_name: "fixture.js", content: "// fixture source\nexport const count = 42;", total_lines: 2, truncated: false, mime_type: "text/javascript" } });
    if (url.pathname.endsWith("/files")) return route.fulfill({ json: [{ id: fileId, name: "fixture.js", path: "fixture.js", is_directory: false }] });
    if (url.pathname.endsWith("/changesets")) return route.fulfill({ json: [draft] });
    if (url.pathname.endsWith("/runs")) return route.fulfill({ json: [{ id: runId, workspace_id: workspaceId, run_type: "validate", status: "passed", exit_code: 0 }] });
    return route.fulfill({ json: [] });
  });
  await page.goto("/workspace");
  await expect.poll(async () => (await listTools(page)).includes("suitestudio_workspace_get_state")).toBe(true);
  await expect.poll(async () => JSON.parse(await callTool(page, "workspace_get_state")).workspaces.length).toBe(1);
  await callTool(page, "workspace_select", { workspace_id: workspaceId });
  await expect.poll(async () => JSON.parse(await callTool(page, "workspace_get_state")).files_ready).toBe(true);
  const files = JSON.parse(await callTool(page, "workspace_list_files"));
  expect(files.data[0].id).toBe(fileId);
  await callTool(page, "workspace_open_file", { file_id: fileId });
  await expect.poll(async () => JSON.parse(await callTool(page, "workspace_get_state")).file_ready).toBe(true);
  const file = JSON.parse(await callTool(page, "workspace_read_editor", { file_id: fileId, start_line: 2, line_count: 1 }));
  expect(file.data).toBe("export const count = 42;");
  await expect(page.getByText("fixture.js", { exact: true }).first()).toBeVisible();
  const runs = JSON.parse(await callTool(page, "workspace_get_runs"));
  expect(runs.data[0].status).toBe("passed");
  await callTool(page, "workspace_set_panel", { panel: "chat" });
  await expect.poll(() => listTools(page)).toHaveLength(22);
  await expect(page.getByTestId("workspace-chat-panel")).toBeVisible();
  await callTool(page, "workspace_chat_create_session");
  expect(createBody).toEqual({ workspace_id: workspaceId });
  await expect.poll(async () => JSON.parse(await callTool(page, "workspace_chat_get_state")).ready).toBe(true);
  const content = "Explain this file. " + "x".repeat(5000);
  const input = { session_id: sessionId, request_id: requestId, workspace_id: workspaceId, context_file: "fixture.js", content };
  expect(JSON.parse(await callTool(page, "workspace_chat_send_message", input)).run_id).toBe(runId);
  await expect(page.getByText("Workspace response ready.", { exact: true })).toBeVisible();
  expect(messages[0].content).toBe(`[Currently viewing file: fixture.js]\n\n${content}`);
  expect(JSON.parse(await callTool(page, "workspace_chat_send_message", input)).replayed).toBe(true);
  const stale = JSON.parse(await callTool(page, "workspace_chat_send_message", { ...input, context_file: "another.js" }));
  expect(stale.error.message).toContain("context changed");
  expect(posts).toBe(2);
  await callTool(page, "workspace_set_panel", { panel: "changesets" });
  await expect.poll(() => listTools(page)).toHaveLength(15);
  await expect.poll(async () => JSON.parse(await callTool(page, "workspace_get_state")).changesets_ready).toBe(true);
  expect(JSON.parse(await callTool(page, "workspace_list_changesets")).data[0].id).toBe(draftId);
  await callTool(page, "workspace_open_changeset", { changeset_id: draftId });
  await expect.poll(async () => JSON.parse(await callTool(page, "workspace_get_state")).diff_ready).toBe(true);
  const diff = JSON.parse(await callTool(page, "workspace_read_diff", { changeset_id: draftId, side: "unified" }));
  expect(diff).toMatchObject({ diff_status: "stale", baseline_drift: true, data: "-const count = 42;\n+const count = 43;" });
  expect(JSON.parse(await callTool(page, "workspace_get_state")).file_ready).toBe(false);
  await expect(page.getByText(draft.title, { exact: true }).first()).toBeVisible();
  expect(draftWrites).toBe(0);
  await page.screenshot({ path: test.info().outputPath("workspace-draft.png"), fullPage: true });
});

test("native chat cancellation reports a request and waits for terminal status", async ({ page }) => {
  let status = "running";
  let releaseStream: (() => void) | undefined;
  let cancelCalls = 0;
  const session = { id: sessionId, title: "Cancellable fixture", is_archived: false, created_at: "2026-09-15T00:00:00Z", updated_at: "2026-09-15T00:00:00Z" };
  await page.route("**/api/v1/chat/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/health")) return route.fulfill({ json: { status: "ok", max_input_chars: 32000, request_id_supported: true } });
    const active = status === "running" || status === "cancelling";
    if (path.endsWith("/sessions")) return route.fulfill({ json: [{ ...session, status: active ? status : "idle", active_run_id: active ? runId : null }] });
    if (path.endsWith(`/sessions/${sessionId}`)) return route.fulfill({ json: { ...session, status: active ? status : "idle", active_run_id: active ? runId : null, messages: [] } });
    if (path.endsWith("/stream")) {
      await new Promise<void>((resolve) => { releaseStream = resolve; });
      status = "cancelled";
      return route.fulfill({ contentType: "text/event-stream", body: 'data: {"type":"run_status","status":"cancelled"}\n\n' });
    }
    if (path.endsWith("/cancel")) {
      cancelCalls++;
      status = "cancelling";
      await route.fulfill({ json: { run_id: runId, status: "cancelling" } });
      releaseStream?.();
      return;
    }
    if (path.endsWith(`/runs/${runId}`)) return route.fulfill({ json: { run_id: runId, session_id: sessionId, status, terminal: status === "cancelled" } });
    return route.fulfill({ json: [] });
  });
  await page.goto("/chat");
  await expect.poll(() => !!releaseStream).toBe(true);
  const request = JSON.parse(await callTool(page, "chat_cancel_run", { session_id: sessionId, run_id: runId }));
  expect(request.status).toBe("cancelling");
  await expect.poll(async () => JSON.parse(await callTool(page, "chat_get_run", { run_id: runId })).status).toBe("cancelled");
  await expect.poll(async () => JSON.parse(await callTool(page, "chat_get_state")).busy).toBe(false);
  expect(cancelCalls).toBe(1);
});

test("navigation during capability lookup prevents a stale chat submission", async ({ page }) => {
  let holdHealth = false;
  let releaseHealth: (() => void) | undefined;
  let submissions = 0;
  const session = { id: sessionId, title: "Race fixture", is_archived: false, status: "idle", created_at: "2026-09-15T00:00:00Z", updated_at: "2026-09-15T00:00:00Z" };
  await page.route("**/api/v1/chat/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/health")) {
      if (holdHealth) await new Promise<void>((resolve) => { releaseHealth = resolve; });
      return route.fulfill({ json: { request_id_supported: true, max_input_chars: 32000 } });
    }
    if (path.endsWith("/sessions")) return route.fulfill({ json: [session] });
    if (path.endsWith(`/sessions/${sessionId}`)) return route.fulfill({ json: { ...session, messages: [] } });
    if (path.endsWith("/messages")) { submissions++; return route.fulfill({ status: 202, json: { run_id: runId, session_id: sessionId } }); }
    return route.fulfill({ json: [] });
  });
  await page.goto("/chat");
  await expect.poll(async () => (await listTools(page)).includes("suitestudio_chat_get_state")).toBe(true);
  await expect.poll(async () => JSON.parse(await callTool(page, "chat_get_state")).session_id).toBe(sessionId);
  holdHealth = true;
  const pending = callTool(page, "chat_send_message", { session_id: sessionId, request_id: requestId, content: "Do not dispatch after navigation" }).catch(() => null);
  await expect.poll(() => !!releaseHealth).toBe(true);
  await callTool(page, "navigate", { path: "/audit" });
  await expect(page).toHaveURL(/\/audit$/);
  await expect.poll(() => listTools(page)).toHaveLength(3);
  releaseHealth!();
  await pending;
  expect(submissions).toBe(0);
});

test("a late submission receipt cannot reopen the previously selected chat stream", async ({ page }) => {
  const secondId = "10000000-0000-4000-8000-000000000004";
  let releaseSubmit: (() => void) | undefined;
  let streamCalls = 0;
  const session = { id: sessionId, title: "First", is_archived: false, status: "idle", created_at: "2026-09-15T00:00:00Z", updated_at: "2026-09-15T00:00:00Z" };
  const second = { ...session, id: secondId, title: "Second" };
  await page.route("**/api/v1/chat/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/health")) return route.fulfill({ json: { request_id_supported: true, max_input_chars: 32000 } });
    if (path.endsWith("/sessions")) return route.fulfill({ json: [session, second] });
    if (path.endsWith(`/sessions/${sessionId}`)) return route.fulfill({ json: { ...session, messages: [] } });
    if (path.endsWith(`/sessions/${secondId}`)) return route.fulfill({ json: { ...second, messages: [] } });
    if (path.endsWith("/messages")) {
      await new Promise<void>((resolve) => { releaseSubmit = resolve; });
      return route.fulfill({ status: 202, json: { run_id: runId, session_id: sessionId } });
    }
    if (path.endsWith("/stream")) { streamCalls++; return route.fulfill({ contentType: "text/event-stream", body: "" }); }
    return route.fulfill({ json: [] });
  });
  await page.goto("/chat");
  await expect.poll(async () => (await listTools(page)).includes("suitestudio_chat_get_state")).toBe(true);
  await expect.poll(async () => JSON.parse(await callTool(page, "chat_get_state")).session_id).toBe(sessionId);
  const pending = callTool(page, "chat_send_message", { session_id: sessionId, request_id: requestId, content: "Receipt race" });
  await expect.poll(() => !!releaseSubmit).toBe(true);
  await callTool(page, "chat_select_session", { session_id: secondId });
  await expect.poll(async () => JSON.parse(await callTool(page, "chat_get_state")).session_id).toBe(secondId);
  releaseSubmit!();
  const result = JSON.parse(await pending);
  expect(result.error).toBeDefined();
  const current = JSON.parse(await callTool(page, "chat_get_state"));
  expect(current.session_id).toBe(secondId);
  expect(current.busy).toBe(false);
  expect(streamCalls).toBe(0);
});
