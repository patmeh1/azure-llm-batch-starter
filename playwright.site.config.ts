import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/site',
  workers: 1,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: 'http://127.0.0.1:8771',
    headless: true,
    channel: process.env.BROWSER_CHANNEL || undefined,
    viewport: { width: 1440, height: 1100 },
    reducedMotion: 'reduce',
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  reporter: [
    ['list'],
    ['json', { outputFile: 'reports/site-results.json' }],
    ['junit', { outputFile: 'reports/site.xml' }],
  ],
  outputDir: 'reports/site-artifacts',
  webServer: {
    command: 'python -m http.server 8771 --bind 127.0.0.1 --directory _site',
    url: 'http://127.0.0.1:8771/',
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
