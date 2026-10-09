import React from "react";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const nav = vi.hoisted(() => ({ query: "", push: vi.fn(), replace: vi.fn() }));
const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), streamGet: vi.fn() }));
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(nav.query),
  useRouter: () => ({ push: nav.push, replace: nav.replace }),
}));
vi.mock("@/lib/api-client", () => ({ apiClient: api }));
vi.mock("@/hooks/use-workspace", () => ({ useWorkspaces: () => ({ data: [] }) }));
vi.mock("@/hooks/use-agents", () => ({ useAgents: () => ({ data: [] }) }));
vi.mock("@/components/chat/chat-welcome", () => ({ ChatWelcome: () => <p>Start working</p> }));
vi.mock("@/components/chat/message-list", () => ({ MessageList: (p: any) => <div>{p.messages.map((m: any) => <p key={m.id}>{m.content}</p>)}</div> }));
vi.mock("@/components/chat/chat-input", () => ({ ChatInput: (p: any) => <button disabled={p.isLoading} onClick={() => p.onSend("Hello")}>Send message</button> }));
vi.mock("@/components/chat/session-sidebar", () => ({ SessionSidebar: (p: any) => <nav><button onClick={p.onNewChat}>New chat</button>{p.sessions.map((s: any) => <button key={s.id} onClick={() => p.onSelectSession(s.id)}>{s.title}</button>)}</nav> }));
import ChatPage from "../page";
const sessions = ["recent", "older"].map(id => ({ id, title: id, status: "idle" }));
const detail = (id: string) => ({ ...sessions.find(s => s.id === id), messages: [{ id: `msg-${id}`, role: "assistant", content: `Conversation ${id}` }] });
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const view = () => <QueryClientProvider client={client}><ChatPage /></QueryClientProvider>;
  return { ...render(view()), view };
}
beforeEach(() => {
  vi.clearAllMocks(); nav.query = "";
  Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) });
  api.get.mockImplementation(async (path: string) => path === "/api/v1/chat/sessions" ? sessions : detail(path.split("/").pop()!));
  api.post.mockImplementation(async (path: string) => path === "/api/v1/chat/sessions" ? { id: "created" } : { run_id: "run-new" });
  api.streamGet.mockResolvedValue(new Response(new ReadableStream({ start(c) { c.close(); } })));
});
afterEach(cleanup);
describe("durable Chat session navigation", () => {
  it("opens the URL session even when another conversation is newer", async () => {
    nav.query = "session=older"; mount();
    expect(await screen.findByText("Conversation older")).toBeInTheDocument();
    expect(api.get).not.toHaveBeenCalledWith("/api/v1/chat/sessions/recent");
  });
  it("writes the selected conversation into a reloadable URL", async () => {
    mount(); fireEvent.click(await screen.findByRole("button", { name: "older" }));
    expect(nav.push).toHaveBeenCalledWith("/chat?session=older", { scroll: false });
    expect(await screen.findByText("Conversation older")).toBeInTheDocument();
  });
  it("restores back/forward session selection without creating or sending", async () => {
    nav.query = "session=older"; const app = mount();
    await screen.findByText("Conversation older"); nav.query = "session=recent"; app.rerender(app.view());
    expect(await screen.findByText("Conversation recent")).toBeInTheDocument();
    expect(api.post).not.toHaveBeenCalled();
  });
  it("keeps New chat blank across a reload and preserves agent context", async () => {
    nav.query = "session=older&agent=bi-agent"; mount();
    await screen.findByText("Conversation older"); fireEvent.click(screen.getByRole("button", { name: "New chat" }));
    expect(nav.push).toHaveBeenCalledWith("/chat?agent=bi-agent&new_session=true", { scroll: false });
    expect(screen.queryByText("Conversation older")).not.toBeInTheDocument();
  });
  it("shows unavailable sessions and prevents sending into them", async () => {
    nav.query = "session=unavailable";
    api.get.mockImplementation(async (path: string) => { if (path.endsWith("/unavailable")) throw new Error("Session not found"); return sessions; });
    mount(); expect(await screen.findByRole("alert")).toHaveTextContent(/conversation.*unavailable/i);
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
    expect(api.post).not.toHaveBeenCalled();
  });
  it("canonicalizes a created session so the same conversation can resume", async () => {
    nav.query = "new_session=true&compose=Use+skill"; mount();
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/chat?session=created", { scroll: false }));
    expect(api.post.mock.calls.filter(c => c[0] === "/api/v1/chat/sessions")).toHaveLength(1);
  });
});

it("does not let a delayed session creation replace a conversation selected in the meantime", async () => {
  nav.query = "new_session=true";
  let finishCreate!: (s: { id: string }) => void;
  api.post.mockImplementation((path: string) => path === "/api/v1/chat/sessions" ? new Promise(resolve => { finishCreate = resolve; }) : Promise.resolve({ run_id: "late" }));
  mount(); fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  fireEvent.click(await screen.findByRole("button", { name: "older" }));
  await screen.findByText("Conversation older");
  finishCreate({ id: "late-created" });
  await waitFor(() => expect(api.get).toHaveBeenCalledWith("/api/v1/chat/sessions/older"));
  await new Promise(resolve => setTimeout(resolve, 10));
  expect(screen.getByText("Conversation older")).toBeInTheDocument();
  expect(nav.replace).not.toHaveBeenCalledWith("/chat?session=late-created", { scroll: false });
  expect(api.post.mock.calls.some(c => c[0].endsWith("/messages"))).toBe(false);
});

it("an old aborted stream cannot reset the active conversation's response", async () => {
  nav.query = "session=older";
  let rejectOld!: (e: Error) => void;
  api.streamGet.mockImplementationOnce(() => new Promise((_, reject) => { rejectOld = reject; }))
    .mockImplementationOnce(() => new Promise(() => {}));
  mount(); await screen.findByText("Conversation older");
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(api.streamGet).toHaveBeenCalledTimes(1));
  fireEvent.click(screen.getByRole("button", { name: "recent" })); await screen.findByText("Conversation recent");
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(api.streamGet).toHaveBeenCalledTimes(2));
  rejectOld(new DOMException("aborted", "AbortError"));
  await waitFor(() => expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled());
});

it.each(["new_session=true&", ""])("creates only one session for an investigate-prefill deep link (%s)", async (prefix) => {
  nav.query = `${prefix}prefill=Investigate+the+evidence`; mount();
  await waitFor(() => expect(api.post.mock.calls.some(c => c[0] === "/api/v1/chat/sessions/created/messages")).toBe(true));
  expect(api.post.mock.calls.filter(c => c[0] === "/api/v1/chat/sessions")).toHaveLength(1);
  expect(nav.replace).toHaveBeenCalledWith("/chat?session=created", { scroll: false });
});

it("preserves a legacy compose link without selecting previous history", async () => {
  nav.query = "compose=Use+skill"; mount();
  await screen.findByRole("button", { name: "older" });
  expect(nav.replace).not.toHaveBeenCalledWith("/chat?session=recent", { scroll: false });
  expect(api.get).not.toHaveBeenCalledWith("/api/v1/chat/sessions/recent");
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(api.post.mock.calls.filter(c => c[0] === "/api/v1/chat/sessions")).toHaveLength(1));
});

it("releases the composer after a terminal SSE error", async () => {
  nav.query = "session=older";
  api.streamGet.mockResolvedValue(new Response('data: {"type":"error","error":"Synthetic stream failure"}\n\n'));
  mount(); await screen.findByText("Conversation older"); fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  await screen.findByText("Synthetic stream failure");
  await waitFor(() => expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled());
});

it("releases a resumed run after persisted status becomes idle during history refresh", async () => {
  nav.query = "session=older";
  let listCalls = 0, detailCalls = 0;
  api.get.mockImplementation(async (path: string) => {
    if (path === "/api/v1/chat/sessions") { if (++listCalls > 1) await new Promise(r => setTimeout(r, 75)); return sessions; }
    return { ...detail("older"), status: ++detailCalls <= 2 ? "running" : "idle", active_run_id: detailCalls <= 2 ? "run-resumed" : null };
  });
  api.streamGet.mockResolvedValue(new Response('data: {"type":"message","message":{"id":"done","role":"assistant","content":"Done"}}\n\n'));
  mount(); await waitFor(() => expect(api.streamGet).toHaveBeenCalled());
  await waitFor(() => expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled());
});
