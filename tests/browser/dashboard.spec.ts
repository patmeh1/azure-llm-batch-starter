import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'LLM Batch Operations', exact: true })).toBeVisible();
  await expect(page.getByText('Elapsed seconds', { exact: true })).toBeVisible();
  await expect(page.getByText(/SIMULATED demo rows/)).toBeVisible();
});

test('shows run durations and exports the selected history', async ({ page }, testInfo) => {
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Download filtered run CSV' }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe('run-history.csv');
  await expect(page.getByRole('heading', { name: 'Batch partitions', exact: true })).toBeVisible();
  await page.getByRole('combobox', { name: 'Inspect a run', exact: true }).click();
  await page.getByRole('option', { name: 'simulated-2', exact: true }).click();
  await expect(page.getByText('420.00', { exact: true })).toBeVisible();
  await page.setViewportSize({ width: 1440, height: 1700 });
  await page.getByRole('heading', { name: 'LLM Batch Operations', exact: true }).scrollIntoViewIfNeeded();
  await expect(page.getByText(/SIMULATED demo rows/)).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('dashboard-synthetic.png'), fullPage: true });
});

test('drills into failures and exposes test timings', async ({ page }) => {
  const selector = page.getByRole('combobox', { name: 'Inspect a run', exact: true });
  await selector.click();
  await selector.press('ArrowDown');
  await page.getByRole('option', { name: 'simulated-3', exact: true }).click();
  await expect(page.getByText('610.00', { exact: true })).toBeVisible();
  await page.getByRole('tab', { name: 'Errors & retries', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Final failed records', exact: true })).toBeVisible();
  const throttles = page.getByTestId('stMetric').filter({ hasText: 'HTTP 429 attempts' });
  await expect(throttles.getByTestId('stMetricValue')).toHaveText('2');
  await page.getByRole('tab', { name: 'Test timings', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Test execution history', exact: true })).toBeVisible();
});
