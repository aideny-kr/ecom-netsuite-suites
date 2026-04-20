import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { useAgentLabRun } from "../use-agent-lab-run";

class MockEventSource {
  url: string;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  listeners: Record<string, ((e: MessageEvent) => void)[]> = {};
  readyState = 0;

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
    setTimeout(() => { this.readyState = 1; this.onopen?.(); }, 0);
  }

  addEventListener(type: string, handler: (e: MessageEvent) => void) {
    (this.listeners[type] ??= []).push(handler);
  }

  close() { this.readyState = 2; }

  emit(type: string, data: unknown) {
    const evt = new MessageEvent(type, { data: JSON.stringify(data) });
    this.listeners[type]?.forEach(h => h(evt));
  }

  static instances: MockEventSource[] = [];
}

describe("useAgentLabRun", () => {
  beforeEach(() => {
    MockEventSource.instances = [];
    vi.stubGlobal("EventSource", MockEventSource);
  });
  afterEach(() => { vi.unstubAllGlobals(); });

  it("connects to events endpoint with run_id", () => {
    renderHook(() => useAgentLabRun("abc-123"));
    expect(MockEventSource.instances[0].url).toContain("/agent-lab/runs/abc-123/events");
  });

  it("accumulates case_complete events", async () => {
    const { result } = renderHook(() => useAgentLabRun("abc-123"));
    const es = MockEventSource.instances[0];
    await waitFor(() => expect(result.current.status).toBe("connecting"));

    es.emit("run_started", { total_cases: 18, estimated_cost_usd: 6.3 });
    await waitFor(() => expect(result.current.totalCases).toBe(18));

    es.emit("case_complete", {
      case_id: "c1",
      cases_completed: 1,
      running_cost_usd: 0.35,
      result: { verdict: "TIE" },
    });
    await waitFor(() => expect(result.current.casesCompleted).toBe(1));
    expect(result.current.runningCost).toBeCloseTo(0.35);
    expect(result.current.cases[0].case_id).toBe("c1");
  });

  it("sets status to completed on run_complete", async () => {
    const { result } = renderHook(() => useAgentLabRun("abc-123"));
    const es = MockEventSource.instances[0];
    es.emit("run_complete", { status: "completed", total_cost_usd: 5.84 });
    await waitFor(() => expect(result.current.status).toBe("completed"));
  });

  it("closes EventSource on unmount", () => {
    const { unmount } = renderHook(() => useAgentLabRun("abc-123"));
    const es = MockEventSource.instances[0];
    unmount();
    expect(es.readyState).toBe(2);  // CLOSED
  });
});
