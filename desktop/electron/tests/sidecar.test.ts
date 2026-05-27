/**
 * Tests for the Sidecar wrapper class (electron/sidecar.ts).
 *
 * The wrapper owns the Python child process that speaks the JSON-line
 * protocol from `desktop/runtime/sidecar.py::serve_json_protocol`. We
 * mock `child_process.spawn` so these tests run without Electron, without
 * Python, and without the Hermes Agent import surface — same isolation
 * the Python-side tests at desktop/tests/test_sidecar.py achieve with
 * the _StubAIAgent.
 *
 * Plan gates covered: #3 (spawn + kill), #4 (IPC contract), #6 (crash
 * propagation).
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";

// ---------------------------------------------------------------------------
// Mock child_process.spawn so no real Python ever starts.
// ---------------------------------------------------------------------------

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

// Import AFTER the mock is registered. Vitest hoists vi.mock calls, but
// we still defer the import below until each test sets up the spawn mock.
import { Sidecar } from "../sidecar";

beforeEach(() => {
  spawnMock.mockReset();
});

describe("Sidecar.start()", () => {
  it("spawns python with --serve and -u in unbuffered mode", () => {
    const fake = makeFakeChild();
    spawnMock.mockReturnValue(fake);

    const s = new Sidecar({
      pythonPath: "/usr/bin/python3",
      sidecarPath: "/abs/runtime/sidecar.py",
    });
    s.start();

    expect(spawnMock).toHaveBeenCalledTimes(1);
    const [cmd, args] = spawnMock.mock.calls[0];
    expect(cmd).toBe("/usr/bin/python3");
    expect(args).toEqual(expect.arrayContaining(["-u", "/abs/runtime/sidecar.py", "--serve"]));
    // -u must precede the script path so it applies to the script — the
    // Python -u flag is positional with respect to script args.
    expect((args as string[]).indexOf("-u")).toBeLessThan((args as string[]).indexOf("/abs/runtime/sidecar.py"));
  });

  it("forwards extra env vars to the child process", () => {
    const fake = makeFakeChild();
    spawnMock.mockReturnValue(fake);

    const s = new Sidecar({
      pythonPath: "python",
      sidecarPath: "/p/sidecar.py",
      env: { SUITE_STUDIO_ORG: "acme", ANTHROPIC_API_KEY: "sk-xyz" },
    });
    s.start();

    const [, , opts] = spawnMock.mock.calls[0] as [string, string[], { env: Record<string, string> }];
    expect(opts.env.SUITE_STUDIO_ORG).toBe("acme");
    expect(opts.env.ANTHROPIC_API_KEY).toBe("sk-xyz");
  });

  it("packaged-mode (empty sidecarPath) spawns the PyInstaller binary directly with only --serve", () => {
    // Per /goal #5 gate #11: in packaged builds the Python sidecar is a
    // PyInstaller-bundled binary — there's no separate .py script. The
    // wrapper must spawn `<bundle>/sidecar --serve` with no -u flag
    // (PyInstaller binaries are not python; -u is a python flag).
    const fake = makeFakeChild();
    spawnMock.mockReturnValue(fake);

    const s = new Sidecar({
      pythonPath: "/abs/Resources/sidecar/sidecar",
      sidecarPath: "",  // empty = packaged-mode signal
    });
    s.start();

    const [cmd, args] = spawnMock.mock.calls[0] as [string, string[]];
    expect(cmd).toBe("/abs/Resources/sidecar/sidecar");
    expect(args).toEqual(["--serve"]);
    expect(args).not.toContain("-u");  // -u is invalid for PyInstaller binaries
  });
});

describe("Sidecar.runAgent()", () => {
  it("writes a {action,query} JSON line to stdin and resolves with the response", async () => {
    const fake = makeFakeChild();
    spawnMock.mockReturnValue(fake);
    const s = new Sidecar({ pythonPath: "python", sidecarPath: "/x.py" });
    s.start();

    // Capture what the wrapper writes into the child's stdin
    const writes: string[] = [];
    fake.stdin.on("data", (chunk: Buffer) => writes.push(chunk.toString()));

    const pending = s.runAgent("say hello");

    // Allow the wrapper's write to flush
    await new Promise((r) => setImmediate(r));

    expect(writes.join("")).toContain(JSON.stringify({ action: "run", query: "say hello" }));
    expect(writes.join("")).toMatch(/\n$/);

    // Simulate the Python sidecar emitting a JSON-line response
    fake.stdout.write(JSON.stringify({ response: "hi there" }) + "\n");

    const result = await pending;
    expect(result.response).toBe("hi there");
    expect(result.error).toBeUndefined();
  });

  it("serializes concurrent calls — second runAgent waits for first response", async () => {
    const fake = makeFakeChild();
    spawnMock.mockReturnValue(fake);
    const s = new Sidecar({ pythonPath: "python", sidecarPath: "/x.py" });
    s.start();

    const writes: string[] = [];
    fake.stdin.on("data", (chunk: Buffer) => writes.push(chunk.toString()));

    const p1 = s.runAgent("first");
    const p2 = s.runAgent("second");

    await new Promise((r) => setImmediate(r));

    // Only the first request must have been written so far — the second is queued
    const writeCountAfterFirst = writes.length;
    expect(writes.join("")).toContain('"query":"first"');
    expect(writes.join("")).not.toContain('"query":"second"');

    // Sidecar responds to the first
    fake.stdout.write(JSON.stringify({ response: "1" }) + "\n");
    expect(await p1).toEqual({ response: "1" });

    await new Promise((r) => setImmediate(r));
    // NOW the second request should have been written
    expect(writes.length).toBeGreaterThan(writeCountAfterFirst);
    expect(writes.join("")).toContain('"query":"second"');

    fake.stdout.write(JSON.stringify({ response: "2" }) + "\n");
    expect(await p2).toEqual({ response: "2" });
  });

  it("propagates {error:...} responses from the sidecar to the caller", async () => {
    const fake = makeFakeChild();
    spawnMock.mockReturnValue(fake);
    const s = new Sidecar({ pythonPath: "python", sidecarPath: "/x.py" });
    s.start();

    const pending = s.runAgent("triggers error");
    await new Promise((r) => setImmediate(r));

    fake.stdout.write(JSON.stringify({ error: "upstream blew up" }) + "\n");

    const result = await pending;
    expect(result.error).toBe("upstream blew up");
    expect(result.response).toBeUndefined();
  });

  it("rejects pending runAgent when the child exits before responding", async () => {
    const fake = makeFakeChild();
    spawnMock.mockReturnValue(fake);
    const s = new Sidecar({ pythonPath: "python", sidecarPath: "/x.py" });
    s.start();

    const pending = s.runAgent("orphan");
    await new Promise((r) => setImmediate(r));

    // Sidecar crashes / exits before sending a response
    fake.emit("exit", 1, null);

    await expect(pending).rejects.toThrow(/sidecar.*exit|exit.*sidecar|crashed|terminated/i);
  });
});

describe("Sidecar.kill()", () => {
  it("sends SIGTERM to the child process", () => {
    const fake = makeFakeChild();
    spawnMock.mockReturnValue(fake);
    const s = new Sidecar({ pythonPath: "python", sidecarPath: "/x.py" });
    s.start();

    s.kill();

    expect(fake.kill).toHaveBeenCalledTimes(1);
  });

  it("is a no-op if start() was never called", () => {
    const s = new Sidecar({ pythonPath: "python", sidecarPath: "/x.py" });
    expect(() => s.kill()).not.toThrow();
  });
});

describe("Sidecar.onCrash()", () => {
  it("fires the callback when the child exits with a non-zero code", () => {
    const fake = makeFakeChild();
    spawnMock.mockReturnValue(fake);
    const s = new Sidecar({ pythonPath: "python", sidecarPath: "/x.py" });
    s.start();

    const crashCb = vi.fn();
    s.onCrash(crashCb);

    fake.emit("exit", 137, "SIGKILL");

    expect(crashCb).toHaveBeenCalledTimes(1);
    expect(crashCb).toHaveBeenCalledWith({ code: 137, signal: "SIGKILL" });
  });

  it("does NOT fire when kill() initiated a graceful shutdown (exit 0 / null code)", () => {
    const fake = makeFakeChild();
    spawnMock.mockReturnValue(fake);
    const s = new Sidecar({ pythonPath: "python", sidecarPath: "/x.py" });
    s.start();

    const crashCb = vi.fn();
    s.onCrash(crashCb);

    s.kill();
    fake.emit("exit", 0, null);

    expect(crashCb).not.toHaveBeenCalled();
  });
});
