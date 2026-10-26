import { compose } from "./stack";

/** Deletes the e2e stack and its volumes. E2E_KEEP=1 leaves it up to look around. */
export default function globalTeardown(): void {
  if (process.env.E2E_KEEP === "1") return;
  compose(["down", "-v", "--remove-orphans"]);
}
