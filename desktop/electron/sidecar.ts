/**
 * Wrapper around the Python sidecar child process.
 *
 * Spawns ``python -u runtime/sidecar.py --serve`` and speaks the
 * newline-delimited JSON protocol defined in
 * ``desktop/runtime/sidecar.py::serve_json_protocol``. One process per
 * Electron app; sequential request/response over stdin/stdout.
 *
 * Why this file exists separate from ``main.ts``: the child-process
 * plumbing is the only part of the Electron shell with non-trivial
 * logic (framing, queueing, crash propagation), and it's the only part
 * worth unit-testing without booting Electron. Keeping it factored out
 * makes ``main.ts`` a thin lifecycle wrapper that can be tested by
 * mocking this module — see ``tests/sidecar.test.ts`` and
 * ``tests/main.test.ts``.
 */
import { spawn, type ChildProcess } from "node:child_process";

export interface SidecarOptions {
  /** Absolute path to the Python interpreter (PyInstaller bundle in
   *  packaged builds; `desktop/.venv/bin/python` in dev). */
  pythonPath: string;
  /** Absolute path to `runtime/sidecar.py`. Resolved by the caller from
   *  `__dirname` (dev) or `process.resourcesPath` (packaged). */
  sidecarPath: string;
  /** Working directory for the child. Defaults to the directory holding
   *  `sidecarPath` if omitted. */
  cwd?: string;
  /** Environment overrides merged on top of `process.env`. The Anthropic
   *  API key is expected here for live runs; never hard-code it. */
  env?: Record<string, string>;
}

export interface AgentResult {
  response?: string;
  error?: string;
  /**
   * Per-turn sum of input + output tokens from the Python sidecar's JSON
   * response (gate #2 — see `desktop/runtime/sidecar.py::serve_json_protocol`
   * and `_extract_tokens_used`). Always present alongside `response` on the
   * success path; absent on error responses. Renderer surfaces this for
   * cost/budget telemetry; the breakdown (input vs output, cache reads,
   * etc.) stays inside Hermes Agent's `run_conversation` result dict and
   * is not currently propagated across the JSON-line boundary.
   */
  tokens_used?: number;
}

/**
 * One typed streaming event forwarded from the Python sidecar to the renderer
 * (rich-pipe slice 1). The wrapper stays schema-agnostic — it forwards the
 * parsed JSON (`{type:"text",...}`, `{type:"data_table",...}`, `{type:"done",...}`)
 * verbatim and only synthesizes `{type:"error",...}` for sidecar error / malformed
 * lines, so the renderer's `chat-stream.ts` normalizer validates the shapes.
 */
export type SidecarEvent = Record<string, unknown>;
export type SidecarEventListener = (event: SidecarEvent) => void;

interface SingleRequest {
  mode: "single";
  resolve: (result: AgentResult) => void;
  reject: (err: Error) => void;
  query: string;
}

interface StreamRequest {
  mode: "stream";
  resolve: () => void;
  reject: (err: Error) => void;
  query: string;
  onEvent: SidecarEventListener;
}

type PendingRequest = SingleRequest | StreamRequest;

export type CrashListener = (info: { code: number | null; signal: NodeJS.Signals | string | null }) => void;

export class Sidecar {
  private readonly opts: SidecarOptions;
  private child: ChildProcess | null = null;
  private stdoutBuffer = "";
  private readonly queue: PendingRequest[] = [];
  private inflight: PendingRequest | null = null;
  private gracefulShutdown = false;
  private readonly crashListeners: CrashListener[] = [];

  constructor(opts: SidecarOptions) {
    this.opts = opts;
  }

  start(): void {
    if (this.child) {
      return;
    }
    // Dev: spawn python with -u and the sidecar.py script path.
    // Packaged: pythonPath IS the PyInstaller-bundled binary (which is
    // NOT a python interpreter — it embeds Python), so -u is invalid
    // and there's no separate script to pass. Empty sidecarPath is the
    // signal that we're in packaged-mode.
    const args = this.opts.sidecarPath
      ? ["-u", this.opts.sidecarPath, "--serve"]
      : ["--serve"];
    const env = { ...process.env, ...(this.opts.env ?? {}) } as NodeJS.ProcessEnv;
    this.child = spawn(this.opts.pythonPath, args, {
      cwd: this.opts.cwd,
      env,
      stdio: ["pipe", "pipe", "pipe"],
    });

    this.child.stdout?.setEncoding("utf8");
    this.child.stdout?.on("data", (chunk: string) => this.onStdout(chunk));
    this.child.on("exit", (code, signal) => this.onExit(code, signal));
    // Surface stderr to the Electron main-process log so packaged
    // builds leave a paper trail when something goes wrong. (Renderer
    // crash propagation goes through the explicit onCrash callback.)
    this.child.stderr?.setEncoding("utf8");
    this.child.stderr?.on("data", (chunk: string) => {
      process.stderr.write(`[sidecar] ${chunk}`);
    });
  }

  runAgent(query: string): Promise<AgentResult> {
    return new Promise<AgentResult>((resolve, reject) => {
      this.queue.push({ mode: "single", resolve, reject, query });
      this.pump();
    });
  }

  /**
   * Streaming variant (rich-pipe slice 1). Sends a `stream:true` run request and
   * delivers each typed event (text / data_table / done / error) to `onEvent` in
   * order, resolving when the terminal `done` (or an error) arrives. Malformed or
   * `{error:...}` lines are surfaced to `onEvent` as a `{type:"error"}` event,
   * never silently dropped, then finalize the stream.
   */
  runAgentStream(query: string, onEvent: SidecarEventListener): Promise<void> {
    return new Promise<void>((resolve, reject) => {
      this.queue.push({ mode: "stream", resolve, reject, query, onEvent });
      this.pump();
    });
  }

  kill(signal: NodeJS.Signals = "SIGTERM"): void {
    this.gracefulShutdown = true;
    this.child?.kill(signal);
  }

  onCrash(listener: CrashListener): void {
    this.crashListeners.push(listener);
  }

  private pump(): void {
    if (this.inflight || this.queue.length === 0 || !this.child) {
      return;
    }
    const next = this.queue.shift()!;
    this.inflight = next;
    const request =
      next.mode === "stream"
        ? { action: "run", query: next.query, stream: true }
        : { action: "run", query: next.query };
    this.child.stdin?.write(JSON.stringify(request) + "\n");
  }

  private onStdout(chunk: string): void {
    this.stdoutBuffer += chunk;
    let nl: number;
    while ((nl = this.stdoutBuffer.indexOf("\n")) !== -1) {
      const line = this.stdoutBuffer.slice(0, nl).trim();
      this.stdoutBuffer = this.stdoutBuffer.slice(nl + 1);
      if (!line) continue;
      this.deliver(line);
    }
  }

  private deliver(line: string): void {
    const req = this.inflight;
    if (!req) {
      process.stderr.write(`[sidecar] orphan response (no inflight request): ${line}\n`);
      return;
    }
    if (req.mode === "stream") {
      this.deliverStreamLine(req, line);
      return;
    }
    // Single-shot (back-compat): resolve on the first line.
    this.inflight = null;
    try {
      const parsed = JSON.parse(line) as AgentResult;
      req.resolve(parsed);
    } catch (err) {
      req.resolve({ error: `malformed sidecar response: ${(err as Error).message}` });
    }
    this.pump();
  }

  private deliverStreamLine(req: StreamRequest, line: string): void {
    let event: SidecarEvent;
    try {
      event = JSON.parse(line) as SidecarEvent;
    } catch (err) {
      // Surface, never silently drop (B0 deliver() MINOR). A non-JSON line means
      // chatter leaked onto the protocol stdout — terminal for this stream.
      req.onEvent({ type: "error", error: `malformed sidecar event: ${(err as Error).message}` });
      this.finalizeStream(req);
      return;
    }
    // A sidecar error line ({"error":...}, no type) is terminal — normalize it to
    // a typed error event so the renderer's normalizer recognizes it.
    const errMsg = (event as { error?: unknown }).error;
    if (errMsg) {
      req.onEvent({ type: "error", error: String(errMsg) });
      this.finalizeStream(req);
      return;
    }
    req.onEvent(event);
    if ((event as { type?: unknown }).type === "done") {
      this.finalizeStream(req);
    }
  }

  private finalizeStream(req: StreamRequest): void {
    this.inflight = null;
    req.resolve();
    this.pump();
  }

  private onExit(code: number | null, signal: NodeJS.Signals | string | null): void {
    const drainErr = new Error(
      `sidecar exited (code=${code} signal=${signal ?? "null"}) before responding`,
    );
    if (this.inflight) {
      this.inflight.reject(drainErr);
      this.inflight = null;
    }
    while (this.queue.length > 0) {
      this.queue.shift()!.reject(drainErr);
    }
    if (!this.gracefulShutdown) {
      for (const cb of this.crashListeners) {
        cb({ code, signal });
      }
    }
    this.child = null;
  }
}
