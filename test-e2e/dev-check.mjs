import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const pw = require("playwright-core");
const base = process.env.DEV || "http://127.0.0.1:18791";
const dir = process.env.DIR || "/users/admin/camera/?order=desc";
const browser = await pw.webkit.launch();
const ctx = await browser.newContext({ ...pw.devices["iPhone 15"], baseURL: base });
const page = await ctx.newPage();
const stat = { thumbs: 0, thumbBytes: 0, statuses: {}, originals: 0, pending404: 0, other: 0 };
page.on("response", async (r) => {
  const u = new URL(r.url());
  if (u.pathname.startsWith("/_thumb/")) {
    stat.thumbs++;
    stat.statuses[r.status()] = (stat.statuses[r.status()] || 0) + 1;
    if (r.status() === 404 && r.headers()["x-thumb"] === "pending") stat.pending404++;
    const l = Number(r.headers()["content-length"] || 0); stat.thumbBytes += l;
  } else if (/\.(jpe?g|heic|mp4)$/i.test(u.pathname)) stat.originals++;
});
let url = dir, pages = 0, tiles = 0, bare = 0, heic = 0, video = 0, broken = 0, cont = 0;
const names = new Set(); const firstPaint = [];
while (url) {
  const t0 = Date.now();
  await page.goto(url, { waitUntil: "load" });
  if (pages === 0) { firstPaint.push(Date.now() - t0); await page.waitForTimeout(800); await page.screenshot({ path: "/tmp/dev-p1.png" }); }
  // scroll to the bottom in steps so lazy images load
  const h = await page.evaluate(() => document.body.scrollHeight);
  for (let y = 0; y < h; y += 700) { await page.evaluate((v) => window.scrollTo(0, v), y); await page.waitForTimeout(60); }
  await page.waitForTimeout(400);
  const t = await page.evaluate(() => ({
    n: document.querySelectorAll(".p-tile").length,
    bare: document.querySelectorAll(".p-tile.bare").length,
    heic: document.querySelectorAll(".p-tile .heic").length,
    video: document.querySelectorAll(".p-tile.video").length,
    broken: [...document.querySelectorAll(".p-tile img")].filter((i) => i.complete && i.naturalWidth === 0).length,
    cont: [...document.querySelectorAll(".p-day span:first-child")].filter((x) => x.textContent.includes("(continued)")).length,
    names: [...document.querySelectorAll(".p-tile a")].map((a) => (a.querySelector("img") ? a.querySelector("img").alt : (a.querySelector(".glyph span") || {}).textContent)),
  }));
  tiles += t.n; bare += t.bare; heic += t.heic; video += t.video; broken += t.broken; cont += t.cont; t.names.forEach((n) => names.add(n));
  pages++;
  const next = await page.$(".p-more a.p-btn:not(.ghost)");
  url = next ? await next.getAttribute("href") : null;
  if (pages > 15) break;
}
console.log(JSON.stringify({ pages, tiles, unique: names.size, bare_pending_tiles: bare, heic_tiles: heic, video_tiles: video, broken_images: broken, continued_headings: cont, first_paint_ms: firstPaint[0], ...stat }));
await browser.close();
