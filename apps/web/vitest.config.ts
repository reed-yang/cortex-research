import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

// Mirrors the `@/*` -> `./*` path mapping in tsconfig.json so tests can import
// shell components the same way the application does.
const projectRoot = fileURLToPath(new URL(".", import.meta.url));

export default defineConfig({
  resolve: {
    alias: { "@/": projectRoot },
  },
  test: {
    environment: "jsdom",
    restoreMocks: true,
    setupFiles: ["./tests/jsdom-setup.ts"],
  },
});
