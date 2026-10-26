/**
 * The throwaway stack the end-to-end tests run against (D44): a separate Docker Compose
 * project with its own empty volumes and its own ports, so it never touches the real
 * database or user, and can run beside the dev stack. Its engine runs one paper bot,
 * for the kill switch test (D45), on live MEXC prices: it needs the internet.
 */
import { execFileSync } from "node:child_process";
import path from "node:path";

export const PROJECT = "crypto-trading-e2e";
export const API_PORT = 8100;
export const FRONTEND_PORT = 3100;

const REPO_ROOT = path.resolve(__dirname, "..", "..");

function stackEnv(): NodeJS.ProcessEnv {
  return { ...process.env, API_PORT: String(API_PORT), FRONTEND_PORT: String(FRONTEND_PORT) };
}

/** Runs SQL in the e2e stack's database; returns psql's unaligned, tuples-only output. */
export function psql(sql: string): string {
  return execFileSync(
    "docker",
    [
      "compose",
      "-p",
      PROJECT,
      "exec",
      "-T",
      "db",
      "sh",
      "-c",
      'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA',
    ],
    { cwd: REPO_ROOT, env: stackEnv(), input: sql, encoding: "utf8" },
  ).trim();
}

/** `docker compose -p crypto-trading-e2e <args>` from the repo root, its output shown. */
export function compose(args: string[], input?: string): void {
  execFileSync("docker", ["compose", "-p", PROJECT, ...args], {
    cwd: REPO_ROOT,
    env: stackEnv(),
    input,
    stdio: [input === undefined ? "ignore" : "pipe", "inherit", "inherit"],
  });
}
