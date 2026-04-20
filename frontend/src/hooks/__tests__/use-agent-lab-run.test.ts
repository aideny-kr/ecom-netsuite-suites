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
    // Guard: don't set readyState=1 if close() has already set it to 2.
    setTimeout(() => {
      if (this.readyState !== 2) {
        this.readyState = 1;
        this.onopen?.();
      }
    }, 0);
  }

  addEventListener(type: string, handler: (e: MessageEvent) => void) {
    (this.listeners[type] ??= []).push(handler);
  }

  close() { this.readyState = 2; }

  emit(type: string, data: unknown, lastEventId = "") {
    const evt = new MessageEvent(type, { data: JSON.stringify(data), lastEventId });
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

  it("connects to events endpoint with run_id and last_id=0-0 on first mount", () => {
    renderHook(() => useAgentLabRun("abc-123"));
    const url = MockEventSource.instances[0].url;
    expect(url).toContain("/agent-lab/runs/abc-123/events");
    // On first mount, last_id must be 0-0 (not empty) so backend sends all events
    expect(url).toContain("last_id=0-0");
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
      result: { case_id: "c1", verdict: "TIE" },
    });
    await waitFor(() => expect(result.current.casesCompleted).toBe(1));
    expect(result.current.runningCost).toBeCloseTo(0.35);
    expect(result.current.cases[0].case_id).toBe("c1");
  });

  it("does not duplicate cases when the same case_id is received twice", async () => {
    const { result } = renderHook(() => useAgentLabRun("abc-123"));
    const es = MockEventSource.instances[0];

    es.emit("run_started", { total_cases: 18, estimated_cost_usd: 6.3 });
    await waitFor(() => expect(result.current.totalCases).toBe(18));

    const casePayload = {
      case_id: "c1",
      cases_completed: 1,
      running_cost_usd: 0.35,
      result: { case_id: "c1", verdict: "TIE" },
    };
    // Emit the same case_complete twice (simulates reconnect replay)
    es.emit("case_complete", casePayload);
    es.emit("case_complete", casePayload);
    await waitFor(() => expect(result.current.casesCompleted).toBe(1));
    // Must only have one entry despite two emissions
    expect(result.current.cases).toHaveLength(1);
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

  it("closes EventSource on error to prevent native retry from replaying from 0-0", async () => {
    const { result } = renderHook(() => useAgentLabRun("abc-123"));
    const es = MockEventSource.instances[0];
    // Trigger the error event (hook registers a plain `() => void` listener)
    const errorEvt = new Event("error");
    es.listeners["error"]?.forEach(h => h(errorEvt as unknown as MessageEvent));
    await waitFor(() => expect(result.current.error).toBe("connection error"));
    // close() is called synchronously in the error handler; readyState=2 immediately
    await waitFor(() => expect(es.readyState).toBe(2));  // CLOSED — not left open for browser retry
  });
});
