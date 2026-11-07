import { defineConfig } from '@playwright/test';
import { join } from 'node:path';

export default defineConfig({
  testDir: './tests/browser',
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 30_000 },
  use: {
    baseURL: 'http://127.0.0.1:8769',
    headless: true,
    channel: process.env.BROWSER_CHANNEL || undefined,
    viewport: { width: 1440, height: 1000 },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  reporter: [
    ['list'],
    ['json', { outputFile: 'reports/browser-results.json' }],
    ['junit', { outputFile: 'reports/browser.xml' }],
  ],
  outputDir: 'reports/browser-artifacts',
  webServer: {
    command: `llm-batch dashboard --demo --state "${join('outputs', 'browser-demo.sqlite3')}" --port 8769`,
    url: 'http://127.0.0.1:8769/_stcore/health',
    reuseExistingServer: false,
    timeout: 60_000,
    env: { LLM_DASHBOARD_CONFIG: '' },
  },
});
