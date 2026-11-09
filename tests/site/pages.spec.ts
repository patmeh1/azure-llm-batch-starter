import { expect, test } from '@playwright/test';
import { readFileSync } from 'node:fs';

const v3 = JSON.parse(readFileSync('docs/v3-results.json', 'utf8'));

test('renders measured comparisons and truthful v3 status without external requests', async ({ page }) => {
  const errors: string[] = [];
  const externalRequests: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {
    if (!request.url().startsWith('http://127.0.0.1:8771/') && !request.url().startsWith('data:')) {
      externalRequests.push(request.url());
    }
  });
  await page.goto('/?scoutTheme=light');
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('More workers.Not more capacity.');
  await expect(page.locator('#v1 table').first()).toContainText('90.34 s');
  await expect(page.locator('#v2')).toContainText('All three acceptance gates failed.');
  await expect(page.locator('#v2 tbody tr')).toHaveCount(3);
  await expect(page.locator('#v3')).toHaveAttribute('data-results-final', String(v3.results_final));
  if (!v3.results_final) {
    await expect(page.locator('#v3-notice')).toContainText('Final comparison results are not available.');
    await expect(page.locator('#v3 tbody tr').first()).toContainText('Not final');
  }
  await expect(page.locator('#v3 time')).toHaveAttribute('datetime', v3.observed_at);
  expect(errors).toEqual([]);
  expect(externalRequests).toEqual([]);
});

test('switches themes and keeps dark secondary text readable', async ({ page }, testInfo) => {
  await page.goto('/?scoutTheme=light');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await page.getByRole('button', { name: 'Switch to dark theme' }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  const secondary = page.locator('#v2 .evidence-context');
  await expect(secondary).toHaveCSS('color', 'rgb(176, 176, 176)');
  await page.getByRole('link', { name: 'Explore v2 →', exact: true }).click();
  await expect(page).toHaveURL(/#v2$/);
  await page.screenshot({ path: testInfo.outputPath('site-dark.png') });
});

test('mobile layout has no document overflow and comparisons remain reachable', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/?scoutTheme=light');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('site-mobile.png') });
  await page.getByRole('link', { name: 'Inspect v3 status →', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'One quota. Five client groups.' })).toBeVisible();
  const region = page.getByRole('region', { name: 'V3 sequential and client-group status' });
  await expect(region).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('downloads the exact public evidence and packaged prompts', async ({ page, request }) => {
  await page.goto('/');
  const paths = await page.locator('a[href^="data/"], a[href^="prompts/"]').evaluateAll(
    links => [...new Set(links.map(link => link.getAttribute('href')!))],
  );
  expect(paths.length).toBeGreaterThanOrEqual(8);
  for (const path of paths) {
    const response = await request.get(path);
    expect(response.ok(), path).toBe(true);
  }
  const snapshot = await (await request.get('data/v3-results.json')).json();
  expect(snapshot).toEqual(v3);
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('link', { name: 'Download v2 evidence JSON' }).click();
  expect((await downloadPromise).suggestedFilename()).toBe('v2-results.json');
});

test('copies commands and reports clipboard denial explicitly', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: async (value: string) => {
        document.documentElement.setAttribute('data-copied-text', value);
      } },
    });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Copy commands' }).click();
  await expect(page.getByRole('status')).toHaveText('Commands copied.');
  await expect(page.locator('html')).toHaveAttribute('data-copied-text', /llm-batch dashboard --demo/);
  await page.evaluate(() => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: async () => { throw new Error('Denied'); } },
    });
  });
  await page.getByRole('button', { name: 'Copy commands' }).click();
  await expect(page.getByRole('status')).toContainText('Clipboard access is unavailable.');
});

test('retains the evidence and navigation when JavaScript is disabled', async ({ browser }) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  const page = await context.newPage();
  await page.goto('http://127.0.0.1:8771/');
  await expect(page.getByRole('heading', { name: 'Find the comparison you need.' })).toBeVisible();
  await expect(page.locator('#v2')).toContainText('516.43 s');
  await expect(page.locator('#v3')).toHaveAttribute('data-results-final', String(v3.results_final));
  await expect(page.locator('#theme-toggle')).toBeHidden();
  await expect(page.getByRole('link', { name: 'Full v2 report ↗', exact: true })).toBeVisible();
  await context.close();
});
