import type { NextConfig } from "next";

// Where /api/* goes: the API service in Docker (set at build time, see the Dockerfile),
// or a local `uvicorn` when running `pnpm dev`.
const apiUrl = process.env.API_URL ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  // Bundle only the files the server needs into .next/standalone, for a small Docker image.
  output: "standalone",
  // The browser calls the API on the dashboard's own origin, so the session cookie is
  // sent with no CORS (D44). /api/auth/me becomes ${apiUrl}/auth/me.
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiUrl}/:path*` }];
  },
};

export default nextConfig;
