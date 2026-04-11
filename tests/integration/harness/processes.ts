import { ChildProcess, spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// Stub harness: boots the harmonograf server (Python, via uv) and the
// frontend Vite dev server, waits for both to accept connections, and
// returns a teardown function. The actual implementation lands when
// task #11 is unblocked — for now this is a scaffold with the intended
// shape so spec authors can pattern-match against it.

export interface BootedStack {
  serverPort: number;
  frontendPort: number;
  dataDir: string;
  stop: () => Promise<void>;
}

export interface BootOptions {
  serverPort?: number;
  frontendPort?: number;
  env?: Record<string, string>;
}

export async function bootStack(_opts: BootOptions = {}): Promise<BootedStack> {
  // TODO(#11): spawn `uv run python -m harmonograf_server ...` and
  // `pnpm --prefix ../../frontend dev --port <port>`, waitOn both,
  // return a stop() that kills both and rms the temp data dir.
  throw new Error("bootStack not implemented — pending task #11 unblock");
}

export function reserveDataDir(): string {
  return mkdtempSync(join(tmpdir(), "harmonograf-e2e-"));
}

export function killTree(child: ChildProcess): Promise<void> {
  return new Promise((resolve) => {
    if (!child.pid || child.exitCode !== null) return resolve();
    child.once("exit", () => resolve());
    try {
      process.kill(-child.pid, "SIGTERM");
    } catch {
      child.kill("SIGTERM");
    }
  });
}

export function cleanupDataDir(dir: string): void {
  try {
    rmSync(dir, { recursive: true, force: true });
  } catch {
    // best effort
  }
}

// Re-exported for spec use so fixtures can be typed without
// re-importing node's child_process.
export type { ChildProcess };
export { spawn };
