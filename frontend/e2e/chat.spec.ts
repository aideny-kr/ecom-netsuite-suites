import { test, expect } from '@playwright/test';
import { registerAndLogin } from './helpers';
import * as http from 'http';
import { AddressInfo } from 'net';

test.describe('Chat Messaging UI', () => {
    let server: http.Server;
    let mockUrl: string;

    test.beforeAll(async () => {
        server = http.createServer((req, res) => {
            // Support CORS requests
            res.setHeader('Access-Control-Allow-Origin', '*');
            res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
            res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');

            if (req.method === 'OPTIONS') {
                res.writeHead(200);
                res.end();
                return;
            }

            res.writeHead(200, {
                'Content-Type': 'text/event-stream',
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
            });

            // Simulate real-time SSE chunk streaming with delays
            res.write('data: ' + JSON.stringify({ content: "Hello," }) + '\n\n');
            setTimeout(() => {
                res.write('data: ' + JSON.stringify({ content: " streaming" }) + '\n\n');
            }, 200);
            setTimeout(() => {
                res.write('data: ' + JSON.stringify({ content: " response." }) + '\n\n');
                res.end();
            }, 400);
        });

        await new Promise<void>((resolve) => server.listen(0, resolve));
        const port = (server.address() as AddressInfo).port;
        mockUrl = `http://localhost:${port}`;
    });

    test.afterAll(() => {
        if (server) {
            server.close();
        }
    });

    test('should progressively render streaming response', async ({ page }) => {
        await registerAndLogin(page);

        // Mock the streaming API endpoint to redirect to the local SSE server
        await page.route('**/api/v1/chat/stream*', async (route) => {
            // route.fallback forwards the request to the specified url (mock server)
            // wait, Playwright fallback just rewrites the URL if we mutate request.url,
            // but if we call fetch within route.fulfill, it's easier.
            // Easiest is to fetch from mock builder and fulfill.
            try {
                const fetchRes = await fetch(mockUrl, {
                    method: route.request().method(),
                    headers: route.request().headers(),
                });

                // Actually, route.fulfill with a stream from fetch is tricky in older Node.
                // It's safer to just rewrite url with page.route! 
                // Oh wait, route.continue({ url: mockUrl }) rewrites.
                await route.continue({ url: mockUrl });
            } catch (e) {
                await route.continue();
            }
        });

        // Navigate to chat
        await page.goto('/chat');

        // Ensure we find the right input
        const chatInput = page.getByRole('textbox').or(page.getByPlaceholder(/ask a question|message/i)).first();
        await chatInput.fill('This is a simulated test message');

        // Submit form (either button or enter key)
        const sendButton = page.getByRole('button', { name: /send/i, exact: false }).or(page.locator('button[type="submit"]'));
        if (await sendButton.isVisible()) {
            await sendButton.click();
        } else {
            await page.keyboard.press('Enter');
        }

        // Progressively assert text rendering on screen
        await expect(page.locator('text="Hello,"')).toBeVisible();
        await expect(page.locator('text="Hello, streaming"')).toBeVisible();
        await expect(page.locator('text="Hello, streaming response."')).toBeVisible();
    });
});
