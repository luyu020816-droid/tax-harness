import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  fullyParallel: false,
  workers: 1,
  timeout: 90000,
  use: {
    baseURL: process.env.UI_BASE_URL ?? 'http://127.0.0.1:8101',
    viewport: { width: 1440, height: 900 },
    launchOptions: { executablePath: process.env.CHROME_PATH ?? 'C:/Program Files/Google/Chrome/Application/chrome.exe' },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    video: process.env.UI_RECORD === '1' ? { mode: 'on', size: { width: 1440, height: 900 } } : 'off',
  },
});
