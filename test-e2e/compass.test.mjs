import { createRequire } from "node:module";
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import assert from "node:assert/strict";

const require = createRequire(import.meta.url);
const pw = require(process.env.PLAYWRIGHT_CORE || "playwright-core");
const base = process.env.RIG || "http://127.0.0.1:8793";
const kind = process.env.SAVE_TEST_BROWSER || "webkit";
const shots = process.env.SHOTS || "";
if (shots) mkdirSync(shots, { recursive: true });
const browser = await pw[kind].launch({ executablePath: process.env.SAVE_TEST_EXE || undefined });
const VIEWS = {
  390: kind === "webkit" ? pw.devices["iPhone 15"] : { viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true },
  1280: { viewport: { width: 1280, height: 800 } },
};
const ADMIN = { user: "admin", groups: "files-admin" };
const results = [];
const test = async (name, fn) => {
  for (const width of Object.keys(VIEWS)) {
    try { await fn(Number(width)); results.push(["ok", `${name} @${width}`]); } catch (e) { results.push(["FAIL", `${name} @${width}`, e.message]); }
  }
};

async function open(width, { user, groups = "" }) {
  const headers = { "Remote-User": user, Host: "files.kelliher.info" };
  if (groups) headers["Remote-Groups"] = groups;
  const ctx = await browser.newContext({ ...VIEWS[width], extraHTTPHeaders: headers, baseURL: base });
  const page = await ctx.newPage();
  const problems = [];
  page.on("console", (m) => { if (["error", "warning"].includes(m.type())) problems.push(m.text()); });
  page.on("pageerror", (e) => problems.push(String(e)));
  return { ctx, page, problems };
}

const links = (page) => page.$$eval(".p-menu a", (a) => a.map((x) => [x.dataset.bucket, x.getAttribute("href")]));
const shot = async (page, name) => { if (shots) await page.screenshot({ path: join(shots, name + ".png") }); };

await test("the home bucket is in the nav and the menu is closed until it is asked for", async (width) => {
  const { ctx, page, problems } = await open(width, ADMIN);
  await page.goto("/users/admin/");
  const chip = page.locator(".p-buckets > a");
  assert.equal((await chip.textContent()).trim(), "Your files");
  assert.equal(await chip.getAttribute("href"), "/users/admin/");
  assert.equal(await chip.getAttribute("aria-current"), "page");
  assert.equal(await page.$eval(".p-menu", (d) => d.open), false);
  assert.equal(await page.locator(".p-menu a").first().isVisible(), false, "closed menu shows no links");
  assert.equal(await page.$$eval("script", (s) => s.length), 0);
  assert.deepEqual(problems, []);
  await shot(page, `closed-${width}`);
  await ctx.close();
});

await test("opening the menu lists every readable bucket as a link, inside the viewport", async (width) => {
  const { ctx, page } = await open(width, ADMIN);
  await page.goto("/users/admin/");
  await page.click(".p-menu summary");
  assert.deepEqual(await links(page), [
    ["home", "/users/admin/"], ["ctkelliher", "/users/ctkelliher/"], ["pilar", "/users/pilar/"], ["shared", "/"],
  ]);
  const labels = await page.$$eval(".p-menu a .nm", (e) => e.map((x) => x.textContent));
  assert.deepEqual(labels, ["Your files", "ctkelliher", "pilar", "Shared"]);
  const access = await page.$$eval(".p-menu a .acc", (e) => e.map((x) => x.textContent));
  assert.deepEqual(access, ["read, list, write", "read, list, write", "read, list, write", "read, list, write"]);
  const vp = page.viewportSize();
  for (const a of await page.locator(".p-menu a").all()) {
    const box = await a.boundingBox();
    assert.ok(box.x >= 0 && box.x + box.width <= vp.width, "link inside the viewport width: " + JSON.stringify(box));
    assert.ok(box.height >= 44, "tap target " + box.height);
  }
  const summary = await page.locator(".p-menu summary").boundingBox();
  assert.ok(summary.height >= 44 && summary.width >= 44);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, "no sideways scroll");
  await shot(page, `open-${width}`);
  await ctx.close();
});

await test("the open menu sits above the tiles and the sticky day headings", async (width) => {
  const { ctx, page } = await open(width, ADMIN);
  await page.goto("/users/ctkelliher/camera/");
  assert.ok((await page.$$eval(".p-day", (e) => e.length)) >= 1, "the folder has dated tiles");
  await page.click(".p-menu summary");
  for (const a of await page.locator(".p-menu a").all()) {
    const box = await a.boundingBox();
    const hit = await page.evaluate(([x, y]) => document.elementFromPoint(x, y).closest("a")?.dataset.bucket ?? null, [box.x + box.width / 2, box.y + box.height / 2]);
    assert.equal(hit, await a.getAttribute("data-bucket"), "the link is what a tap lands on");
  }
  await shot(page, `over-tiles-${width}`);
  await ctx.close();
});

await test("choosing a bucket opens its listing, marks it, and keeps the home chip", async (width) => {
  const { ctx, page } = await open(width, ADMIN);
  await page.goto("/users/admin/");
  await page.click(".p-menu summary");
  await page.click('.p-menu a[data-bucket="ctkelliher"]');
  await page.waitForURL("**/users/ctkelliher/");
  assert.equal(await page.textContent(".p-title h1"), "ctkelliher");
  assert.deepEqual(await page.$$eval('.p-menu a[aria-current="page"]', (e) => e.map((x) => x.dataset.bucket)), ["ctkelliher"]);
  const chip = page.locator(".p-buckets > a");
  assert.equal(await chip.getAttribute("href"), "/users/admin/");
  assert.equal(await chip.getAttribute("aria-current"), null);
  assert.equal(await page.$eval(".p-menu", (d) => d.open), false, "a new page starts closed");
  await page.click(".p-buckets > a");
  await page.waitForURL("**/users/admin/");
  await ctx.close();
});

await test("every bucket view is deep-linkable from a cold start", async (width) => {
  const { ctx, page, problems } = await open(width, ADMIN);
  const api = await (await ctx.request.get("/_files/api/buckets")).json();
  assert.equal(api.home, "home");
  for (const b of api.buckets) {
    const r = await page.goto(b.root_url);
    assert.equal(r.status(), 200, b.root_url);
    assert.deepEqual(await page.$$eval('.p-menu a[aria-current="page"]', (e) => e.map((x) => x.dataset.bucket)), [b.id], b.root_url);
  }
  const granted = await page.goto("/users/pilar/");
  assert.equal(granted.status(), 200);
  assert.deepEqual(await page.$$eval(".p-card b", (e) => e.map((x) => x.textContent)), ["camera"], "a row on one folder shows that folder only");
  assert.deepEqual(problems, []);
  await ctx.close();
});

await test("a keyboard opens the menu and follows a link", async (width) => {
  const { ctx, page } = await open(width, ADMIN);
  await page.goto("/users/admin/");
  await page.focus(".p-menu summary");
  await page.keyboard.press("Enter");
  assert.equal(await page.$eval(".p-menu", (d) => d.open), true);
  await page.focus('.p-menu a[data-bucket="shared"]');
  await Promise.all([page.waitForURL((u) => u.pathname === "/"), page.keyboard.press("Enter")]);
  assert.deepEqual(await page.$$eval('.p-menu a[aria-current="page"]', (e) => e.map((x) => x.dataset.bucket)), ["shared"]);
  await ctx.close();
});

await test("a home-only user has her home in the nav and nothing to choose from", async (width) => {
  const { ctx, page } = await open(width, { user: "pilar" });
  await page.goto("/users/pilar/");
  assert.equal((await page.textContent(".p-buckets > a")).trim(), "Your files");
  assert.equal(await page.$$eval(".p-menu", (e) => e.length), 0);
  const body = await page.textContent("body");
  for (const gone of ["ctkelliher", "Shared", "dad", "zoe"]) assert.ok(!body.includes(gone), "no " + gone);
  await shot(page, `home-only-${width}`);
  await ctx.close();
});

await test("a user with no home row defaults to the shared tree", async (width) => {
  const { ctx, page } = await open(width, { user: "dad" });
  await page.goto("/dad/");
  assert.equal((await page.textContent(".p-buckets > a")).trim(), "Shared");
  assert.equal(await page.getAttribute(".p-buckets > a", "href"), "/");
  const api = await (await ctx.request.get("/_files/api/buckets")).json();
  assert.deepEqual(api, { home: "shared", buckets: [{ id: "shared", label: "Shared", root_url: "/", access: ["read", "list"], kind: "shared" }] });
  await ctx.close();
});

await test("a row that only lets you write opens nothing, and no bucket is named for it", async (width) => {
  for (const user of ["scribe", "friend", "nobody"]) {
    const { ctx, page } = await open(width, { user });
    const api = await (await ctx.request.get("/_files/api/buckets")).json();
    assert.deepEqual(api, { home: null, buckets: [] }, user);
    for (const path of ["/", "/inbox/", "/users/zoe/", "/users/pilar/"]) assert.equal((await page.goto(path)).status(), 404, `${user} ${path}`);
    await ctx.close();
  }
});

await test("the API is no-store JSON and names no Garage bucket", async (width) => {
  const { ctx } = await open(width, ADMIN);
  const r = await ctx.request.get("/_files/api/buckets");
  assert.equal(r.headers()["cache-control"], "no-store");
  assert.match(r.headers()["content-type"], /^application\/json/);
  const text = await r.text();
  for (const owner of ["admin", "pilar", "ctkelliher", "zoe"]) assert.ok(!text.includes("u-" + owner));
  assert.equal((await ctx.request.post("/_files/api/buckets")).status(), 405);
  await ctx.close();
});

await browser.close();
for (const r of results) console.log(r.join("  "));
const failed = results.filter((r) => r[0] === "FAIL").length;
console.log(`${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
