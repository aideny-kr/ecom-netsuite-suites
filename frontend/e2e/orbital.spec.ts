/** Orbital acceptance against API fixtures. No live writes or model calls. */
import { test, expect, type Page } from "@playwright/test";
import { createServer, type ServerResponse } from "node:http";
import { reportReadingFixture } from "../src/lib/__fixtures__/report-presentation";
const API = "http://localhost:18000",
  token = "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjQ3NDA4NDQ4MDB9.signature",
  now = "2026-09-13T12:00:00Z";
test.beforeEach(async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  page.on("pageerror", (error) => console.error("PAGE ERROR", error.message));
});
const session = {
  id: "s1",
  title: "Read-only investigation",
  status: "completed",
  is_archived: false,
  created_at: now,
  updated_at: now,
};

async function expectStyledWorkbook(downloaded: Uint8Array, original: Uint8Array) {
  const {unzipSync,strFromU8} = await import('fflate');
  const before = unzipSync(original), after = unzipSync(downloaded);
  expect(Object.keys(after).sort()).toEqual(Object.keys(before).sort());
  const sheet = 'xl/worksheets/sheet1.xml';
  // The fixture's only removed cell is its generated row-count footer, A8.
  expect(strFromU8(after[sheet]).match(/<c\b[\s\S]*?<\/c>/g)).toEqual(strFromU8(before[sheet]).match(/<c\b[\s\S]*?<\/c>/g)?.filter(cell=>!cell.includes('r="A8"')));
  expect(strFromU8(after['xl/styles.xml'])).toContain('FF243442');
  expect(strFromU8(after[sheet])).toContain('showGridLines="0"');
  for (const path of Object.keys(before).filter(path=>![sheet,'xl/styles.xml'].includes(path))) expect(after[path]).toEqual(before[path]);
}
async function fixture(page: Page, role = "admin") {
  await page
    .context()
    .addCookies([
      { name: "access_token", value: token, domain: "localhost", path: "/" },
    ]);
  await page.addInitScript((t) => {
    if (window !== window.top) return; // Seed the app only; report frames stay opaque.
    localStorage.setItem("access_token", t);
    localStorage.setItem("theme", "dark");
    localStorage.setItem("onboarding_skipped", "true");
  }, token);
  const state = {
    writes: [] as { path: string; body: unknown }[],
    messages: [] as unknown[],
    failSave: false,
    description: "Local verification company",
    streamUrl: "",
    cancelled: false,
  };
  await page.route(/http:\/\/localhost:(18000|13005)\/api\//, async (route) => {
    const request = route.request(),
      path = new URL(request.url()).pathname;
    if (path.endsWith("/stream") && state.streamUrl)
      return route.continue({ url: state.streamUrl });
    let data: unknown = [],
      status = 200;
    if (!["GET", "OPTIONS"].includes(request.method()))
      state.writes.push({
        path,
        body: request.postData() ? request.postDataJSON() : null,
      });
    if (path === "/api/v1/auth/me")
      data = {
        id: "u1",
        tenant_id: "t1",
        tenant_name: "Orbital Test Company",
        email: "orbital@test.local",
        full_name: "Test Operator",
        roles: [role],
        onboarding_completed_at: now,
      };
    else if (path === "/api/v1/settings/features")
      data = {
        flags: {
          chat: true,
          workspace: true,
          reconciliation: true,
          celigo: true,
          custom_branding: true,
          analytics_export: true,
        },
      };
    else if (path === "/api/v1/onboarding/soul")
      data = { exists: false, bot_tone: null, netsuite_quirks: null };
    else if (path === "/api/v1/jobs")
      data = { items: [], total: 0, page: 1, page_size: 10, pages: 0 };
    else if (path === "/api/v1/settings/branding")
      data = { brand_name: "Framework", brand_color_hsl: null };
    else if (path.includes("/connection/validate")) data = { valid: true };
    else if (path === "/api/v1/connections/health") data = { connections: [], mcp_connectors: [] };
    else if (path === "/api/v1/tenants/me/plan")
      data = { plan: "self_hosted", limits: { max_schedules: -1 }, usage: { schedules: 0 } };
    else if (path === "/api/v1/chat/sessions")
      data = request.method() === "POST" ? session : [session];
    else if (path === "/api/v1/chat/sessions/s1")
      data = { ...session, messages: state.messages };
    else if (path === "/api/v1/chat/sessions/s1/messages") {
      state.messages.push({
        id: "u1",
        role: "user",
        content: request.postDataJSON().content,
        created_at: now,
      });
      data = { run_id: "r1" };
    } else if (path.endsWith("/cancel")) {
      state.cancelled = true;
      data = { status: "cancelling" };
    } else if (path === "/api/v1/schedules")
      data = {
        schedules: [],
        runs_last_7_days_total: 0,
        runs_last_7_days_failed: 0,
      };
    else if (path === "/api/v1/onboarding/profiles/active")
      data = {
        id: "p1",
        industry: "Retail / E-commerce",
        business_description: state.description,
        team_size: "1-5",
        version: 1,
        status: "confirmed",
      };
    else if (
      path === "/api/v1/onboarding/profiles" &&
      request.method() === "POST"
    ) {
      if (state.failSave) {
        status = 500;
        data = { detail: "Profile could not be saved" };
      } else {
        state.description = request.postDataJSON().business_description;
        data = { id: "p1" };
      }
    } else if (path.includes("/metadata") || path === "/api/v1/settings/ai")
      data = {};
    await route.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify(data),
    });
  });
  return state;
}
async function screenshot(page: Page, name: string) {
  await page.locator("main").evaluate((el) =>
    Promise.all(
      el
        .getAnimations({ subtree: true })
        .filter((a) => a.effect?.getComputedTiming().iterations !== Infinity)
        .map((a) => a.finished.catch(() => {})),
    ),
  );
  if (process.env.ORBITAL_EVIDENCE)
    await page.screenshot({
      path: `${process.env.ORBITAL_EVIDENCE}/${name}.png`,
    });
}
test("Command Center landing, merged Chat, legacy draft links, theme and motion", async ({
  page,
}) => {
  const state = await fixture(page);
  await page.goto("/");
  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.getByRole("heading", {name:"Command Center",exact:true})).toBeVisible();
  const navigation = page.getByRole("complementary", {name:"Workspace sidebar"});
  await expect(navigation.getByRole("link").first()).toHaveText("Command Center");
  await expect(navigation.getByRole("link", {name:"Workbench",exact:true})).toHaveCount(0);
  await screenshot(page, "command-center");
  await page.goto("/workbench?compose=Inspect%20evidence%20without%20changing%20records");
  await expect(page).toHaveURL(/\/chat\?compose=/);
  await expect(
    page.getByRole("heading", { name: "Chat", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Pause motion" }).click();
  await expect(page.locator(".metal-orbits svg")).toHaveCSS(
    "animation-play-state",
    "paused",
  );
  await page.reload();
  await expect(
    page.getByRole("button", { name: "Resume motion" }),
  ).toBeVisible();
  await expect(page.getByRole("textbox", {name:"Message",exact:true})).toHaveCount(1);
  await expect(page.getByRole("button", {name:"Continue in Chat"})).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: "Message" })).toHaveValue(
    "Inspect evidence without changing records",
  );
  expect(state.writes).toHaveLength(0);
  await screenshot(page, "chat-desktop");
  await page.goto("/workbench");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await expect(
    page.getByRole("button", { name: "Reduced motion" }),
  ).toBeDisabled();
  await expect(page.locator(".metal-orbits svg")).toHaveCSS(
    "animation-name",
    "none",
  );
  await page.getByRole("button", { name: "Light Mode" }).click();
  await screenshot(page, "chat-welcome-light");
  await page.setViewportSize({ width: 390, height: 844 });
  await screenshot(page, "chat-welcome-mobile");
  await expect(
    page.getByRole("link", { name: "Chat", exact: true }),
  ).toBeHidden();
  await page.getByRole("button", { name: "Open sidebar" }).click();
  await page.getByRole("link", { name: "Chat", exact: true }).click();
  await expect(page.getByRole("complementary", {name:"Workspace sidebar"})).toBeHidden();
  await expect(page.getByRole("textbox", { name: "Message" })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Open chat history" }),
  ).toBeVisible();
  await screenshot(page, "chat-mobile");
  expect(
    await page.evaluate(
      () =>
        document.documentElement.scrollWidth <= innerWidth &&
        document.querySelector("main")!.scrollWidth <=
          document.querySelector("main")!.clientWidth,
    ),
  ).toBe(true);
});
test("Command Center sculpture, launch links, keyboard and motion preferences", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/dashboard");
  const launch = page.getByRole("region", { name: "Launch your next task" });
  await expect(launch).toBeVisible();
  await page.getByRole("button", { name: "Pause motion" }).click();
  await expect(launch).toHaveAttribute("data-motion-paused", "true");
  await page.reload();
  await expect(page.getByRole("button", { name: "Resume motion" })).toBeVisible();
  const actions = page.getByRole("navigation", { name: "Command Center starting actions" });
  const chat = actions.getByRole("link", { name: /Ask in Chat/ });
  await chat.focus();
  await expect(chat).toBeFocused();
  await screenshot(page, "command-center-focus");
  await chat.click();
  await expect(page).toHaveURL(/\/chat$/);
  await expect(page.getByRole("textbox", { name: "Message", exact: true })).toHaveValue("");
  await page.goto("/dashboard");
  await actions.getByRole("link", { name: /Explore transactions/ }).click();
  await expect(page).toHaveURL(/\/tables\/orders\?view=records$/);
  await page.goto("/dashboard");
  await actions.getByRole("link", { name: /Open reports/ }).click();
  await expect(page).toHaveURL(/\/reports$/);
  await page.goto("/dashboard");
  await page.getByRole("button", { name: "Light Mode" }).click();
  await screenshot(page, "command-center-light");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await expect(page.getByRole("button", { name: "Reduced motion" })).toBeDisabled();
  const animations = await launch.locator("svg g").evaluateAll(nodes => nodes.map(node => getComputedStyle(node).animationName));
  expect(animations.every(name => name === "none")).toBe(true);
  await page.setViewportSize({ width: 390, height: 844 });
  await screenshot(page, "command-center-mobile");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(state.writes).toHaveLength(0);
});

test("Report reading view exposes full chart labels and values in the sandbox", async ({ page }) => {
  const state = await fixture(page);
  await page.route("**/api/v1/reports/layout-fixture**", async route => {
    const path = new URL(route.request().url()).pathname;
    await route.fulfill(path.endsWith("/view") ? { contentType: "text/html", body: reportReadingFixture } : {
      contentType: "application/json", body: JSON.stringify(path.endsWith("/versions") ? [] : {
        id: "layout-fixture", title: "Report layout fixture", version: 1, status: "draft", created_at: now, has_recipe: false,
      }),
    });
  });
  await page.goto("/reports/layout-fixture");
  const iframe = page.locator('iframe[title="Report"]');
  await expect(iframe).toHaveAttribute("sandbox", "");
  const frame = page.frameLocator('iframe[title="Report"]');
  await expect(frame.getByText("10001 - A very long receivables category with an unabridged name", { exact: true })).toBeVisible();
  await expect(frame.getByText("-1,234.567891", { exact: true })).toBeVisible();
  await screenshot(page, "report-reading-desktop");
  const downloaded = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download HTML" }).click();
  const download = await downloaded;
  const {readFile} = await import("node:fs/promises");
  expect(await readFile((await download.path())!, "utf8")).toBe(reportReadingFixture);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(frame.getByText("10002 - Inventory & equipment", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Publish to Command Center" })).toBeInViewport();
  await screenshot(page, "report-reading-mobile");
  const contentFrame = await (await iframe.elementHandle())!.contentFrame();
  expect(await contentFrame!.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(state.writes).toHaveLength(0);
});

for (const family of ["inventory", "financial"]) {
  test(`${family} report retains controls and reflows in reports and Command Center`, async ({ page }, testInfo) => {
    const { readFile } = await import("node:fs/promises");
    const { join } = await import("node:path");
    const html = await readFile(join(testInfo.project.testDir, `fixtures/reports/${family}.html`), "utf8");
    const state = await fixture(page);
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    const report = { id: "family-fixture", title: `${family} analysis`, version: 1, status: "published", created_at: now, has_recipe: false };
    await page.route("**/api/v1/reports/family-fixture**", route => {
      expect(route.request().method()).toBe("GET");
      const path = new URL(route.request().url()).pathname;
      return route.fulfill(path.endsWith("/view") ? { contentType: "text/html", body: html } : { json: path.endsWith("/versions") ? [] : report });
    });
    await page.route("**/api/v1/dashboard", route => route.fulfill({ json: { active: report, published: [report], active_is_fallback: false } }));
    await page.goto("/reports/family-fixture");
    const iframe = page.locator('iframe[title="Report"]');
    const frame = page.frameLocator('iframe[title="Report"]');
    await expect(iframe).toHaveAttribute("sandbox", "");
    await expect(frame.locator(`.orbital-${family}`)).toBeVisible();
    await expect(frame.locator("svg")).toHaveCount(family === "inventory" ? 4 : 5);
    if (family === "inventory") {
      await frame.locator("summary").click();
      await expect(frame.locator("details")).toHaveAttribute("open", "");
      await expect(frame.locator("details tbody tr")).toHaveCount(6); // Four SKUs and two location headers.
      await frame.locator("summary").click();
      await expect(frame.locator("details")).not.toHaveAttribute("open", "");
    } else {
      const revenue = frame.getByRole("checkbox", { name: "Revenue", exact: false });
      await revenue.uncheck();
      await expect(frame.locator(".fs-of-0").first()).toBeHidden();
      await expect(frame.getByText("Total Revenue", { exact: true })).toBeVisible();
      await revenue.check();
      await expect(frame.locator(".fs-of-0").first()).toBeVisible();
      await expect(frame.locator(".fs-stmt .fs-net td.fs-bad")).toHaveText("−$51,700");
    }
    await frame.locator("h1").scrollIntoViewIfNeeded();
    await page.emulateMedia({ media: "print" });
    await expect(frame.locator(".orbital-scroll-hint").first()).toBeHidden();
    await page.emulateMedia({ media: "screen" });
    await screenshot(page, `report-${family}-desktop`);
    await page.setViewportSize({ width: 390, height: 844 });
    const chart = frame.locator(".orbital-chart-scroll").first();
    await chart.focus();
    await expect(chart).toBeFocused();
    expect(await chart.evaluate(el => el.scrollWidth > el.clientWidth)).toBe(true);
    await chart.press("ArrowRight");
    await expect.poll(() => chart.evaluate(el => el.scrollLeft)).toBeGreaterThan(0);
    const contentFrame = await (await iframe.elementHandle())!.contentFrame();
    expect(await contentFrame!.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await screenshot(page, `report-${family}-mobile`);
    await page.goto("/dashboard");
    const wall = page.locator(`iframe[title="${report.title}"]`);
    await expect(wall).toHaveAttribute("sandbox", "");
    await expect(wall).toHaveCSS("transform", "none");
    await expect(page.frameLocator(`iframe[title="${report.title}"]`).locator(`.orbital-${family}`)).toBeVisible();
    const wallFrame = await (await wall.elementHandle())!.contentFrame();
    expect(await wallFrame!.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await screenshot(page, `command-center-${family}-mobile`);
    expect(errors).toEqual([]);
    expect(state.writes).toHaveLength(0);
  });
}

test("Settings draft, save error, existing save contract and deep links", async ({
  page,
}) => {
  const state = await fixture(page);
  await page.goto("/settings");
  const profile = page
    .locator("div.space-y-4")
    .filter({
      has: page.getByRole("heading", { name: "Company profile", exact: true }),
    })
    .first();
  await profile.getByRole("button", { name: "Edit", exact: true }).click();
  await page
    .getByRole("textbox", { name: "Business description" })
    .fill("Unsaved Orbital draft");
  await page
    .getByRole("navigation", { name: "Settings sections" })
    .getByRole("link", { name: /Connections/ })
    .click();
  await expect(
    page.getByRole("heading", { name: "Connected systems", exact: true }),
  ).toBeVisible();
  await screenshot(page, "settings-connections");
  await page
    .getByRole("navigation", { name: "Settings sections" })
    .getByRole("link", { name: /Workspace/ })
    .click();
  await expect(
    page.getByRole("textbox", { name: "Business description" }),
  ).toHaveValue("Unsaved Orbital draft");
  state.failSave = true;
  await page.getByRole("button", { name: "Save Profile" }).click();
  await expect(
    page.getByRole("alert").filter({ hasText: "Profile could not be saved" }),
  ).toBeVisible();
  await expect(
    page.getByRole("textbox", { name: "Business description" }),
  ).toHaveValue("Unsaved Orbital draft");
  state.failSave = false;
  await page.getByRole("button", { name: "Save Profile" }).click();
  await expect(
    page.getByText("Unsaved Orbital draft", { exact: true }),
  ).toBeVisible();
  await expect
    .poll(() => state.writes.map((w) => w.path))
    .toEqual([
      "/api/v1/onboarding/profiles",
      "/api/v1/onboarding/profiles",
      "/api/v1/onboarding/profiles/p1/confirm",
    ]);
  await screenshot(page, "settings-desktop");
  await page.goto("/connections");
  await expect(
    page.getByRole("heading", { name: "Connected systems", exact: true }),
  ).toBeVisible();
  await page.goto("/settings#agent");
  await expect(
    page.getByRole("heading", { name: "Skills and company context" }),
  ).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await screenshot(page, "settings-mobile");
  expect(
    await page.evaluate(
      () =>
        document.documentElement.scrollWidth <= innerWidth &&
        document.querySelector("main")!.scrollWidth <=
          document.querySelector("main")!.clientWidth,
    ),
  ).toBe(true);
});
test("Viewer status, workflow and developer surfaces", async ({ page }) => {
  const state = await fixture(page, "readonly");
  await page.goto("/connections");
  await expect(
    page.getByText(
      "You can view connections. A connection manager can add, test, or delete them.",
    ),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Add Connection", exact: true }),
  ).toHaveCount(0);
  expect(state.writes).toHaveLength(0);
  await page.goto("/scheduled-jobs");
  await expect(
    page.getByRole("heading", { name: "Workflows", exact: true }),
  ).toBeVisible();
  await screenshot(page, "workflows-desktop");
  await page.goto("/workspace");
  await expect(
    page.getByText("Explorer", { exact: true }).first(),
  ).toBeVisible();
  await screenshot(page, "developer-desktop");
});
test("Timed SSE preserves text, completion and cancel contracts (fixture)", async ({
  page,
}, testInfo) => {
  const state = await fixture(page);
  let firstEvent = 0,
    submitted = 0,
    finalEvent = 0;
  let response: ServerResponse | undefined;
  const server = createServer((req, res) => {
    res.setHeader(
      "Access-Control-Allow-Origin",
      req.headers.origin ||
        new URL(process.env.BASE_URL || "http://localhost:13005").origin,
    );
    res.setHeader("Access-Control-Allow-Credentials", "true");
    res.setHeader("Access-Control-Allow-Headers", "authorization,content-type");
    if (req.method === "OPTIONS") {
      res.end();
      return;
    }
    response = res;
    res.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
    });
    firstEvent = performance.now();
    res.write(
      `data: ${JSON.stringify({ type: "text", content: "Read-only evidence arrives. " })}\n\n`,
    );
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string") throw Error("No fixture port");
  state.streamUrl = `http://127.0.0.1:${address.port}/stream`;
  try {
    await page.goto("/chat");
    await page
      .getByRole("textbox", { name: "Message" })
      .fill("Summarize the read-only fixture");
    submitted = performance.now();
    await page.getByRole("button", { name: "Send message" }).click();
    await expect(
      page.getByText("Read-only evidence arrives.", { exact: true }),
    ).toBeVisible();
    const visible = performance.now();
    await page.getByRole("button", { name: "Stop response" }).click();
    await expect.poll(() => state.cancelled).toBe(true);
    const content = "Read-only evidence arrives. Final content is preserved.";
    const message = {
      id: "a1",
      role: "assistant",
      content,
      tool_calls: null,
      citations: null,
      created_at: now,
    };
    state.messages.push(message);
    finalEvent = performance.now();
    response!.write(
      `data: ${JSON.stringify({ type: "text", content: "Final content is preserved." })}\n\n`,
    );
    response!.write(
      `data: ${JSON.stringify({ type: "message", message })}\n\n`,
    );
    response!.end();
    await expect(page.getByText(content, { exact: true })).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Stop response" }),
    ).toHaveCount(0);
    const completed = performance.now();
    expect(
      state.writes.find((w) => w.path.endsWith("/messages"))?.body,
    ).toEqual({ content: "Summarize the read-only fixture" });
    await testInfo.attach("fixture-stream-timing", {
      body: JSON.stringify({
        submitToFirstServerEventMs: firstEvent - submitted,
        submitToVisibleTextMs: visible - submitted,
        eventToVisibleUpperBoundMs: visible - firstEvent,
        finalEventToCompletionMs: completed - finalEvent,
        note: "Loopback fixture; assertion polling included. No model/tool latency measured.",
      }),
      contentType: "application/json",
    });
  } finally {
    response?.end();
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});


test("One settings destination, contextual setup, clear interactions and layered motion", async ({ page }) => {
  await fixture(page);
  await page.route("**/api/v1/onboarding/checklist/connection/validate", route => route.fulfill({json:{valid:false}}));
  await page.goto("/workbench");
  await expect(page.getByRole("heading", {name:"Chat",exact:true})).toBeVisible();
  const sidebar = page.getByRole("complementary", {name:"Workspace sidebar"});
  await expect(sidebar.getByRole("link", {name:"Settings",exact:true})).toHaveCount(1);
  await expect(sidebar.getByRole("link", {name:"Connections",exact:true})).toHaveCount(0);
  await expect(page.getByText("NetSuite is not connected.")).toHaveCount(0);
  await expect(page.getByText("Connect NetSuite when your work needs NetSuite data.", {exact:false})).toHaveCount(0);
  const card = page.getByRole("link", {name:"Build a workflow",exact:true});
  const before = await card.evaluate(el => getComputedStyle(el).backgroundColor);
  await card.hover();
  await expect.poll(() => card.evaluate(el => getComputedStyle(el).backgroundColor)).not.toBe(before);
  for (const selector of [".metal-orbits svg", ".metal-orbit-plane", ".metal-orbit-traveler"])
    await expect(page.locator(selector).first()).toHaveCSS("animation-play-state", "running");
  await page.getByRole("button", {name:"Pause motion",exact:true}).click();
  for (const selector of [".metal-orbits svg", ".metal-orbit-plane", ".metal-orbit-traveler"])
    await expect(page.locator(selector).first()).toHaveCSS("animation-play-state", "paused");
  await page.setViewportSize({width:1200,height:900});
  await page.getByRole("textbox", {name:"Message",exact:true}).fill("Laptop draft");
  await expect(page.getByRole("button", {name:"Send message",exact:true})).toBeEnabled();
  await page.setViewportSize({width:1280,height:900});
  await page.getByRole("navigation", {name:"Chat starting actions"}).getByRole("link", {name:"Developer workspace"}).click({trial:true});
  await page.setViewportSize({width:1440,height:1000});
  await page.goto("/connections");
  await expect(page.getByRole("heading", {name:"Settings",exact:true})).toBeVisible();
  await expect(page.getByRole("link", {name:"Settings",exact:true})).toHaveAttribute("aria-current","page");
  await expect(page.getByRole("heading", {name:"Connected systems",exact:true})).toBeVisible();
  await expect(page.getByText("NetSuite Connections failed to load")).toHaveCount(0);
  await page.getByText("Celigo", {exact:true}).click();
  await page.getByRole("textbox", {name:"API Token",exact:true}).fill("unsaved-test-token");
  await page.getByRole("navigation", {name:"Settings sections"}).getByRole("link", {name:/Workspace Profile/}).click();
  await page.getByRole("navigation", {name:"Settings sections"}).getByRole("link", {name:/Connections Systems/}).click();
  await expect(page.getByRole("textbox", {name:"API Token",exact:true})).toHaveValue("unsaved-test-token");
  await page.goto("/settings#celigo");
  await expect(page.getByRole("textbox", {name:"API Token",exact:true})).toBeVisible();
  await page.route("**/api/v1/onboarding/checklist/connection/validate", route => route.fulfill({json:{valid:false,connection_status:"error",error_reason:"Authorization expired"}}));
  await page.goto("/workbench");
  await expect(page.getByRole("status").filter({hasText:"Authorization expired"})).toBeVisible();
  await expect(page.getByRole("link", {name:"Reconnect in Settings"})).toHaveAttribute("href", "/settings#connections");
});


test('Keyboard entry, workflow clarification recovery, Skills and Reports', async ({page}) => {
  await fixture(page);
  await page.goto('/workbench');await expect(page.getByRole('heading',{name:'Chat',exact:true})).toBeVisible();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('link', {name:'Skip to content'})).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.locator('#main-content')).toBeFocused();
  await page.route(/\/api\/v1\/schedules$/, route => route.request().method()==='POST' ? route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({detail:{clarification:'Which source should this report use?'}})}) : route.fallback());
  await page.goto('/scheduled-jobs/new');await expect(page.locator('.animate-fade-in').first()).toHaveCSS('animation-name','orbital-arrive');
  const instruction=page.getByPlaceholder('Every Friday at 6pm, run the Stripe payout reconciliation for the week, hold anything that needs review, and email me the exception summary.');
  await instruction.fill('Prepare a weekly read-only report');
  await page.getByRole('button',{name:'Compile plan →'}).click();
  await expect(page.getByText(/Which source should this report use/)).toBeVisible();
  await expect(page.getByRole('button',{name:'Compile plan →'})).toBeDisabled();
  await page.getByPlaceholder("Answer the agent's question…").fill('Use the approved inventory source');
  await expect(page.getByRole('button',{name:'Compile plan →'})).toBeEnabled();
  await page.getByRole('button',{name:'Back',exact:true}).click();
  await expect(instruction).toHaveValue('Prepare a weekly read-only report');
  await screenshot(page,'workflow-clarification-recovery');
  await page.goto('/skills');await expect(page.getByRole('heading',{name:'Skills',exact:true})).toBeVisible();await screenshot(page,'skills-desktop');
  await page.goto('/reports');await expect(page.getByRole('heading',{name:'Reports',exact:true})).toBeVisible();await screenshot(page,'reports-desktop');
  const ws={id:'ws1',name:'Evidence project',status:'active',tenant_id:'t1',created_by:'u1',created_at:now,updated_at:now};
  const file={id:'f1',name:'example.sql',path:'example.sql',is_directory:false};
  await page.route(/\/api\/v1\/workspaces(?:[/?]|$)/,route=>{
    const path=new URL(route.request().url()).pathname;
    const data=path==='/api/v1/workspaces'?[ws]:path==='/api/v1/workspaces/ws1'?ws:path==='/api/v1/workspaces/ws1/files'?[file]:path==='/api/v1/workspaces/ws1/files/f1'?{id:'f1',path:'example.sql',file_name:'example.sql',content:'SELECT id FROM transaction;',truncated:false,total_lines:1,mime_type:'text/plain'}:[];
    return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(data)});
  });
  await page.goto('/workspace?workspace=ws1&file=example.sql');
  await expect(page.locator('code').filter({hasText:'SELECT id FROM transaction;'}).first()).toBeVisible();
  await screenshot(page,'developer-file');
});

test('Transactions unifies records, comparisons, cases, approvals and history without submitting work', async ({page}) => {
  const state = await fixture(page);
  const errors: string[] = [];
  page.on('pageerror', error => { errors.push(error.message); void test.info().attach('page-error', {body:error.stack || error.message,contentType:'text/plain'}); });
  await page.route('**/api/v1/transaction-ops/**', route => {
    const path = new URL(route.request().url()).pathname;
    const data = path.endsWith('/workspace-page') ? {items:[],total:0,has_next:false} : path.endsWith('/case-groups') ? {groups:[],has_next:false,total_groups:0,total_cases:0} : [];
    return route.fulfill({json:data});
  });
  await page.route('**/api/v1/tables/**', route => route.fulfill({json:{items:[],total:0,page:1,pages:1,page_size:25}}));
  await page.route('**/api/v1/reconciliation/data-status', route => route.fulfill({json:{stripe:{connected:false,last_sync:null,status:'disconnected'},netsuite:{connected:false,last_sync:null,status:'disconnected'}}}));
  await page.goto('/transactions');
  await expect(page).toHaveURL(/\/tables\/orders\?view=records$/);
  const sidebar = page.getByRole('complementary',{name:'Workspace sidebar'});
  await expect(sidebar.getByRole('link',{name:'Transactions',exact:true})).toHaveAttribute('aria-current','page');
  await expect(sidebar.getByRole('link',{name:/^(Investigations|Reconciliation|Orders)$/})).toHaveCount(0);
  const nav = page.getByRole('navigation',{name:'Transaction sections'});
  await expect(page.getByRole('navigation',{name:'Record types'}).getByRole('link')).toHaveCount(7);
  await page.getByRole('combobox',{name:'Currency',exact:true}).selectOption('USD');
  await screenshot(page,'transactions-records');
  await nav.getByRole('link',{name:'Reconcile',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Order consistency',exact:true})).toBeVisible();
  await page.getByRole('combobox',{name:'Review period'}).selectOption('last_month');
  await screenshot(page,'transactions-reconcile');
  await nav.getByRole('link',{name:'Cases',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Open cases',exact:true})).toBeVisible();
  await expect(page.getByRole('combobox',{name:'Review period'})).toBeHidden();
  await screenshot(page,'transactions-cases');
  await nav.getByRole('link',{name:'Approvals',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Order correction approvals',exact:true})).toBeVisible();
  await screenshot(page,'transactions-approvals');
  await nav.getByRole('link',{name:'History',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Order review history',exact:true})).toBeVisible();
  await nav.getByRole('link',{name:'Reconcile',exact:true}).click();
  await expect(page.getByRole('combobox',{name:'Review period'})).toHaveValue('last_month');
  await nav.getByRole('link',{name:'Records',exact:true}).click();
  await expect(page.getByRole('combobox',{name:'Currency',exact:true})).toHaveValue('USD');
  await page.getByRole('navigation',{name:'Record types'}).getByRole('link',{name:'Payments',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Payments',exact:true})).toBeVisible();
  await page.goto('/reconciliation');
  await expect(page.getByRole('heading',{name:'Payment / deposit matching',exact:true})).toBeVisible();
  await nav.getByRole('link',{name:'Approvals',exact:true}).click();
  await expect(page).toHaveURL(/\/reconciliation\?view=approvals$/);
  await expect(page.getByRole('navigation',{name:'Approval types'}).getByRole('link',{name:'Payment matching',exact:true})).toHaveAttribute('aria-current','page');
  await screenshot(page,'transactions-payment-matching');
  await page.goto('/transaction-operations');
  await expect(nav.getByRole('link',{name:'Cases',exact:true})).toHaveAttribute('aria-current','page');
  await expect(page.getByRole('heading',{name:'Investigations',exact:true})).toBeVisible();
  await page.setViewportSize({width:390,height:844});
  await screenshot(page,'transactions-mobile');
  expect(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(state.writes).toHaveLength(0);
  expect(errors).toEqual([]);
});

test('Restricted accounts retain records without reconciliation controls', async ({page}) => {
  await fixture(page,'readonly');
  let paymentReads = 0;
  await page.route('**/api/v1/reconciliation/**', route => {paymentReads++; return route.fulfill({json:[]});});
  await page.goto('/transactions');
  await expect(page.getByRole('navigation',{name:'Transaction sections'}).getByRole('link')).toHaveCount(1);
  await expect(page.getByRole('heading',{name:'Orders',exact:true})).toBeVisible();
  await page.goto('/reconciliation');
  await expect(page.getByRole('status').filter({hasText:'Payment matching requires'})).toBeVisible();
  await expect(page.getByRole('button',{name:'Run Reconciliation'})).toHaveCount(0);
  expect(paymentReads).toBe(0);
  await page.goto('/tables/orders?view=approvals');
  await expect(page.getByText(/This workspace needs Celigo and reconciliation enabled/)).toBeVisible();
});

test('MCP partial-result downloads expose REST errors and download loaded CSV and Excel', async ({page}, testInfo) => {
  const {readFile} = await import('node:fs/promises');
  const {join} = await import('node:path');
  const state = await fixture(page);
  const rows = [[1, 'North, "branch"'], [2, 'South\nbranch']];
  state.messages.push({id:'export-result',role:'assistant',content:'Partial query results.',created_at:now,citations:null,tool_calls:[{
    tool:'netsuite_suiteql',params:{query:'SELECT id, name FROM customer'},result_summary:'100 rows; partial response',duration_ms:5,
    result_payload:{kind:'table',columns:['id','name'],rows,row_count:100,truncated:true,query:'SELECT id, name FROM customer',limit:2},
  }]});
  let fullRequests = 0;
  let excelRequest: unknown;
  const xlsx = await readFile(join(testInfo.project.testDir,'fixtures/export-loaded-rows.xlsx'));
  await page.route('**/api/v1/exports/query-export', route => {fullRequests++; return route.fulfill({status:400,json:{detail:'No active NetSuite connection. Connect your NetSuite account first.'}});});
  await page.route('**/api/v1/exports/excel', route => {excelRequest=route.request().postDataJSON();return route.fulfill({status:200,contentType:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',body:xlsx});});
  await page.goto('/chat');
  await page.getByText('Read-only investigation',{exact:true}).click();
  await page.getByRole('button',{name:'Export CSV',exact:true}).click();
  await expect(page.getByRole('alert').filter({hasText:'No active NetSuite connection'})).toBeVisible();
  await page.getByRole('button',{name:'Export Excel',exact:true}).click();
  await expect.poll(()=>fullRequests).toBe(2);
  await expect(page.getByText(/2 rows already loaded.*partial result/)).toBeVisible();
  const csvEvent=page.waitForEvent('download');
  await page.getByRole('button',{name:'Download loaded rows as CSV',exact:true}).click();
  const csv=await csvEvent;
  expect(csv.suggestedFilename()).toContain('loaded-rows');
  const csvPath=testInfo.outputPath('loaded-rows.csv');await csv.saveAs(csvPath);
  expect(await readFile(csvPath,'utf8')).toBe('id,name\r\n1,"North, ""branch"""\r\n2,"South\nbranch"');
  const excelEvent=page.waitForEvent('download');
  await page.getByRole('button',{name:'Download loaded rows as Excel',exact:true}).click();
  const excel=await excelEvent;
  expect(excel.suggestedFilename()).toMatch(/loaded-rows.*\.xlsx$/);
  const excelPath=testInfo.outputPath('loaded-rows.xlsx');await excel.saveAs(excelPath);
  await expectStyledWorkbook(await readFile(excelPath), xlsx);
  expect(excelRequest).toMatchObject({columns:['id','name'],rows,title:expect.stringContaining('loaded-rows')});
  expect(fullRequests).toBe(2);
  await testInfo.attach('loaded-rows.csv',{path:csvPath,contentType:'text/csv'});
  await testInfo.attach('loaded-rows.xlsx',{path:excelPath,contentType:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'});
  await screenshot(page,'chat-export-loaded-rows');
});

test('persisted MCP Chat table downloads CSV and Excel without a REST query', async ({page}, testInfo) => {
  const {readFile} = await import('node:fs/promises');
  const {join} = await import('node:path');
  const state = await fixture(page);
  const rows = [[1, 'North, "branch"'], [2, 'South\nbranch']];
  state.messages.push({id:'persisted-table',role:'assistant',content:'Latest records.',created_at:now,citations:null,tool_calls:[],structured_output:{type:'data_table',data:{columns:['id','name'],rows,row_count:2,truncated:false,query:'SELECT id, name FROM customer FETCH FIRST 2 ROWS ONLY'}}});
  let fullRequests = 0;
  let excelRequest: unknown;
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const xlsx = await readFile(join(testInfo.project.testDir,'fixtures/export-loaded-rows.xlsx'));
  await page.route('**/api/v1/exports/query-export', route => {fullRequests++; return route.fulfill({status:400,json:{detail:'No active NetSuite connection. Connect your NetSuite account first.'}});});
  await page.route('**/api/v1/exports/excel', route => {excelRequest=route.request().postDataJSON();return route.fulfill({status:200,contentType:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',body:xlsx});});
  await page.goto('/chat');
  await page.getByText('Read-only investigation',{exact:true}).click();
  const csvEvent = page.waitForEvent('download');
  await page.getByRole('button',{name:'CSV',exact:true}).click();
  const csv = await csvEvent;
  const csvPath = testInfo.outputPath('persisted-table.csv'); await csv.saveAs(csvPath);
  expect(await readFile(csvPath,'utf8')).toBe('id,name\r\n1,"North, ""branch"""\r\n2,"South\nbranch"');
  const excelEvent = page.waitForEvent('download');
  await page.getByRole('button',{name:'Excel',exact:true}).click();
  const excel = await excelEvent;
  const excelPath = testInfo.outputPath('persisted-table.xlsx'); await excel.saveAs(excelPath);
  await expectStyledWorkbook(await readFile(excelPath), xlsx);
  expect(excelRequest).toMatchObject({columns:['id','name'],rows});
  expect(fullRequests).toBe(0);
  expect(errors).toEqual([]);
  await screenshot(page,'chat-data-frame-downloads');
  await page.setViewportSize({width:390,height:844});
  await expect(page.getByRole('button',{name:'Excel',exact:true})).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await screenshot(page,'chat-data-frame-downloads-mobile');
});

for (const entry of ["/settings", "/connections"]) {
test(`Company profile deep link selects Workspace from Agent and supports history at ${entry}`, async ({page}) => {
  await fixture(page);
  await page.goto(`${entry}#agent`);
  const sections = page.getByRole('navigation', {name:'Settings sections'});
  await expect(sections.getByRole('link', {name:/Agent Models/})).toHaveAttribute('aria-current','location');
  await page.getByRole('link', {name:'Company profile',exact:true}).click();
  await expect(page).toHaveURL(/#workspace$/);
  await expect(sections.getByRole('link', {name:/Workspace Profile/})).toHaveAttribute('aria-current','location');
  await expect(page.getByRole('heading', {name:'Company profile',exact:true})).toBeVisible();
  await page.goBack();
  await expect(sections.getByRole('link', {name:/Agent Models/})).toHaveAttribute('aria-current','location');
  await page.goForward();
  await expect(page.getByRole('heading', {name:'Company profile',exact:true})).toBeVisible();
});
}


test("Company instructions stay in one editor without navigation writes", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/settings#agent");
  await expect(page.getByRole("heading", { name: "Company instructions", exact: true })).toHaveCount(1);
  await page.getByLabel("Agent tone", { exact: true }).fill("Synthetic unsaved tone");
  await page.getByLabel("Agent tone", { exact: true }).press("Tab");
  await expect(page.getByLabel("NetSuite guidance", { exact: true })).toBeFocused();
  await page.getByLabel("NetSuite guidance", { exact: true }).fill("Synthetic unsaved guidance");
  const sections = page.getByRole("navigation", { name: "Settings sections" });
  await sections.getByRole("link", { name: /^Advanced/ }).click();
  await expect(page.getByRole("heading", { name: "Activity and maintenance" })).toBeVisible();
  await page.goBack();
  await expect(page.getByLabel("Agent tone", { exact: true })).toHaveValue("Synthetic unsaved tone");
  await expect(page.getByLabel("NetSuite guidance", { exact: true })).toHaveValue("Synthetic unsaved guidance");
  expect(state.writes).toHaveLength(0);
  await page.getByRole("heading", { name: "Company instructions", exact: true }).scrollIntoViewIfNeeded();
  await screenshot(page, "instructions-desktop");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByLabel("Agent tone", { exact: true }).scrollIntoViewIfNeeded();
  await screenshot(page, "instructions-mobile");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("Viewer Settings keeps five groups and hides instruction and policy editors", async ({ page }) => {
  const state = await fixture(page, "readonly");
  await page.goto("/settings#agent");
  const sections = page.getByRole("navigation", { name: "Settings sections" });
  await expect(sections.getByRole("link")).toHaveCount(5);
  await expect(page.getByText("An administrator manages agent configuration and approval policy.")).toBeVisible();
  await expect(page.getByLabel("Agent tone", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Save instructions", exact: true })).toHaveCount(0);
  await sections.getByRole("link", { name: /^Team & access/ }).click();
  await expect(page.getByText(/Contact your administrator to manage team members/)).toBeVisible();
  expect(state.writes).toHaveLength(0);
});

test("FW008 mixed method health, exact setup, dependency warning and narrow screen", async ({ page }) => {
  const state = await fixture(page);
  const apiId = "11111111-1111-4111-8111-111111111111", mcpId = "22222222-2222-4222-8222-222222222222";
  await page.route("**/api/v1/connections", route => route.fulfill({ json: [{ id: apiId, provider: "netsuite", label: "Synthetic ERP API", status: "active", auth_type: "oauth2", metadata_json: { client_id: "api-client" } }] }));
  await page.route("**/api/v1/mcp-connectors", route => route.fulfill({ json: [{ id: mcpId, provider: "netsuite_mcp", label: "Synthetic ERP MCP", status: "error", auth_type: "oauth2", server_url: "https://sandbox.example/mcp", metadata_json: { client_id: "mcp-client" }, is_enabled: true }] }));
  await page.route("**/api/v1/connections/health", route => route.fulfill({ json: {
    connections: [{ id: apiId, status: "active", verification_status: "ok", account_identity: "SYNTHETIC-SB1", access_scope: "rest_webservices", role: "Reader", client_id: "api-client", last_health_check: "2026-09-01T12:00:00Z" }],
    mcp_connectors: [{ id: mcpId, status: "needs_reauth", client_id: "mcp-client", error_reason: "Authorization expired. Reconnect this method.", last_health_check: null }],
  } }));
  await page.route("**/api/v1/connections/usage/**", route => route.fulfill({ json: { uses: [{ name: "Synthetic stock report", href: "/scheduled-jobs/report-1", binding: "exact binding", active: true }], visibility_limited: false, coverage: "Saved supported bindings only; dynamic skills choose access at run time." } }));
  await page.goto(`/settings#connection-mcp-${mcpId}`);
  const panel = page.getByRole("region", { name: "Connections settings" });
  await expect(panel.getByRole("heading", { name: "NetSuite", exact: true })).toHaveCount(1);
  await expect(panel.getByText("Verified at last test", { exact: true })).toBeVisible();
  await expect(page.locator(`#connection-mcp-${mcpId}`).getByText("Authorization expired", { exact: true })).toBeVisible();
  await expect(panel.getByText("SYNTHETIC-SB1", { exact: true })).toBeVisible();
  if (process.env.ORBITAL_EVIDENCE) await page.screenshot({ path: `${process.env.ORBITAL_EVIDENCE}/fw008-desktop.png`, animations: "disabled" });
  await page.locator(`#connection-mcp-${mcpId}`).getByRole("link", { name: "Connection setup" }).click();
  const editor = page.locator(`#connection-settings-mcp-${mcpId}`);
  await expect(editor).toBeVisible();
  await expect(editor.getByText("mcp-client", { exact: true })).toBeVisible();
  await page.goBack();
  await panel.getByRole("button", { name: "Delete Synthetic ERP API", exact: true }).click();
  await expect(page.getByRole("dialog").getByRole("link", { name: "Synthetic stock report" })).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "Cancel", exact: true }).click();
  expect(state.writes).toEqual([]);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/connections#connection-mcp-${mcpId}`);
  await expect(page.locator(`#connection-mcp-${mcpId}`).getByText("Authorization expired", { exact: true })).toBeVisible();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  if (process.env.ORBITAL_EVIDENCE) await page.screenshot({ path: `${process.env.ORBITAL_EVIDENCE}/fw008-mobile.png`, animations: "disabled" });
});
