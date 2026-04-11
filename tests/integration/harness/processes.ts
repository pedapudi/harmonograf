import { ChildProcess, spawn } from "node:child_process";
import { createServer, connect as netConnect } from "node:net";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";

// Boots the full harmonograf stack for a Playwright spec:
//   1. harmonograf-server via `uv run python -m harmonograf_server`
//      on ephemeral --port and --web-port, memory store, temp data dir
//   2. the Vite dev server via `pnpm dev --port <free> --strictPort`
//      with VITE_HARMONOGRAF_API pointed at the server's web-port
// Both children are spawned in their own process group so stop()
// SIGTERMs the whole tree — Vite spawns esbuild workers that otherwise
// leak.

const HARNESS_DIR = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HARNESS_DIR, "..", "..", "..");
const SERVER_DIR = join(REPO_ROOT, "server");
const FRONTEND_DIR = join(REPO_ROOT, "frontend");

export interface BootedStack {
  serverGrpcPort: number;
  serverWebPort: number;
  frontendPort: number;
  frontendUrl: string;
  dataDir: string;
  server: ChildProcess;
  frontend: ChildProcess;
  stop: () => Promise<void>;
}

export interface BootOptions {
  serverBootTimeoutMs?: number;
  frontendBootTimeoutMs?: number;
}

export async function bootStack(opts: BootOptions = {}): Promise<BootedStack> {
  const serverBootTimeoutMs = opts.serverBootTimeoutMs ?? 30_000;
  const frontendBootTimeoutMs = opts.frontendBootTimeoutMs ?? 60_000;

  const serverGrpcPort = await reservePort();
  const serverWebPort = await reservePort();
  const frontendPort = await reservePort();
  const dataDir = reserveDataDir();

  const server = spawn(
    "uv",
    [
      "run",
      "python",
      "-m",
      "harmonograf_server",
      "--host",
      "127.0.0.1",
      "--port",
      String(serverGrpcPort),
      "--web-port",
      String(serverWebPort),
      "--store",
      "memory",
      "--data-dir",
      dataDir,
      "--log-level",
      "WARNING",
      "--grace",
      "0.5",
    ],
    {
      cwd: SERVER_DIR,
      stdio: ["ignore", "pipe", "pipe"],
      detached: true,
      env: { ...process.env },
    },
  );

  attachLog(server, "[server]");

  try {
    await waitForTcp("127.0.0.1", serverWebPort, serverBootTimeoutMs, server);
  } catch (err) {
    await killTree(server);
    cleanupDataDir(dataDir);
    throw err;
  }

  const frontend = spawn(
    "pnpm",
    [
      "dev",
      "--host",
      "127.0.0.1",
      "--port",
      String(frontendPort),
      "--strictPort",
    ],
    {
      cwd: FRONTEND_DIR,
      stdio: ["ignore", "pipe", "pipe"],
      detached: true,
      env: {
        ...process.env,
        VITE_HARMONOGRAF_API: `http://127.0.0.1:${serverWebPort}`,
        BROWSER: "none",
      },
    },
  );

  attachLog(frontend, "[vite]");

  try {
    await waitForTcp("127.0.0.1", frontendPort, frontendBootTimeoutMs, frontend);
  } catch (err) {
    await killTree(frontend);
    await killTree(server);
    cleanupDataDir(dataDir);
    throw err;
  }

  const frontendUrl = `http://127.0.0.1:${frontendPort}`;

  let stopped = false;
  const stop = async (): Promise<void> => {
    if (stopped) return;
    stopped = true;
    await Promise.allSettled([killTree(frontend), killTree(server)]);
    cleanupDataDir(dataDir);
  };

  return {
    serverGrpcPort,
    serverWebPort,
    frontendPort,
    frontendUrl,
    dataDir,
    server,
    frontend,
    stop,
  };
}

async function reservePort(): Promise<number> {
  return new Promise((resolvePort, rejectPort) => {
    const srv = createServer();
    srv.unref();
    srv.on("error", rejectPort);
    srv.listen(0, "127.0.0.1", () => {
      const addr = srv.address();
      if (addr && typeof addr === "object") {
        const { port } = addr;
        srv.close(() => resolvePort(port));
      } else {
        srv.close(() => rejectPort(new Error("could not reserve port")));
      }
    });
  });
}

async function waitForTcp(
  host: string,
  port: number,
  timeoutMs: number,
  child: ChildProcess,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  // eslint-disable-next-line no-constant-condition
  while (true) {
    if (child.exitCode !== null) {
      throw new Error(
        `child exited before ${host}:${port} accepted connections (code=${child.exitCode})`,
      );
    }
    const ok = await tryConnect(host, port);
    if (ok) return;
    if (Date.now() > deadline) {
      throw new Error(`timed out waiting for ${host}:${port} after ${timeoutMs}ms`);
    }
    await delay(200);
  }
}

function tryConnect(host: string, port: number): Promise<boolean> {
  return new Promise((resolveOk) => {
    const sock = netConnect({ host, port });
    let done = false;
    const finish = (ok: boolean) => {
      if (done) return;
      done = true;
      sock.destroy();
      resolveOk(ok);
    };
    sock.once("connect", () => finish(true));
    sock.once("error", () => finish(false));
    sock.setTimeout(1000, () => finish(false));
  });
}

function attachLog(child: ChildProcess, prefix: string): void {
  const onLine = (buf: Buffer) => {
    if (!process.env.HARMONOGRAF_E2E_VERBOSE) return;
    const text = buf.toString("utf8").trimEnd();
    if (text) console.error(`${prefix} ${text}`);
  };
  child.stdout?.on("data", onLine);
  child.stderr?.on("data", onLine);
}

export function reserveDataDir(): string {
  return mkdtempSync(join(tmpdir(), "harmonograf-e2e-"));
}

export function killTree(child: ChildProcess): Promise<void> {
  return new Promise((resolveStop) => {
    if (!child.pid || child.exitCode !== null) return resolveStop();
    const done = () => resolveStop();
    child.once("exit", done);
    try {
      process.kill(-child.pid, "SIGTERM");
    } catch {
      try {
        child.kill("SIGTERM");
      } catch {
        return resolveStop();
      }
    }
    setTimeout(() => {
      if (child.exitCode === null) {
        try {
          process.kill(-child.pid!, "SIGKILL");
        } catch {
          try {
            child.kill("SIGKILL");
          } catch {
            // fall through
          }
        }
      }
    }, 3000).unref();
  });
}

export function cleanupDataDir(dir: string): void {
  try {
    rmSync(dir, { recursive: true, force: true });
  } catch {
    // best effort
  }
}

// ---------------------------------------------------------------------------
// Synthetic client helper
// ---------------------------------------------------------------------------
// Spawns tests/integration/harness/synth_client.py under `uv run python`,
// which connects to the running server and emits one session worth of
// spans (INVOCATION + LLM_CALL + TOOL_CALL) then exits. The golden-path
// spec uses this to populate the UI with deterministic content before
// it starts asserting against the rendered Gantt.

export interface SyntheticAgentOptions {
  serverGrpcPort: number;
  sessionTitle?: string;
  timeoutMs?: number;
}

export async function startSyntheticAgent(
  opts: SyntheticAgentOptions,
): Promise<void> {
  const sessionTitle = opts.sessionTitle ?? "smoke-golden-path";
  const timeoutMs = opts.timeoutMs ?? 30_000;
  const script = join(HARNESS_DIR, "synth_client.py");

  const child = spawn(
    "uv",
    [
      "run",
      "python",
      script,
      "--server-addr",
      `127.0.0.1:${opts.serverGrpcPort}`,
      "--session-title",
      sessionTitle,
    ],
    {
      cwd: REPO_ROOT,
      stdio: ["ignore", "pipe", "pipe"],
      env: { ...process.env },
    },
  );
  attachLog(child, "[synth]");

  await new Promise<void>((resolveDone, rejectDone) => {
    const timer = setTimeout(() => {
      try {
        child.kill("SIGKILL");
      } catch {
        // ignore
      }
      rejectDone(new Error(`synthetic client timed out after ${timeoutMs}ms`));
    }, timeoutMs);
    child.once("exit", (code) => {
      clearTimeout(timer);
      if (code === 0) resolveDone();
      else rejectDone(new Error(`synthetic client exited with code ${code}`));
    });
    child.once("error", (err) => {
      clearTimeout(timer);
      rejectDone(err);
    });
  });
}

export type { ChildProcess };
export { spawn };
