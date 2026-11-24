"use strict";
const $ = (id) => document.getElementById(id);
const PARALLEL = 3;
let partBytes = 16 * 1024 * 1024;
let queue = [];

async function api(method, path, body, raw) {
  const opts = { method, credentials: "same-origin", headers: {} };
  if (raw !== undefined) { opts.body = raw; opts.headers["Content-Type"] = "application/octet-stream"; }
  else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, opts);
  let data = {};
  try { data = await r.json(); } catch (e) { data = {}; }
  if (!r.ok) { const err = new Error(data.error || ("HTTP " + r.status)); err.status = r.status; throw err; }
  return data;
}

const sleep = (ms) => new Promise((res) => setTimeout(res, ms));
const mb = (n) => (n / 1048576).toFixed(n < 10485760 ? 1 : 0) + " MB";
const memoKey = (f) => "upload:" + f.name + "|" + f.size + "|" + f.lastModified;

function row(file) {
  const li = document.createElement("li");
  li.innerHTML = '<div class="name"></div><div class="state">Waiting</div><div class="bar"><span></span></div>';
  li.querySelector(".name").textContent = file.name;
  $("files").appendChild(li);
  return {
    li,
    state(text, cls) { li.querySelector(".state").textContent = text; li.className = cls || ""; },
    bar(frac) { li.querySelector(".bar > span").style.width = Math.round(frac * 100) + "%"; },
  };
}

async function begin(file) {
  const memo = localStorage.getItem(memoKey(file));
  if (memo) {
    const up = JSON.parse(memo);
    try {
      const got = await api("GET", "/_upload/api/parts?key=" + encodeURIComponent(up.key) + "&id=" + encodeURIComponent(up.id));
      return { up, done: new Set(got.parts.filter((p) => p.size > 0).map((p) => p.n)) };
    } catch (e) { localStorage.removeItem(memoKey(file)); }
  }
  const up = await api("POST", "/_upload/api/start", { name: file.name, size: file.size });
  partBytes = up.partBytes || partBytes;
  localStorage.setItem(memoKey(file), JSON.stringify({ key: up.key, id: up.id }));
  return { up, done: new Set() };
}

async function sendPart(file, up, n) {
  const blob = file.slice((n - 1) * partBytes, Math.min(n * partBytes, file.size));
  for (let attempt = 0; ; attempt++) {
    try {
      await api("PUT", "/_upload/api/part?key=" + encodeURIComponent(up.key) + "&id=" + encodeURIComponent(up.id) + "&n=" + n, undefined, blob);
      return;
    } catch (e) {
      if (attempt >= 5 || (e.status && e.status < 500 && e.status !== 408 && e.status !== 429)) throw e;
      await sleep(1000 * 2 ** attempt);
    }
  }
}

async function upload(item) {
  const { file, ui } = item;
  ui.state("Starting", "busy");
  const { up, done } = await begin(file);
  const total = Math.max(1, Math.ceil(file.size / partBytes));
  const todo = [];
  for (let n = 1; n <= total; n++) if (!done.has(n)) todo.push(n);
  let finished = total - todo.length;
  const show = () => { ui.bar(finished / total); ui.state("Uploading " + mb(Math.min(file.size, finished * partBytes)) + " of " + mb(file.size), "busy"); };
  show();
  const workers = Array.from({ length: PARALLEL }, async () => {
    while (todo.length) { const n = todo.shift(); await sendPart(file, up, n); finished++; show(); }
  });
  await Promise.all(workers);
  ui.state("Finishing", "busy");
  await api("POST", "/_upload/api/complete", { key: up.key, id: up.id, size: file.size });
  localStorage.removeItem(memoKey(file));
  ui.bar(1);
  ui.state("Done: saved as " + up.key.split("/").pop(), "done");
  item.done = true;
}

async function run() {
  $("retry").hidden = true;
  $("pick").disabled = true;
  let failed = 0;
  for (const item of queue) {
    if (item.done) continue;
    try { await upload(item); }
    catch (e) {
      failed++;
      item.ui.state((e.status === 403 || e.status === 415 ? "Not uploaded: " : "Stopped: ") + e.message + (e.status === 403 || e.status === 415 ? "" : ". Tap Retry."), "failed");
    }
  }
  $("pick").disabled = false;
  const ok = queue.filter((i) => i.done).length;
  if (failed) {
    $("summary").textContent = ok + " of " + queue.length + " files are saved. " + failed + " stopped.";
    $("retry").hidden = false;
  } else {
    $("summary").textContent = "Done: all " + queue.length + " files are saved in your folder.";
  }
}

$("pick").addEventListener("change", () => {
  const files = Array.from($("pick").files || []);
  if (!files.length) return;
  queue = queue.filter((i) => !i.done).concat(files.map((file) => ({ file, ui: row(file), done: false })));
  $("summary").textContent = "";
  run();
});
$("retry").addEventListener("click", run);
window.addEventListener("beforeunload", (e) => { if (queue.some((i) => !i.done) && $("pick").disabled) { e.preventDefault(); e.returnValue = ""; } });

(async () => {
  try {
    const me = await api("GET", "/_upload/api/me");
    partBytes = me.partBytes || partBytes;
    $("who").textContent = me.user;
    $("lede").textContent = me.writable ? "Files go to your folder." : "This account can't upload yet. Ask whoever runs this server to turn it on.";
    $("folder").href = "/" + encodeURI(me.folder) + "uploads/";
    $("folder").hidden = false;
    $("pickLabel").hidden = !me.writable;
    $("hint").hidden = !me.writable;
  } catch (e) {
    $("who").textContent = "signed out";
    $("lede").textContent = e.status === 401 ? "Sign in first, then open this page again." : "Can't reach the server: " + e.message;
  }
})();
