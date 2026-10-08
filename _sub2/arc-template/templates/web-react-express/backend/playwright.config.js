const { defineConfig } = require('@playwright/test');

// Resolve the runtime base URL for E2E tests.
// Priority: explicit Playwright vars > harness-injected ARC_WEB_BASE_URL >
// the shard/compile port (PORT or ARC_WEB_PORT) > evaluation default 3000.
// NOTE: never hard-code a port here. During compilation shards run the app on
// a derived port (e.g. 43019); during official grading the app runs on 3000.
// All of these are supplied as env at runtime, so this file stays port-agnostic
// and safe to ship in the artifact.
const port =
  process.env.PORT ||
  process.env.ARC_WEB_PORT ||
  process.env.ARC_WEB_BASE_URL_PORT ||
  3000;
const baseURL =
  process.env.PLAYWRIGHT_BASE_URL ||
  process.env.ARC_WEB_BASE_URL ||
  `http://127.0.0.1:${port}`;

module.exports = defineConfig({
  testDir: './test-e2e',
  testMatch: /.*\.(js|jsx|ts|tsx)$/,
  timeout: 30000,
  use: {
    baseURL,
    trace: 'retain-on-failure',
  },
});
