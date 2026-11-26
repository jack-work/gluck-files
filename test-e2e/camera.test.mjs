import { createRequire } from "node:module";
import assert from "node:assert/strict";

const require = createRequire(import.meta.url);
const pw = require("playwright-core");
const base = process.env.RIG || "http://127.0.0.1:8791";
const kind = process.env.SAVE_TEST_BROWSER || "webkit";
const browser = await pw[kind].launch({ executablePath: process.env.SAVE_TEST_EXE || undefined });
const device = kind === "webkit" ? pw.devices["iPhone 15"] : { viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true };
const DIR = "/users/pilar/camera/?order=desc";
const results = [];
const numbers = {};
const test = async (name, fn) => { try { await fn(); results.push(["ok", name]); } catch (e) { results.push(["FAIL", name, e.message]); } };
const hits = async () => (await fetch(base + "/_rig/hits")).json();

async function open() {
  const ctx = await browser.newContext({ ...device, extraHTTPHeaders: { "Remote-User": "pilar", Host: "files.kelliher.info" }, baseURL: base });
  return { ctx, page: await ctx.newPage() };
}

await test("first paint: 200 tiles, thumbs only, lazy, no original in the grid", async () => {
  const { ctx, page } = await open();
  const reqs = [];
  page.on("request", (r) => reqs.push(r.url()));
  const before = await hits();
  const t0 = Date.now();
  const r = await page.goto(DIR, { waitUntil: "load" });
  numbers.first_paint_ms = Date.now() - t0;
  assert.match(r.headers()["cache-control"], /no-cache/);
  assert.equal(await page.$$eval("script", (e) => e.length), 0);
  const imgs = await page.$$eval(".p-grid img", (e) => e.map((i) => ({ src: i.getAttribute("src"), lazy: i.loading, dec: i.decoding, w: i.width, h: i.height })));
  assert.ok(imgs.length > 150, "thumb tiles " + imgs.length);
  for (const i of imgs) {
    assert.match(i.src, /^\/_thumb\/users\/pilar\/camera\/.+\?v=/);
    assert.equal(i.lazy, "lazy");
    assert.equal(i.dec, "async");
    assert.ok(i.w > 0 && i.h > 0);
  }
  assert.equal(reqs.filter((u) => /\/users\/pilar\/camera\/[^?]*$/.test(new URL(u).pathname) && !u.includes("_thumb") && u.split("?")[0].match(/\.(jpg|heic|mp4)$/i)).length, 0, "no original fetched");
  await page.waitForTimeout(500);
  const after = await hits();
  numbers.thumbs_on_first_paint = after.thumb - before.thumb;
  numbers.originals_on_first_paint = after.original - before.original;
  numbers.thumb_requests_of_200 = numbers.thumbs_on_first_paint;
  numbers.pending_requests = after.thumb_pending - before.thumb_pending;
  assert.equal(numbers.originals_on_first_paint, 0);
  assert.equal(numbers.pending_requests, 0, "a thumb:false tile makes no /_thumb request");
  assert.ok(numbers.thumbs_on_first_paint < imgs.length, "lazy: not every thumb fetched on first paint");
  numbers.html_bytes = (await (await ctx.request.get(DIR)).body()).length;
  await ctx.close();
});

await test("pending tiles are glyphs that say so, and HEIC and video tiles are marked", async () => {
  const { ctx, page } = await open();
  await page.goto(DIR);
  const bare = await page.$$eval(".p-tile.bare", (e) => e.length);
  assert.ok(bare >= 1, "some thumbs are pending in the seed");
  assert.equal(await page.$$eval(".p-tile.bare img", (e) => e.length), 0);
  assert.ok((await page.textContent(".p-tile.bare .wait")).includes("on its way"));
  assert.ok((await page.$$eval(".p-tile .heic", (e) => e.length)) > 5);
  await ctx.close();
});

await test("paging walks all 2000 once, newest first, dated before undated, headings continue", async () => {
  const { ctx, page } = await open();
  let url = DIR, names = [], pages = 0, conts = 0, last = null;
  const t0 = Date.now();
  while (url) {
    await page.goto(url);
    pages++;
    const got = await page.$$eval(".p-tile a", (e) => e.map((a) => (a.querySelector("img") ? a.querySelector("img").alt : a.querySelector(".glyph span:not(.wait)").textContent)));
    names.push(...got);
    conts += await page.$$eval(".p-day span:first-child", (e) => e.filter((x) => x.textContent.includes("(continued)")).length);
    const next = await page.$(".p-more a.p-btn:not(.ghost)");
    url = next ? await next.getAttribute("href") : null;
    if (pages > 20) throw new Error("does not end");
  }
  numbers.pages = pages;
  numbers.page_walk_ms = Date.now() - t0;
  assert.equal(names.length, 2000);
  assert.equal(new Set(names).size, 2000);
  const dated = names.filter((n) => /^\d{8}_/.test(n));
  const undated = names.slice(dated.length);
  assert.equal(undated.length, 10);
  assert.ok(undated.every((n) => !/^\d{8}_/.test(n)), "undated names are last");
  for (let i = 1; i < dated.length; i++) assert.ok(dated[i - 1] >= dated[i], dated[i - 1] + " before " + dated[i]);
  assert.ok(conts >= 1, "a heading split across pages repeats with (continued)");
  assert.equal(pages, 10);
  numbers.continued_headings = conts;
  await ctx.close();
});

await test("the first page says N of total, and the limit is capped at 500", async () => {
  const { ctx, page } = await open();
  await page.goto(DIR);
  assert.match(await page.textContent(".p-more span"), /200 shown of 2000/);
  assert.match(await page.textContent(".p-more a.p-btn"), /Show next 200/);
  await page.goto(DIR + "&limit=500");
  assert.equal(await page.$$eval(".p-tile", (e) => e.length), 500);
  await page.goto(DIR + "&limit=99999");
  assert.equal(await page.$$eval(".p-tile", (e) => e.length), 500, "limit is capped at 500");
  await ctx.close();
});

await test("select on a later page keeps its place", async () => {
  const { ctx, page } = await open();
  await page.goto(DIR);
  const next = await (await page.$(".p-more a.p-btn:not(.ghost)")).getAttribute("href");
  await page.goto(next + "&select=1");
  const first = await page.getAttribute(".p-tile > a >> nth=0", "href");
  assert.match(first, /after=/);
  assert.match(first, /select=1/);
  assert.match(first, /pick=/);
  await ctx.close();
});

await browser.close();
for (const r of results) console.log(r.join("  "));
console.log("numbers", JSON.stringify(numbers));
process.exit(results.some((r) => r[0] === "FAIL") ? 1 : 0);
