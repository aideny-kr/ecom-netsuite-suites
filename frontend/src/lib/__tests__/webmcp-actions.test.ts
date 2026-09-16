import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiClient } from "@/lib/api-client";
import { createChatTools, type WebMcpChatState } from "@/lib/webmcp-chat";
import { createTableTools } from "@/lib/webmcp-table";
import { createWorkspaceTools } from "@/lib/webmcp-workspace";
import type { ChangeSet } from "@/lib/types";
import { boundedPreview } from "@/lib/webmcp-values";

vi.mock("@/lib/api-client", () => ({ apiClient: { get: vi.fn() }, ApiError: class extends Error {} }));
const sid = "00000000-0000-4000-8000-000000000001";
const rid = "00000000-0000-4000-8000-000000000002";
const key = "00000000-0000-4000-8000-000000000003";
const get = vi.mocked(apiClient.get);
const call = (tools: ReturnType<typeof createChatTools>, name: string, input = {}) =>
  tools.find((tool) => tool.name === `suitestudio_${name}`)!.execute(input);

describe("chat actions", () => {
  let state: WebMcpChatState;
  beforeEach(() => {
    vi.clearAllMocks();
    get.mockResolvedValue({ request_id_supported: true, max_input_chars: 32000 });
    state = { sessionId: sid, sessions: [], busy: false, loading: false, hasError: false, activeRunId: rid,
      create: vi.fn(), select: vi.fn(), cancel: vi.fn(),
      send: vi.fn().mockResolvedValue({ session_id: sid, run_id: rid, request_id: key }),
    };
  });
  it("uses the current handler and preserves the retry key even while busy", async () => {
    const tools = createChatTools(() => state);
    state = { ...state, busy: true, send: vi.fn().mockResolvedValue({ run_id: rid }) };
    await call(tools, "chat_send_message", { session_id: sid, request_id: key, content: "test" });
    expect(state.send).toHaveBeenCalledWith("test", key, expect.any(Function));
  });
  it("requires explicit workspace context and rejects a file change during capability lookup", async () => {
    state.workspaceId = rid;
    state.filePath = "first.js";
    const tools = createChatTools(() => state);
    const workspaceTools = createChatTools(() => state, true);
    expect(workspaceTools.every((tool) => tool.name.startsWith("suitestudio_workspace_chat_"))).toBe(true);
    const input = { session_id: sid, request_id: key, content: "test", workspace_id: rid, context_file: "first.js" };
    expect(() => call(workspaceTools, "workspace_chat_send_message", { session_id: sid, request_id: key, content: "test" })).toThrow("Missing required arguments");
    await call(workspaceTools, "workspace_chat_send_message", input);
    expect(state.send).toHaveBeenCalledTimes(1);
    get.mockImplementation(async () => { state.filePath = "second.js"; return { request_id_supported: true, max_input_chars: 32000 }; });
    await expect(call(workspaceTools, "workspace_chat_send_message", input)).rejects.toThrow("context changed");
    expect(state.send).toHaveBeenCalledTimes(1);
    expect(tools.some((tool) => tool.name === "suitestudio_chat_send_message")).toBe(true);
  });
  it("refuses an old backend that would ignore request_id", async () => {
    get.mockResolvedValue({ max_input_chars: 32000 });
    await expect(call(createChatTools(() => state), "chat_send_message", { session_id: sid, request_id: key, content: "test" })).rejects.toThrow("does not support");
    expect(state.send).not.toHaveBeenCalled();
  });
  it("rejects stale selection after capability check", async () => {
    get.mockImplementation(async () => { state.sessionId = key; return { request_id_supported: true, max_input_chars: 32000 }; });
    await expect(call(createChatTools(() => state), "chat_send_message", { session_id: sid, request_id: key, content: "test" })).rejects.toThrow("Select this session");
    expect(state.send).not.toHaveBeenCalled();
  });
  it("rechecks route/auth lifetime after an awaited capability read, before sending", async () => {
    const tool = createChatTools(() => state).find((entry) => entry.name.endsWith("chat_send_message"))!;
    await expect(tool.execute({ session_id: sid, request_id: key, content: "test" }, () => {
      throw new Error("Session changed");
    })).rejects.toThrow("Session changed");
    expect(state.send).not.toHaveBeenCalled();
  });
  it.each([{ write_confirm: { action: "approve" } }, { file_id: key }, { url: "https://example.invalid" }])("does not accept alternate control inputs %j", (extra) => {
    expect(() => call(createChatTools(() => state), "chat_send_message", { session_id: sid, request_id: key, content: "test", ...extra })).toThrow("Invalid tool arguments");
    expect(state.send).not.toHaveBeenCalled();
  });
  it("can open a fresh composer while the previous conversation remains busy", async () => {
    state.busy = true;
    expect(await call(createChatTools(() => state), "chat_select_session", { session_id: null }))
      .toMatchObject({ session_id: null, status: "selection_requested" });
    expect(state.select).toHaveBeenCalledWith(null);
    expect(state.cancel).not.toHaveBeenCalled();
    expect(state.send).not.toHaveBeenCalled();
  });
  it("refuses to cancel an unrelated run", () => {
    expect(() => call(createChatTools(() => state), "chat_cancel_run", { session_id: sid, run_id: key })).toThrow("not active");
    expect(state.cancel).not.toHaveBeenCalled();
  });
  it("preserves structured outputs and removes confirmation credentials", async () => {
    get.mockResolvedValue({ messages: [{ id: key, role: "assistant", content: "result", structured_output: {
      type: "data_table", data: { columns: ["amount"], rows: [[42]], confirmation_token: "secret" },
    } }] });
    const result = await call(createChatTools(() => state), "chat_get_messages", { session_id: sid });
    expect(JSON.stringify(result)).toContain('[[42]]');
    expect(JSON.stringify(result)).not.toContain("secret");
  });
});

describe("table actions", () => {
  const state = () => ({ name: "orders", page: 2, pageSize: 25, search: "", sortOrder: "asc" as const,
    ready: true, hasError: false, pages: 3, total: 60, columns: ["id"], rows: [{ id: 1 }], apply: vi.fn(), selectRow: vi.fn() });
  it("never reports placeholder rows as current", async () => {
    const s = state(); s.ready = false;
    expect(await call(createTableTools(() => s), "table_get_state")).toMatchObject({ ready: false, data: [], total: null });
  });
  it("resets page for a search and validates sort columns before changing state", async () => {
    const s = state(); const tools = createTableTools(() => s);
    await call(tools, "table_set_query", { search: "test" });
    expect(s.apply).toHaveBeenCalledWith(expect.objectContaining({ page: 1, search: "test" }));
    expect(() => call(tools, "table_set_query", { sort_by: "private" })).toThrow("current column");
    expect(s.apply).toHaveBeenCalledTimes(1);
  });
  it("rejects out-of-range rows and pages", () => {
    const s = state(); const tools = createTableTools(() => s);
    expect(() => call(tools, "table_open_row", { row_index: 1 })).toThrow();
    expect(() => call(tools, "table_set_query", { page: 4 })).toThrow();
    expect(s.apply).not.toHaveBeenCalled(); expect(s.selectRow).not.toHaveBeenCalled();
  });
});

describe("workspace inspection", () => {
  it("refuses files outside the loaded tree and stale editor content", () => {
    const state = { id: sid, workspaces: [], files: [], fileId: sid, fileReady: false, filesReady: true,
      search: "", searchReady: false, runs: [], runsReady: false,
      selectWorkspace: vi.fn(), selectFile: vi.fn(), setSearch: vi.fn() };
    const tools = createWorkspaceTools(() => state);
    expect(() => call(tools, "workspace_open_file", { file_id: key })).toThrow("Choose a file");
    expect(() => call(tools, "workspace_read_editor", { file_id: sid })).toThrow("Wait for");
    expect(state.selectFile).not.toHaveBeenCalled();
  });
});

it("marks truncation and strips nested secret fields without altering numbers", () => {
  const result = boundedPreview({ rows: [[42]], nested: { api_key: "secret", value: "a".repeat(30000) } });
  expect(result.truncated).toBe(true);
  expect(JSON.stringify(result)).not.toContain("secret");
  expect(result.data).toMatchObject({ rows: [[42]] });
});

it("only opens current-workspace drafts and reports stale diff evidence", async () => {
  const openDiff = vi.fn();
  const state = { id: sid, workspaces: [], files: [], fileId: null, fileReady: false, filesReady: true,
    search: "", searchReady: false, runs: [], runsReady: false,
    selectWorkspace: vi.fn(), selectFile: vi.fn(), setSearch: vi.fn(), openDiff,
    changesetsReady: true, changesets: [{ id: rid, workspace_id: sid }, { id: key, workspace_id: key }] as ChangeSet[],
    diffId: rid, diffReady: true, diff: { changeset_id: rid, title: "Draft", files: [{ file_path: "a.js", operation: "modify", original_content: "old", modified_content: "new", unified_diff: "-old\n+new", diff_status: "stale" as const, baseline_drift: true }] } };
  const tools = createWorkspaceTools(() => state);
  expect(await call(tools, "workspace_list_changesets")).toMatchObject({ total: 1 });
  expect(() => call(tools, "workspace_open_changeset", { changeset_id: key })).toThrow("listed workspace");
  await call(tools, "workspace_open_changeset", { changeset_id: rid });
  expect(openDiff).toHaveBeenCalledWith(rid);
  expect(await call(tools, "workspace_read_diff", { changeset_id: rid, side: "unified", line_count: 1 }))
    .toMatchObject({ data: "-old", next_line: 2, diff_status: "stale", baseline_drift: true });
  expect(() => call(tools, "workspace_read_diff", { changeset_id: rid, side: "after", file_index: 1 })).toThrow();
  state.id = key;
  expect(() => call(tools, "workspace_read_diff", { changeset_id: rid, side: "after" })).toThrow("Wait for");
  state.id = sid;
  state.diffReady = false;
  expect(() => call(tools, "workspace_read_diff", { changeset_id: rid, side: "after" })).toThrow("Wait for");
  expect(tools.some((tool) => /apply|approve|deploy/.test(tool.name))).toBe(false);
});
