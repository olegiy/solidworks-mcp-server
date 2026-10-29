import { expect, test } from '@playwright/test';

test.use({ locale: 'en-US' });

test('the interface follows the browser language and switches between English and Russian', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'What shall we create today?' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'New project', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Settings', exact: true })).toBeVisible();
  await expect(page.locator('html')).toHaveAttribute('lang', 'en');
  await expect(page).toHaveTitle('CAD-Agent-Next — SolidWorks chat');

  await page.getByLabel('Interface language').selectOption('ru');
  await expect(page.getByRole('heading', { name: 'Что создадим сегодня?' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Новый проект', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Настройки', exact: true })).toBeVisible();
  await expect(page.locator('html')).toHaveAttribute('lang', 'ru');
  await expect(page).toHaveTitle('CAD-Agent-Next — чат SolidWorks');

  await page.reload();
  await expect(page.getByRole('button', { name: 'Новый проект', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Что создадим сегодня?' })).toBeVisible();

  await page.getByLabel('Язык интерфейса').selectOption('en');
  await expect(page.getByRole('heading', { name: 'What shall we create today?' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'New project', exact: true })).toBeVisible();
});

test('English settings and composer labels are translated', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('cadnext.v1.language', 'en'));
  await page.goto('/');
  const composer = page.getByRole('form', { name: 'Send a message' });
  await expect(composer).toBeVisible();
  await expect(composer.getByLabel('Active connection')).toBeVisible();
  await expect(composer.getByLabel('Message', { exact: true })).toBeVisible();
  await expect(composer.getByRole('combobox', { name: 'Reasoning effort', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Attach image', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeVisible();

  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Settings', exact: true });
  await expect(dialog.getByRole('heading', { name: 'Connections' })).toBeVisible();
  await expect(dialog.getByRole('button', { name: 'New connection' })).toBeVisible();
  await expect(dialog.getByLabel('Connection name')).toBeVisible();
  await expect(dialog.getByLabel('API address')).toBeVisible();
  await expect(dialog.getByLabel('API key', { exact: true })).toBeVisible();
  await expect(dialog.getByLabel('Always allow Python execution')).toBeVisible();
  await expect(dialog.getByLabel('Call limit')).toBeVisible();
  await page.getByRole('button', { name: 'Close settings' }).click();
});
