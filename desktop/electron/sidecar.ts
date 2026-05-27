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
}

interface PendingRequest {
  resolve: (result: AgentResult) => void;
  reject: (err: Error) => void;
  query: string;
}

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
      const req: PendingRequest = { resolve, reject, query };
      this.queue.push(req);
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
    const payload = JSON.stringify({ action: "run", query: next.query }) + "\n";
    this.child.stdin?.write(payload);
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
    this.inflight = null;
    if (!req) {
      process.stderr.write(`[sidecar] orphan response (no inflight request): ${line}\n`);
      return;
    }
    try {
      const parsed = JSON.parse(line) as AgentResult;
      req.resolve(parsed);
    } catch (err) {
      req.resolve({ error: `malformed sidecar response: ${(err as Error).message}` });
    }
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
