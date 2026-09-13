import type { NextConfig } from "next";

const NON_RELEASE_BUILD_ID = "cortex-web-nonrelease";
const RELEASE_BUILD_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{7,63}$/;

function resolveBuildId(): string {
  const releaseMode = process.env.CORTEX_WEB_RELEASE_BUILD;
  if (releaseMode !== undefined && releaseMode !== "0" && releaseMode !== "1") {
    throw new Error("CORTEX_WEB_RELEASE_BUILD must be 0 or 1");
  }
  if (releaseMode !== "1") return NON_RELEASE_BUILD_ID;
  const buildId = process.env.CORTEX_WEB_BUILD_ID;
  if (!buildId || !RELEASE_BUILD_ID.test(buildId)) {
    throw new Error(
      "CORTEX_WEB_BUILD_ID is required for release builds and must be a safe 8-64 character identifier",
    );
  }
  return buildId;
}

const nextConfig: NextConfig = {
  allowedDevOrigins: ["127.0.0.1"],
  devIndicators: false,
  generateBuildId: async () => resolveBuildId(),
};

export default nextConfig;
