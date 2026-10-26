import { randomBytes } from "node:crypto";

import { compose } from "./stack";
import { newSecret } from "./totp";

/**
 * Starts the e2e stack from scratch and creates its one user, `e2e`, with a random
 * password and TOTP secret. Environment variables set here reach the tests.
 */
export default function globalSetup(): void {
  compose(["down", "-v", "--remove-orphans"]); // whatever a failed run left behind
  // --wait: until the API and the dashboard report healthy (their dependencies start first).
  compose(["up", "-d", "--build", "--wait", "api", "frontend"]);

  const password = randomBytes(18).toString("base64url");
  const secret = newSecret();
  compose(
    [
      "exec",
      "-T",
      "api",
      "python",
      "-m",
      "backend.users",
      "create",
      "e2e",
      "--password-stdin",
      "--totp-secret",
      secret,
    ],
    `${password}\n`,
  );
  process.env.E2E_PASSWORD = password;
  process.env.E2E_TOTP_SECRET = secret;
}
