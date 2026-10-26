import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  fullyParallel: false,
  workers: 1,
  timeout: 30000,
  use: { baseURL: 'http://127.0.0.1:8877', trace: 'retain-on-failure', locale: 'ru-RU' },
  webServer: {
    command: '.venv\\Scripts\\python.exe -m tests.web_fixture 8877',
    cwd: '..',
    url: 'http://127.0.0.1:8877/api/health',
    reuseExistingServer: false,
    timeout: 30000,
  },
});
