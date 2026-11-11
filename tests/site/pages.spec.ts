import { expect, test, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';

const v3 = JSON.parse(readFileSync('docs/v3-results.json', 'utf8'));
const v4 = JSON.parse(readFileSync('docs/v4-results.json', 'utf8'));
const v5 = JSON.parse(readFileSync('docs/v5-results.json', 'utf8'));
const routes = [
  ['Overview', 'index.html'],
  ['Compare versions', 'comparison.html'],
  ['V1 baseline', 'v1.html'],
  ['V2 complexity', 'v2.html'],
  ['V3 GPT-5 mini', 'v3.html'],
  ['V4 multi-deployment', 'v4.html'],
  ['V5 smart routing', 'v5.html'],
  ['What works', 'approach.html'],
  ['Resources', 'resources.html'],
  ['Methodology', 'methodology.html'],
];

const comparisonLabels = [
  'V1 / BASELINE',
  'V2 / COMPLEXITY',
  'V3 / GROUPED REQUESTS',
  'V4 / MATCHED QUOTA',
  'V5 / SMART ROUTING',
];

async function expectComparisonsToFit(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  for (const table of await page.locator('.comparison-table').all()) {
    const overflow = await table.evaluate(element => {
      const region = element.parentElement!;
      const bounds = region.getBoundingClientRect();
      return {
        region: region.scrollWidth - region.clientWidth,
        table: element.getBoundingClientRect().width - region.clientWidth,
        cells: [...element.querySelectorAll('tbody th, tbody td')]
          .filter(cell => {
            const rect = cell.getBoundingClientRect();
            return rect.left < bounds.left - 1 || rect.right > bounds.right + 1
              || cell.scrollWidth > cell.clientWidth + 1;
          })
          .map(cell => cell.textContent),
      };
    });
    expect(overflow.region).toBeLessThanOrEqual(1);
    expect(overflow.table).toBeLessThanOrEqual(1);
    expect(overflow.cells).toEqual([]);
  }
}

for (const width of [961, 1280, 1440, 1920]) {
  test(`all five versions fit side by side at ${width}px without horizontal scrolling`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1100 });
    for (const route of ['index.html', 'comparison.html']) {
      await page.goto(`/${route}?scoutTheme=light`);
      await expectComparisonsToFit(page);
      for (const table of await page.locator('.comparison-table').all()) {
        const headers = table.getByRole('columnheader');
        await expect(headers).toHaveCount(6);
        const boxes = await headers.evaluateAll(elements => elements.map(element => {
          const box = element.getBoundingClientRect();
          return { left: box.left, right: box.right, top: box.top, width: box.width };
        }));
        for (const [index, box] of boxes.entries()) {
          expect(box.width).toBeGreaterThan(100);
          expect(box.left).toBeGreaterThanOrEqual(0);
          expect(box.right).toBeLessThanOrEqual(width);
          expect(box.top).toBe(boxes[0].top);
          if (index > 0) expect(box.left).toBeGreaterThanOrEqual(boxes[index - 1].right - 1);
        }
        for (const [index, label] of comparisonLabels.entries()) {
          await expect(headers.nth(index + 1)).toContainText(label);
        }
        await expect(table.locator('.comparison-version').first()).toBeHidden();
      }
    }
    await page.locator('#setup table').evaluate(element => element.scrollIntoView({ block: 'start' }));
    await page.screenshot({ path: testInfo.outputPath(`comparison-desktop-${width}.png`) });
  });
}

for (const width of [320, 390, 768, 960]) {
  test(`all comparison values stack with version labels at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 844 });
    for (const route of ['index.html', 'comparison.html']) {
      await page.goto(`/${route}?scoutTheme=light`);
      await expectComparisonsToFit(page);
      for (const table of await page.locator('.comparison-table').all()) {
        const rows = table.locator('tbody tr:not(.group-row)');
        const labels = await rows.evaluateAll(elements => elements.map(row =>
          [...row.querySelectorAll('.comparison-version')].map(label => label.textContent),
        ));
        expect(labels.length).toBeGreaterThan(0);
        for (const rowLabels of labels) expect(rowLabels).toEqual(comparisonLabels);
        const cells = rows.first().getByRole('cell');
        await expect(cells).toHaveCount(5);
        let previousBottom = 0;
        for (const cell of await cells.all()) {
          await expect(cell.locator('.comparison-version')).toBeVisible();
          const box = (await cell.boundingBox())!;
          expect(box.y).toBeGreaterThanOrEqual(previousBottom);
          previousBottom = box.y + box.height;
        }
      }
    }
    await page.locator('#setup tbody tr:not(.group-row)').first().evaluate(element => element.scrollIntoView({ block: 'start' }));
    await page.screenshot({ path: testInfo.outputPath(`comparison-stacked-${width}.png`) });
  });
}

test('every navigation tab opens a real page with an active state and a working refresh', async ({ page }) => {
  const errors: string[] = [];
  const external: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {
    if (!request.url().startsWith('http://127.0.0.1:8771/') && !request.url().startsWith('data:')) {
      external.push(request.url());
    }
  });
  await page.goto('/?scoutTheme=light');
  const titles = new Set<string>();
  for (const [label, file] of routes) {
    const navigation = page.getByRole('navigation', { name: 'Main navigation', exact: true });
    await expect(navigation.getByRole('link')).toHaveCount(routes.length);
    await navigation.getByRole('link', { name: label, exact: true }).click();
    expect(new URL(page.url()).pathname).toBe(`/${file}`);
    expect(new URL(page.url()).hash).toBe('');
    await expect(page.locator('body')).toHaveAttribute('data-page', file);
    await expect(navigation.locator('[aria-current="page"]')).toHaveText(label);
    await expect(page.locator('h1')).toHaveCount(1);
    await expect(page.locator('link[rel="canonical"]')).toHaveAttribute('href', `https://patmeh1.github.io/azure-llm-batch-starter/${file}`);
    titles.add(await page.title());
    await page.reload();
    await expect(navigation.locator('[aria-current="page"]')).toHaveText(label);
  }
  expect(titles.size).toBe(routes.length);
  expect(errors).toEqual([]);
  expect(external).toEqual([]);
});

test('comparison clearly distinguishes GPT builds, workload variables, and measured outcomes', async ({ page }) => {
  await page.goto('/comparison.html');
  const setup = page.getByRole('region', { name: 'Configuration comparison across versions' });
  const identity = setup.getByRole('row').filter({ has: page.getByRole('rowheader', { name: 'Exact GPT model / build', exact: true }) });
  await expect(identity.getByRole('cell').nth(0)).toContainText('GPT-5.4 mini');
  await expect(identity.getByRole('cell').nth(0)).toContainText('2026-03-17');
  await expect(identity.getByRole('cell').nth(1)).toContainText('GPT-5.4 mini');
  await expect(identity.getByRole('cell').nth(2)).toContainText('GPT-5 mini');
  await expect(identity.getByRole('cell').nth(2)).toContainText('2025-08-07');
  await expect(identity.getByRole('cell')).toHaveCount(5);
  await expect(identity.getByRole('cell').nth(3)).toContainText('GPT-5 mini');
  await expect(identity.getByRole('cell').nth(3)).toContainText('2025-08-07');
  await expect(identity.getByRole('cell').nth(4)).toContainText('GPT-5 mini');
  await expect(identity.getByRole('cell').nth(4)).toContainText('2025-08-07');
  await expect(setup.getByRole('row').filter({ has: page.getByRole('rowheader', { name: 'Service allocation', exact: true }) }).getByRole('cell').nth(3)).toContainText('150K TPM / 150 RPM in each arm');
  for (const label of ['Prompt workload', 'Input volume', 'Dataset size', 'Output contract', 'Processing modes', 'Service allocation', 'Client pacing / retries', 'Generation settings', 'Azure SKU / geography', 'Actual Azure Batch API']) {
    await expect(setup.getByRole('rowheader', { name: label, exact: true })).toBeVisible();
  }
  const outcomes = page.getByRole('region', { name: 'Outcome comparison across versions' });
  await expect(outcomes).toContainText('90.34 s');
  await expect(outcomes).toContainText('14.38 s');
  await expect(outcomes).toContainText('Failed in all three modes.');
  await expect(outcomes).toContainText('Simple category label only; not the complex v2/v3 rubric.');
  await expect(outcomes).toContainText('Average successful HTTP call');
  await expect(outcomes).toContainText('Average enqueue-to-record completion');
  if (!v3.results_final) await expect(outcomes).toContainText('No final sequential-versus-grouped duration comparison.');
  await expect(page.getByText('Do not attribute the differences to the GPT model alone.', { exact: false })).toBeVisible();
});

test('version pages preserve original evidence and the incomplete v3 boundary', async ({ page }) => {
  await page.goto('/v1.html');
  await expect(page.locator('#v1 table').first()).toContainText('90.34 s');
  await expect(page.locator('#v2')).toHaveCount(0);
  await page.goto('/v2.html');
  await expect(page.locator('#v2')).toContainText('All three acceptance gates failed.');
  await expect(page.locator('#v2 tbody tr')).toHaveCount(3);
  await page.goto('/v3.html');
  await expect(page.locator('#v3')).toHaveAttribute('data-results-final', String(v3.results_final));
  await expect(page.locator('#v3 time')).toHaveAttribute('datetime', v3.observed_at);
  if (!v3.results_final) {
    await expect(page.locator('#v3-notice')).toContainText('Final comparison results are not available.');
    await expect(page.locator('#v3 tbody tr').first()).toContainText('Not final');
  }
  if (v3.execution_status === 'interrupted') {
    await expect(page.locator('#v3-notice')).toContainText('no active runner was observed');
    await expect(page.locator('#v3-notice')).toContainText('Last recorded activity');
  }
});

test('v4 publishes the actual matched-quota outcomes without hiding failures', async ({ page, request }, testInfo) => {
  await page.goto('/v4.html');
  await expect(page.locator('#v4')).toHaveAttribute('data-results-final', String(v4.results_final));
  await expect(page.locator('#v4-notice time')).toHaveAttribute('datetime', v4.observed_at);
  const outcomes = page.getByRole('region', { name: 'V4 matched-quota outcomes' });
  await expect(outcomes.locator('tbody tr')).toHaveCount(2);
  for (const [index, stage] of v4.stages.entries()) {
    const cells = outcomes.locator('tbody tr').nth(index).getByRole('cell');
    await expect(cells.nth(1)).toHaveText(`${stage.succeeded} / 65`);
    await expect(cells.nth(2)).toHaveText(String(stage.failed));
    await expect(cells.nth(3)).toHaveText(String(stage.http_attempts));
    await expect(cells.nth(4)).toHaveText(String(stage.errors_429));
  }
  await expect(page.getByRole('region', { name: 'V4 deployment observations' }).locator('tbody tr')).toHaveCount(4);
  await expect(page.getByRole('region', { name: 'V4 semantic quality' }).locator('tbody tr')).toHaveCount(2);
  await expect(page.locator('#v4-reproduce')).toContainText('The full 1,700-record scenario was not executed.');
  expect(await (await request.get('data/v4-results.json')).json()).toEqual(v4);
  const samples = await (await request.get('data/v4-output-samples.json')).json();
  expect(samples.arms).toHaveLength(2);
  expect(samples.arms.every((arm: { records: unknown[] }) => arm.records.length === 3)).toBe(true);
  await page.locator('#v4-results-title').scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('v4-results-desktop.png') });
});

test('v5 separates measured averages from job time and preserves pending states', async ({ page, request }, testInfo) => {
  await page.goto('/v5.html');
  await expect(page.locator('#v5')).toHaveAttribute('data-results-final', String(v5.results_final));
  await expect(page.locator('#v5-notice time')).toHaveAttribute('datetime', v5.observed_at);
  const outcomes = page.getByRole('region', { name: 'V5 outcomes and average request time' });
  await expect(outcomes.locator('tbody tr')).toHaveCount(2);
  await expect(outcomes).toContainText('Average HTTP 200');
  await expect(outcomes).toContainText('Average successful record');
  await expect(page.locator('#v5-latency')).toContainText('Explicit denominators.');
  if (v5.results_final) {
    for (const [index, stage] of v5.stages.entries()) {
      const cells = outcomes.locator('tbody tr').nth(index).getByRole('cell');
      await expect(cells.nth(0)).toHaveText(`${stage.succeeded} / 520`);
      await expect(cells.nth(1)).toHaveText(String(stage.failed));
      const mean = stage.http_200_latency_mean_seconds;
      if (mean !== null) await expect(cells.nth(5)).toHaveText(`${mean.toFixed(2)} s`);
    }
    await expect(page.getByRole('region', { name: 'V5 per-target observations' }).locator('tbody tr')).toHaveCount(10);
  } else {
    await expect(outcomes).toContainText('Not final');
    await expect(page.locator('#v5-notice')).toContainText('incomplete');
  }
  expect(await (await request.get('data/v5-results.json')).json()).toEqual(v5);
  await page.screenshot({ path: testInfo.outputPath('v5-request-averages.png'), fullPage: true });
});

test('theme survives page navigation and browser back without storage', async ({ page }, testInfo) => {
  await page.goto('/index.html?scoutTheme=light');
  await page.getByRole('button', { name: 'Switch to dark theme' }).click();
  await page.getByRole('navigation', { name: 'Main navigation', exact: true }).getByRole('link', { name: 'V2 complexity', exact: true }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await expect(page.locator('#v2 .evidence-context')).toHaveCSS('color', 'rgb(176, 176, 176)');
  await page.getByRole('navigation', { name: 'Main navigation', exact: true }).getByRole('link', { name: 'Compare versions', exact: true }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.goBack();
  expect(new URL(page.url()).pathname).toBe('/v2.html');
  await expect(page.getByRole('navigation', { name: 'Main navigation', exact: true }).locator('[aria-current="page"]')).toHaveText('V2 complexity');
  await page.screenshot({ path: testInfo.outputPath('multipage-dark.png') });
});

test('mobile tabs and all comparison versions remain reachable without horizontal scrolling', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/index.html?scoutTheme=light');
  for (const [label, file] of routes) {
    const tab = page.getByRole('navigation', { name: 'Main navigation', exact: true }).getByRole('link', { name: label, exact: true });
    await expect(tab).toBeVisible();
    await tab.click();
    expect(new URL(page.url()).pathname).toBe(`/${file}`);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  }
  await page.goto('/comparison.html?scoutTheme=light');
  const tableRegion = page.getByRole('region', { name: 'Configuration comparison across versions' });
  await tableRegion.scrollIntoViewIfNeeded();
  await expectComparisonsToFit(page);
  await expect(tableRegion.locator('tbody tr:not(.group-row)').first().locator('.comparison-version')).toHaveText(comparisonLabels);
  await page.screenshot({ path: testInfo.outputPath('multipage-mobile.png') });
});

test('old shared hash links redirect to their new dedicated pages', async ({ page }) => {
  for (const [hash, file] of [['v1', 'v1.html'], ['v2', 'v2.html'], ['v3', 'v3.html'], ['v4', 'v4.html'], ['v5', 'v5.html'], ['comparisons', 'comparison.html'], ['resources', 'resources.html'], ['start', 'resources.html']]) {
    await page.goto(`/?scoutTheme=light#${hash}`);
    await expect(page.locator('body')).toHaveAttribute('data-page', file);
    expect(new URL(page.url()).pathname).toBe(`/${file}`);
  }
});

test('evidence downloads and prompt assets work from the resources and comparison pages', async ({ page, request }) => {
  await page.goto('/resources.html');
  const paths = await page.locator('a[href^="data/"], a[href^="prompts/"]').evaluateAll(
    links => [...new Set(links.map(link => link.getAttribute('href')!))],
  );
  expect(paths.length).toBeGreaterThanOrEqual(5);
  for (const path of paths) expect((await request.get(path)).ok(), path).toBe(true);
  await page.goto('/comparison.html');
  expect(await (await request.get('data/v3-results.json')).json()).toEqual(v3);
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('link', { name: 'Download v2 evidence JSON' }).click();
  expect((await downloadPromise).suggestedFilename()).toBe('v2-results.json');
});

test('resource commands copy and clipboard denial stays explicit', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: async (value: string) => {
        document.documentElement.setAttribute('data-copied-text', value);
      } },
    });
  });
  await page.goto('/resources.html');
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

test('each real page and navigation works without JavaScript', async ({ browser }) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  const page = await context.newPage();
  await page.goto('http://127.0.0.1:8771/');
  for (const [label, file] of routes) {
    await page.getByRole('navigation', { name: 'Main navigation', exact: true }).getByRole('link', { name: label, exact: true }).click();
    expect(new URL(page.url()).pathname).toBe(`/${file}`);
    await expect(page.locator('h1')).toHaveCount(1);
    await expect(page.locator('#theme-toggle')).toBeHidden();
  }
  await page.goto('http://127.0.0.1:8771/comparison.html');
  await expect(page.locator('#outcomes')).toContainText('516.43 s');
  await page.setViewportSize({ width: 390, height: 844 });
  await expectComparisonsToFit(page);
  await expect(page.locator('.comparison-version').first()).toBeVisible();
  await context.close();
});
