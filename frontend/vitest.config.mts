import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  test: {
    environment: "jsdom",
    // Expose the hook globals so React Testing Library registers its own
    // `afterEach(cleanup)` when a test file imports it. Under Vitest 5 a
    // top-level hook in the setup file fails with "failed to find the current
    // suite", so the setup file cannot register it.
    // Tests still import `describe`, `it` and `expect` from "vitest" explicitly.
    globals: true,
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["./vitest.setup.ts"],
  },
});
