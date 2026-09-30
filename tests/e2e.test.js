const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test, describe } = require('node:test');
const { chromium } = require('playwright');

const baseURL = process.env.POCKETSMART_BASE_URL || 'http://127.0.0.1:8000';
let account;
let authenticatedStorage;

async function withBrowser(testName, fn, storageState) {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext(storageState ? { storageState } : {});
  const page = await context.newPage();
  const httpErrors = [];
  const consoleErrors = [];
  page.on('response', response => {
    if ([401, 403, 404, 429, 500, 502, 503].includes(response.status())) {
      httpErrors.push({ status: response.status(), url: response.url() });
    }
  });
  page.on('console', message => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });
  page.on('pageerror', error => consoleErrors.push(error.message));
  try {
    await fn({ page, context, httpErrors, consoleErrors });
  } catch (error) {
    fs.mkdirSync(path.join(__dirname, '..', 'test-results'), { recursive: true });
    await page.screenshot({ path: path.join(__dirname, '..', 'test-results', `${testName}.png`), fullPage: true });
    throw error;
  } finally {
    await context.close();
    await browser.close();
  }
}

describe('PocketSmart browser workflows', { concurrency: false }, () => {
  test('1 — registration and login', { timeout: 60000 }, async () => {
    await withBrowser('registration-login', async ({ page, context }) => {
      account = {
        username: `pw-${Date.now()}-${Math.random().toString(16).slice(2, 7)}`,
        email: `pw-${Date.now()}@example.test`,
        password: `Pocket-${Date.now()}-test!`
      };
      await page.goto(baseURL);
      await page.getByRole('navigation').getByRole('link', { name: 'Get Started' }).click();
      await page.locator('input[name="username"]').fill(account.username);
      await page.locator('input[name="email"]').fill(account.email);
      await page.locator('input[name="password"]').fill(account.password);
      await page.locator('input[name="confirm_password"]').fill(account.password);
      const registerPassword = page.locator('#register-password');
      const confirmPassword = page.locator('#confirm-password');
      const registerToggle = page.locator('[data-password-target="register-password"]');
      const confirmToggle = page.locator('[data-password-target="confirm-password"]');
      assert.equal(await registerPassword.getAttribute('type'), 'password');
      assert.equal(await confirmPassword.getAttribute('type'), 'password');
      await registerToggle.click();
      assert.equal(await registerPassword.getAttribute('type'), 'text');
      assert.equal(await confirmPassword.getAttribute('type'), 'password', 'Password toggles must work independently');
      assert.equal(new URL(page.url()).pathname, '/register', 'Visibility buttons must not submit the form');
      await registerToggle.click();
      await confirmToggle.click();
      assert.equal(await confirmPassword.getAttribute('type'), 'text');
      assert.equal(await registerPassword.getAttribute('type'), 'password');
      await confirmToggle.click();
      await page.getByRole('button', { name: 'Create Account' }).click();
      await page.locator('#message').getByText('Account created. Sign in to continue.').waitFor();

      await page.goto(`${baseURL}/login`);
      await page.locator('input[name="username"]').fill(account.username);
      await page.locator('input[name="password"]').fill(account.password);
      const loginPassword = page.locator('#login-password');
      assert.equal(await loginPassword.getAttribute('type'), 'password');
      await page.locator('[data-password-target="login-password"]').click();
      assert.equal(await loginPassword.getAttribute('type'), 'text');
      assert.equal(new URL(page.url()).pathname, '/login', 'Visibility button must not submit the login form');
      await page.locator('[data-password-target="login-password"]').click();
      assert.equal(await loginPassword.getAttribute('type'), 'password');
      await page.getByRole('button', { name: 'Sign in' }).click();
      await page.waitForURL('**/dashboard');
      await page.getByRole('heading', { name: new RegExp(`Welcome, ${account.username}`) }).waitFor();
      authenticatedStorage = await context.storageState();
    });
  });

  test('2 — authenticated Home Planner recommendation flow', { timeout: 240000 }, async () => {
    assert.ok(authenticatedStorage, 'Test 1 must establish the authenticated browser session');
    await withBrowser('home-planner-flow', async ({ page, httpErrors, consoleErrors }) => {
      page.setDefaultTimeout(60000);
      await page.goto(`${baseURL}/home-planner`);
      await page.getByRole('heading', { name: 'Home Interior Budget Planner' }).waitFor();
      await page.locator('input[name="total_budget"]').fill('5000');
      await page.locator('input[name="num_lights"]').fill('4');
      await page.locator('input[name="num_fans"]').fill('2');
      await page.locator('input[name="num_furniture"]').fill('3');
      await page.locator('input[name="num_dining_tables"]').fill('1');
      await page.locator('input[name="has_living_room"]').check();
      await page.locator('textarea[name="additional_requirements"]').fill(
        'Modern and simple interior design. Prefer affordable and functional products with good quality.'
      );

      const apiResponse = page.waitForResponse(response =>
        new URL(response.url()).pathname === '/generate-home' && response.request().method() === 'POST'
      , { timeout: 95000 });
      await page.getByRole('button', { name: 'Generate Recommendations' }).click();
      const response = await apiResponse;
      assert.equal(response.status(), 200, `POST /generate-home returned ${response.status()}; planner error: ${await page.locator('#error').textContent()}`);
      await page.waitForURL('**/home-recommendations');
      await page.getByRole('heading', { name: 'Your Personalized Budget Plan' }).waitFor();
      await page.locator('#result').getByText('5000').waitFor();
      assert.equal(httpErrors.length, 0, `Unexpected HTTP errors: ${JSON.stringify(httpErrors)}`);
      assert.equal(consoleErrors.length, 0, `Browser errors: ${consoleErrors.join(' | ')}`);
    }, authenticatedStorage);
  });

  test('3 — protected planner navigation and history', { timeout: 240000 }, async () => {
    assert.ok(authenticatedStorage, 'Test 1 must establish the authenticated browser session');
    await withBrowser('protected-navigation', async ({ page, httpErrors, consoleErrors }) => {
      await page.goto(`${baseURL}/home-planner`);
      await page.getByRole('heading', { name: 'Home Interior Budget Planner' }).waitFor();
      await page.getByRole('link', { name: 'Party Planner' }).click();
      await page.getByRole('heading', { name: 'Party Budget Planner' }).waitFor();
      await page.locator('input[name="total_budget"]').fill('20000');
      await page.locator('input[name="num_guests"]').fill('30');
      await page.locator('select[name="party_type"]').selectOption('Birthday');
      await page.locator('input[name="venue_type"]').fill('Hall');
      await page.locator('input[name="needs_decoration"]').uncheck();
      await page.locator('input[name="needs_entertainment"]').uncheck();
      await page.locator('textarea[name="additional_requirements"]').fill('Reuse party search values');
      const partyResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/generate-party' && response.request().method() === 'POST', { timeout: 95000 });
      await page.getByRole('button', { name: 'Generate Budget Plan' }).click();
      assert.equal((await partyResponse).status(), 200);
      await page.waitForURL('**/party-recommendations');
      await page.goto(`${baseURL}/party-planner`);
      await page.getByRole('link', { name: 'Jewelry Planner' }).click();
      await page.getByRole('heading', { name: 'Jewelry Budget Planner' }).waitFor();
      await page.locator('input[name="total_budget"]').fill('75000');
      await page.locator('input[name="occasion"]').fill('Wedding');
      await page.locator('textarea[name="preferences"]').fill('Traditional gold jewelry with a modern design');
      const jewelryResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/generate-jewelry' && response.request().method() === 'POST', { timeout: 95000 });
      await page.getByRole('button', { name: 'Get Recommendations' }).click();
      assert.equal((await jewelryResponse).status(), 200);
      await page.waitForURL('**/jewelry-recommendations');
      await page.goto(`${baseURL}/history`);
      await page.getByRole('heading', { name: 'Your Recommendation History' }).waitFor();
      await page.locator('.history-card').filter({ hasText: 'Jewelry Budget' }).getByRole('link', { name: 'Reuse Search' }).click();
      await page.waitForURL('**/jewelry-planner?reuse=*');
      await page.locator('input[name="total_budget"]').waitFor();
      assert.equal(await page.locator('input[name="total_budget"]').inputValue(), '75000');
      assert.equal(await page.locator('input[name="occasion"]').inputValue(), 'Wedding');
      assert.equal(await page.locator('textarea[name="preferences"]').inputValue(), 'Traditional gold jewelry with a modern design');
      await page.goto(`${baseURL}/history`);
      await page.locator('.history-card').filter({ hasText: 'Party Budget' }).getByRole('link', { name: 'Reuse Search' }).click();
      await page.waitForURL('**/party-planner?reuse=*');
      await page.locator('input[name="total_budget"]').waitFor();
      assert.equal(await page.locator('input[name="total_budget"]').inputValue(), '20000');
      assert.equal(await page.locator('input[name="num_guests"]').inputValue(), '30');
      assert.equal(await page.locator('select[name="party_type"]').inputValue(), 'Birthday');
      assert.equal(await page.locator('input[name="venue_type"]').inputValue(), 'Hall');
      assert.equal(await page.locator('input[name="needs_catering"]').isChecked(), true);
      assert.equal(await page.locator('input[name="needs_decoration"]').isChecked(), false);
      assert.equal(await page.locator('input[name="needs_entertainment"]').isChecked(), false);
      assert.equal(await page.locator('textarea[name="additional_requirements"]').inputValue(), 'Reuse party search values');
      await page.goto(`${baseURL}/history`);
      await page.locator('.history-card').filter({ hasText: 'Home Budget' }).getByRole('link', { name: 'Reuse Search' }).click();
      await page.waitForURL('**/home-planner?reuse=*');
      await page.locator('input[name="total_budget"]').waitFor();
      assert.equal(await page.locator('input[name="total_budget"]').inputValue(), '5000');
      assert.equal(await page.locator('input[name="num_lights"]').inputValue(), '4');
      assert.equal(await page.locator('input[name="num_fans"]').inputValue(), '2');
      assert.equal(await page.locator('input[name="num_furniture"]').inputValue(), '3');
      assert.equal(await page.locator('input[name="num_dining_tables"]').inputValue(), '1');
      assert.equal(await page.locator('input[name="has_living_room"]').isChecked(), true);
      assert.equal(await page.locator('input[name="has_kitchen"]').isChecked(), false);
      assert.equal(await page.locator('input[name="has_bedroom"]').isChecked(), false);
      assert.equal(await page.locator('textarea[name="additional_requirements"]').inputValue(), 'Modern and simple interior design. Prefer affordable and functional products with good quality.');
      await page.goto(`${baseURL}/party-planner`);
      await page.getByRole('heading', { name: 'Party Budget Planner' }).waitFor();
      await page.goto(`${baseURL}/jewelry-planner`);
      await page.getByRole('heading', { name: 'Jewelry Budget Planner' }).waitFor();
      await page.goto(`${baseURL}/history`);
      await page.getByRole('heading', { name: 'Your Recommendation History' }).waitFor();
      await page.locator('article').filter({ hasText: 'Home Budget' }).first().waitFor();
      const sessionResponse = await page.request.get(`${baseURL}/session-info`);
      assert.equal(sessionResponse.status(), 200, 'Session should remain valid across navigation');
      assert.equal(httpErrors.length, 0, `Unexpected HTTP errors: ${JSON.stringify(httpErrors)}`);
      assert.equal(consoleErrors.length, 0, `Browser errors: ${consoleErrors.join(' | ')}`);
    }, authenticatedStorage);
  });
});
