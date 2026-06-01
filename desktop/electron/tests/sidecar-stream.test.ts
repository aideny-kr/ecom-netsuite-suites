/**
 * Tests for the STREAMING Sidecar API (electron/sidecar.ts::runAgentStream).
 *
 * The rich-pipe slice upgrades the wrapper from "resolve on first line" to
 * "emit each line as a typed event until done". A request sets stream:true and
 * each newline-JSON event (text / data_table / done) is delivered to onEvent in
 * order; the promise resolves on the terminal `done`. Malformed or {error:...}
 * lines are surfaced as a typed error event (folding the B0 deliver() MINOR:
 * never silently drop a protocol line). Single-shot runAgent stays back-compat.
 *
 * Same isolation as sidecar.test.ts: child_process.spawn is mocked, so no real
 * Python ever starts.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";

interface FakeChild extends EventEmitter {
  stdin: PassThrough;
  stdout: PassThrough;
  stderr: PassThrough;
  kill: ReturnType<typeof vi.fn>;
  killed: boolean;
  pid: number;
}

function makeFakeChild(): FakeChild {
  const ee = new EventEmitter() as FakeChild;
  ee.stdin = new PassThrough();
  ee.stdout = new PassThrough();
  ee.stderr = new PassThrough();
  ee.kill = vi.fn(() => {
    ee.killed = true;
    return true;
  });
  ee.killed = false;
  ee.pid = 4242;
  return ee;
}

const spawnMock = vi.fn();
vi.mock("node:child_process", () => ({
  spawn: (...args: unknown[]) => spawnMock(...args),
}));

import { Sidecar } from "../sidecar";

const flush = () => new Promise((r) => setImmediate(r));

beforeEach(() => {
  spawnMock.mockReset();
});

describe("Sidecar.runAgentStream()", () => {
  it("sends a streaming run request (action:run, stream:true) to stdin", async () => {
    const fake = makeFakeChild();
    spawnMock.mockReturnValue(fake);
    const s = new Sidecar({ pythonPath: "python", sidecarPath: "/x.py" });
    s.start();

    const writes: string[] = [];
    fake.stdin.on("data", (c: Buffer) => writes.push(c.toString()));

    const done = s.runAgentStream("show data", () => {});
    await flush();

    const written = JSON.parse(writes.join("").trim());
    expect(written).toEqual({ action: "run", query: "show data", stream: true });

    fake.stdout.write(JSON.stringify({ type: "done", tokens_used: 0 }) + "\n");
    await done;
  });

  it("invokes onEvent once per event in order and resolves on done", async () => {
    const fake = makeFakeChild();
    spawnMock.mockReturnValue(fake);
    const s = new Sidecar({ pythonPath: "python", sidecarPath: "/x.py" });
    s.start();

    const events: Array<Record<string, unknown>> = [];
    const done = s.runAgentStream("q", (e) => events.push(e));
    await flush();

    fake.stdout.write(
      JSON.stringify({ type: "text", content: "Here are the balances:" }) + "\n" +
        JSON.stringify({
          type: "data_table",
          data: { columns: ["Account"], rows: [["Cash"]], row_count: 1, query: "", truncated: false },
        }) + "\n" +
        JSON.stringify({ type: "done", tokens_used: 42 }) + "\n",
    );

    await done;
    expect(events.map((e) => e.type)).toEqual(["text", "data_table", "done"]);
    expect(events[1].data).toEqual({
      columns: ["Account"],
      rows: [["Cash"]],
      row_count: 1,
      query: "",
      truncated: false,
    });
    expect((events[2] as { tokens_used: number }).tokens_used).toBe(42);
  });

  it("surfaces a malformed line as an error event (never silently dropped)", async () => {
    const fake = makeFakeChild();
    spawnMock.mockReturnValue(fake);
    const s = new Sidecar({ pythonPath: "python", sidecarPath: "/x.py" });
    s.start();

    const events: Array<Record<string, unknown>> = [];
    const done = s.runAgentStream("q", (e) => events.push(e));
    await flush();

    fake.stdout.write("this is not json\n");
    await done; // malformed line is terminal for the stream

    expect(events).toHaveLength(1);
    expect(events[0].type).toBe("error");
    expect(String(events[0].error)).toMatch(/malformed/i);
  });

  it("normalizes a sidecar {error:...} line into a typed error event and finalizes", async () => {
    const fake = makeFakeChild();
    spawnMock.mockReturnValue(fake);
    const s = new Sidecar({ pythonPath: "python", sidecarPath: "/x.py" });
    s.start();

    const events: Array<Record<string, unknown>> = [];
    const done = s.runAgentStream("q", (e) => events.push(e));
    await flush();

    fake.stdout.write(JSON.stringify({ error: "ANTHROPIC_API_KEY not set" }) + "\n");
    await done;

    expect(events).toEqual([{ type: "error", error: "ANTHROPIC_API_KEY not set" }]);
  });

  it("rejects the stream promise if the child exits before done", async () => {
    const fake = makeFakeChild();
    spawnMock.mockReturnValue(fake);
    const s = new Sidecar({ pythonPath: "python", sidecarPath: "/x.py" });
    s.start();

    const pending = s.runAgentStream("orphan", () => {});
    await flush();
    fake.emit("exit", 1, null);

    await expect(pending).rejects.toThrow(/exit|crashed|terminated|before responding/i);
  });

  it("serializes concurrent streams — the second waits until the first finalizes", async () => {
    const fake = makeFakeChild();
    spawnMock.mockReturnValue(fake);
    const s = new Sidecar({ pythonPath: "python", sidecarPath: "/x.py" });
    s.start();

    const writes: string[] = [];
    fake.stdin.on("data", (c: Buffer) => writes.push(c.toString()));

    const a: Array<Record<string, unknown>> = [];
    const b: Array<Record<string, unknown>> = [];
    const p1 = s.runAgentStream("first", (e) => a.push(e));
    s.runAgentStream("second", (e) => b.push(e));
    await flush();

    // Only the first stream's request has been written; the second is queued.
    expect(writes.join("")).toContain('"query":"first"');
    expect(writes.join("")).not.toContain('"query":"second"');

    // Finalize the first; the second is then dispatched.
    fake.stdout.write(JSON.stringify({ type: "done", tokens_used: 0 }) + "\n");
    await p1;
    await flush();
    expect(writes.join("")).toContain('"query":"second"');

    // First stream's events never leaked into the second's callback.
    expect(b).toHaveLength(0);
  });

  it("does not invoke onEvent after the terminal done", async () => {
    const fake = makeFakeChild();
    spawnMock.mockReturnValue(fake);
    const s = new Sidecar({ pythonPath: "python", sidecarPath: "/x.py" });
    s.start();

    const events: Array<Record<string, unknown>> = [];
    const done = s.runAgentStream("q", (e) => events.push(e));
    await flush();
    fake.stdout.write(JSON.stringify({ type: "done", tokens_used: 1 }) + "\n");
    await done;

    const countAtTerminal = events.length;
    // A stray line after the stream finalized must NOT reach onEvent (it's an
    // orphan — no inflight request).
    fake.stdout.write(JSON.stringify({ type: "text", content: "late" }) + "\n");
    await flush();
    expect(events).toHaveLength(countAtTerminal);
    expect(events.map((e) => e.type)).toEqual(["done"]);
  });

  it("keeps single-shot runAgent working alongside streaming (back-compat)", async () => {
    const fake = makeFakeChild();
    spawnMock.mockReturnValue(fake);
    const s = new Sidecar({ pythonPath: "python", sidecarPath: "/x.py" });
    s.start();

    const pending = s.runAgent("hello");
    await flush();
    fake.stdout.write(JSON.stringify({ response: "hi", tokens_used: 3 }) + "\n");

    const result = await pending;
    expect(result.response).toBe("hi");
    expect(result.tokens_used).toBe(3);
  });
});
