import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Bundle only the files the server needs into .next/standalone, for a small Docker image.
  output: "standalone",
};

export default nextConfig;
