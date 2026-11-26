import { createRequire } from "node:module";
import { execFileSync } from "node:child_process";
import { writeFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import assert from "node:assert/strict";

const require = createRequire(import.meta.url);
const pw = require(process.env.PLAYWRIGHT_CORE || "playwright-core");
const kind = process.env.SAVE_TEST_BROWSER || "webkit";
const base = process.env.RIG || "http://127.0.0.1:8791";
const launch = { executablePath: process.env.SAVE_TEST_EXE || undefined };
const device = kind === "webkit" ? pw.devices["iPhone 15"] : { viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true };

const STUB = `
(() => {
  const hex = (b) => [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, "0")).join("");
  window.__shares = [];
  let tap = -1e9;
  addEventListener("click", (e) => { if (e.isTrusted) tap = performance.now(); }, true);
  navigator.canShare = (d) => !!(d && d.files && d.files.length && [...d.files].every((f) => f.type));
  navigator.share = async (d) => {
    if (performance.now() - tap > 300) throw new DOMException("share needs a tap", "NotAllowedError");
    const rec = [];
    for (const f of d.files) rec.push({ name: f.name, type: f.type, size: f.size, sha: hex(await crypto.subtle.digest("SHA-256", await f.arrayBuffer())) });
    window.__shares.push(rec);
  };
})();`;

const results = [];
const test = async (name, fn) => { try { await fn(); results.push(["ok", name]); } catch (e) { results.push(["FAIL", name, e.message]); } };

const browser = await pw[kind].launch(launch);
const objects = await (await fetch(base + "/_rig/objects")).json();

async function open({ share, user = "pilar" }) {
  const ctx = await browser.newContext({ ...device, extraHTTPHeaders: user ? { "Remote-User": user, Host: "files.kelliher.info" } : {}, baseURL: base });
  if (share) await ctx.addInitScript(STUB);
  const page = await ctx.newPage();
  const problems = [];
  page.on("console", (m) => { if (["error", "warning"].includes(m.type())) problems.push(m.text()); });
  page.on("pageerror", (e) => problems.push(String(e)));
  await page.goto("/_save/?root=photos/");
  return { ctx, page, problems };
}

await test("with share: pick a month, fetch the batch, one tap hands the exact bytes to the share sheet", async () => {
  const { ctx, page, problems } = await open({ share: true });
  await page.waitForSelector("details.month");
  assert.match(await page.textContent("#lede"), /photo and video files/);
  const months = await page.$$eval("details.month .mname", (e) => e.map((x) => x.textContent));
  assert.deepEqual(months, ["November 2026", "June 2025", "June 2024", "May 2024", "January 2023"]);
  await page.click('details.month[data-month="2024-06"] > summary');
  const parts = await page.$$eval('details.month[data-month="2024-06"] .part .pmeta', (e) => e.map((x) => x.textContent));
  assert.ok(parts.length >= 2, "the 6 MB cap splits June into several parts: " + parts);
  await page.click('details.month[data-month="2024-06"] .part button >> nth=0');
  await page.waitForSelector("#jobSave:not([hidden])", { timeout: 20000 });
  assert.equal(await page.$eval("#jobBar", (e) => e.style.width), "100%");
  assert.match(await page.textContent("#jobSave"), /^Save \d+ files? to Photos or Files$/);
  const asked = await page.evaluate(() => new Promise((res) => setTimeout(() => navigator.share({ files: [new File(["x"], "a.jpg", { type: "image/jpeg" })] }).then(() => res("allowed"), (e) => res(e.name)), 1500)));
  assert.equal(asked, "NotAllowedError", "the stub refuses a share that is not made from the tap, like iOS");
  await page.click("#jobSave");
  await page.waitForFunction(() => window.__shares.length === 1);
  const sent = await page.evaluate(() => window.__shares[0]);
  assert.ok(sent.length >= 3);
  const listed = await (await ctx.request.get("/_save/api/list?root=photos/")).json();
  const june = listed.months.find((m) => m.month === "2024-06").batches[0].files.map((f) => f.rel.split("/").pop());
  assert.deepEqual(sent.map((s) => s.name), june);
  for (const s of sent) {
    const rel = "DCIM/Camera/" + s.name;
    assert.equal(s.sha, objects[rel], "bytes untouched: " + s.name);
    assert.equal(s.type, s.name.endsWith(".heic") ? "image/heic" : s.name.endsWith(".mp4") ? "video/mp4" : "image/jpeg");
  }
  assert.match(await page.textContent('details.month[data-month="2024-06"] .part.done .pmeta'), /sent to your phone/);
  assert.deepEqual(problems, [], "no console errors, so the page runs under script-src 'self'");
  await ctx.close();
});

await test("with share: the same filename in two folders is kept apart", async () => {
  const { ctx, page } = await open({ share: true });
  await page.waitForSelector("details.month");
  await page.click('details.month[data-month="2024-05"] > summary');
  await page.click('details.month[data-month="2024-05"] .part button >> nth=0');
  await page.waitForSelector("#jobSave:not([hidden])", { timeout: 20000 });
  await page.click("#jobSave");
  await page.waitForFunction(() => window.__shares.length === 1);
  const names = await page.evaluate(() => window.__shares[0].map((s) => s.name));
  assert.equal(new Set(names).size, names.length);
  assert.ok(names.includes("DCIM - Camera - 20240512_101010.jpg") || names.includes("20240512_101010.jpg"));
  await ctx.close();
});

await test("cancelling the share sheet keeps the batch ready", async () => {
  const { ctx, page } = await open({ share: true });
  await page.addInitScript(() => { navigator.share = async () => { throw new DOMException("cancelled", "AbortError"); }; });
  await page.reload();
  await page.waitForSelector("details.month");
  await page.click('details.month[data-month="2023-01"] > summary');
  await page.click('details.month[data-month="2023-01"] .part button');
  await page.waitForSelector("#jobSave:not([hidden])", { timeout: 20000 });
  await page.click("#jobSave");
  await page.waitForFunction(() => /Cancelled/.test(document.getElementById("jobState").textContent));
  assert.equal(await page.isVisible("#jobSave"), true);
  await ctx.close();
});

await test("without share: parts come as zips, and a zip holds the exact bytes", async () => {
  const { ctx, page, problems } = await open({ share: false });
  await page.waitForSelector("details.month");
  assert.match(await page.textContent("#lede"), /zip/);
  await page.click('details.month[data-month="2024-05"] > summary');
  const href = await page.getAttribute('details.month[data-month="2024-05"] .part a.folder >> nth=0', "href");
  assert.match(href, /\/_save\/api\/zip\?root=photos%2F&month=2024-05&batch=1/);
  const r = await ctx.request.get(href);
  assert.equal(r.status(), 200);
  assert.match(r.headers()["content-disposition"], /photos-2024-05-part-1\.zip/);
  const f = join(mkdtempSync(join(tmpdir(), "zip-")), "p.zip");
  writeFileSync(f, await r.body());
  const out = execFileSync("python3", ["-c", `
import zipfile, hashlib, json, sys
z = zipfile.ZipFile(sys.argv[1]); assert z.testzip() is None
print(json.dumps({n: hashlib.sha256(z.read(n)).hexdigest() for n in z.namelist()}))`, f]).toString();
  const got = JSON.parse(out);
  assert.ok(Object.keys(got).length >= 20);
  for (const [n, sha] of Object.entries(got)) assert.equal(sha, objects[n], n);
  assert.deepEqual(problems, []);
  await ctx.close();
});

await test("signed out: the page says to sign in and lists nothing", async () => {
  const ctx = await browser.newContext({ ...device, baseURL: base });
  const page = await ctx.newPage();
  const r = await page.goto("/_save/?root=photos/");
  assert.equal(r.status(), 401);
  await ctx.close();
});

await test("someone else's account sees no photos", async () => {
  const { ctx, page } = await open({ share: true, user: "dadxx" });
  await page.waitForFunction(() => /no photos folder/.test(document.getElementById("lede").textContent));
  assert.equal(await page.$$eval("details.month", (e) => e.length), 0);
  await ctx.close();
});

const LIST = "/users/pilar/photos/DCIM/Camera/";

await test("provino listing: date-headed tiles, no script allowed, links to the save page", async () => {
  const { ctx, page, problems } = await open({ share: true });
  const r = await page.goto(LIST);
  const csp = r.headers()["content-security-policy"];
  assert.match(csp, /default-src 'none'/);
  assert.doesNotMatch(csp, /script-src/);
  assert.match(csp, /form-action 'none'/);
  assert.equal(await page.$$eval("script", (e) => e.length), 0);
  const heads = await page.$$eval(".p-day span:first-child", (e) => e.map((x) => x.textContent));
  assert.ok(heads.length >= 3 && heads.every((h) => /\d{4}$|^No date/.test(h)), heads.join("|"));
  assert.ok((await page.$$eval(".p-tile", (e) => e.length)) >= 50);
  assert.equal(await page.$$eval(".p-tile img[alt^='.']", (e) => e.length), 0, "hidden files are not shown");
  assert.equal(await page.getAttribute(".p-actions a.p-btn:not(.ghost)", "href"), "/_save/?root=photos%2FDCIM%2FCamera%2F");
  assert.equal(await page.getAttribute(".p-mast a.p-btn", "href"), "/_upload/");
  const day = await page.getAttribute(".p-day .save >> nth=0", "href");
  assert.match(day, /^\/_save\/\?root=photos%2FDCIM%2FCamera%2F&month=\d{4}-\d\d$/);
  await ctx.close();
});

await test("Save day opens the save page with that month expanded and a way back", async () => {
  const { ctx, page } = await open({ share: true });
  await page.goto(LIST);
  const href = await page.getAttribute(".p-day .save >> nth=1", "href");
  const month = new URL(href, "http://x").searchParams.get("month");
  await page.click(".p-day .save >> nth=1");
  await page.waitForSelector("details.month[open]");
  assert.equal(await page.$eval("details.month[open]", (e) => e.dataset.month), month);
  assert.equal(await page.textContent("#back"), "Back to Camera");
  await page.click("#back");
  await page.waitForSelector(".p-grid");
  assert.match(page.url(), /\/users\/pilar\/photos\/DCIM\/Camera\/$/);
  await ctx.close();
});

await test("select view: tiles toggle by link, then only the picked files are saved", async () => {
  const { ctx, page, problems } = await open({ share: true });
  await page.goto(LIST);
  await page.click("a.p-btn.ghost");
  await page.waitForSelector(".p-dock");
  assert.equal(await page.$$eval("form", (e) => e.length), 0, "no form, so form-action 'none' is fine");
  for (let i = 0; i < 3; i++) await page.click(".p-tile:not(.picked) > a >> nth=" + i);
  assert.equal(await page.$$eval(".p-tile.picked", (e) => e.length), 3);
  const names = await page.$$eval(".p-tile.picked a", (e) => e.map((a) => (a.querySelector("img") ? a.querySelector("img").alt : a.querySelector(".glyph span").textContent)));
  await page.click("text=Save 3 to iPhone");
  await page.waitForSelector("details.month");
  assert.equal(await page.textContent("#title"), "Save 3 picked");
  const shown = await page.$$eval("details.month .mmeta", (e) => e.map((x) => x.textContent).join(" "));
  assert.match(shown, /\b[123] files?\b/);
  await page.evaluate(() => document.querySelectorAll("details.month").forEach((d) => (d.open = true)));
  await page.click("details.month .part button >> nth=0");
  await page.waitForSelector("#jobSave:not([hidden])", { timeout: 20000 });
  await page.click("#jobSave");
  await page.waitForFunction(() => window.__shares.length === 1);
  const sent = await page.evaluate(() => window.__shares[0].map((x) => x.name));
  assert.ok(sent.every((n) => names.includes(n)), sent + " vs " + names);
  assert.deepEqual(problems, []);
  await ctx.close();
});

await test("the viewer shows a photo and steps to the next one", async () => {
  const { ctx, page } = await open({ share: true });
  await page.goto(LIST);
  const first = await page.getAttribute(".p-tile.image > a >> nth=0", "href");
  assert.match(first, /\/_files\/view\?uri=/);
  await page.goto(first);
  await page.waitForSelector(".stage img", { state: "attached" });
  assert.ok(await page.$(".arrow.r"), "a next arrow");
  assert.equal(await page.$$eval("script", (e) => e.length), 0);
  await ctx.close();
});

await test("someone else's folder gets no save links", async () => {
  const { ctx, page } = await open({ share: true, user: "dadxx" });
  const r = await page.goto(LIST);
  assert.equal(r.status(), 404);
  await ctx.close();
});

await test("the upload page wears the dark skin and still loads", async () => {
  const { ctx, page, problems } = await open({ share: false });
  await page.goto("/_upload/");
  assert.equal(await page.$eval("body", (e) => getComputedStyle(e).colorScheme), "dark");
  await ctx.close();
});

await browser.close();
for (const r of results) console.log(r.join("  "));
console.log(`${kind}: ${results.filter((r) => r[0] === "ok").length}/${results.length} passed`);
process.exit(results.some((r) => r[0] === "FAIL") ? 1 : 0);
