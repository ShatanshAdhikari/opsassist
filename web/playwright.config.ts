import { defineConfig } from "@playwright/test";

// Requires the API, worker and `npm run dev` to be running against a disposable database.
export default defineConfig({
  testDir: "./e2e",
  timeout: 240_000,
  use: { baseURL: "http://localhost:5173", screenshot: "only-on-failure" },
});
