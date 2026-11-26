"use strict";
const $ = (id) => document.getElementById(id);
const PARALLEL = 3;
const TYPES = {
  jpg: "image/jpeg", jpeg: "image/jpeg", png: "image/png", heic: "image/heic", heif: "image/heif", webp: "image/webp",
  gif: "image/gif", mp4: "video/mp4", mov: "video/quicktime", m4v: "video/x-m4v", "3gp": "video/3gpp", pdf: "application/pdf",
};
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const mb = (n) => (n >= 1073741824 ? (n / 1073741824).toFixed(1) + " GB" : (n / 1048576).toFixed(n < 10485760 ? 1 : 0) + " MB");
const plural = (n, w) => n + " " + w + (n === 1 ? "" : "s");
let data = null;
let job = null;
let wake = null;

function canShareFiles(files) {
  return typeof navigator.share === "function" && typeof navigator.canShare === "function" && navigator.canShare({ files });
}

function sharing() {
  try { return canShareFiles([new File([new Uint8Array(1)], "a.jpg", { type: "image/jpeg" })]); } catch (e) { return false; }
}

function monthLabel(m) {
  if (m === "undated") return "No date in the name";
  const [y, n] = m.split("-");
  return MONTHS[Number(n) - 1] + " " + y;
}

const doneKey = (month, b) => "saved:" + data.root + ":" + month + ":" + b.n + ":" + b.files.length + ":" + b.bytes;
const isDone = (month, b) => localStorage.getItem(doneKey(month, b)) === "1";
const zipHref = (month, b) => "/_save/api/zip?root=" + encodeURIComponent(data.root) + "&month=" + encodeURIComponent(month) + "&batch=" + b.n;

function render() {
  const root = $("months");
  root.textContent = "";
  for (const m of data.months) {
    const done = m.batches.filter((b) => isDone(m.month, b)).length;
    const d = document.createElement("details");
    d.className = "month";
    d.dataset.month = m.month;
    const s = document.createElement("summary");
    s.innerHTML = '<span class="mname"></span><span class="mdone"></span><span class="mmeta"></span>';
    s.querySelector(".mname").textContent = monthLabel(m.month);
    s.querySelector(".mdone").textContent = done === m.batches.length ? "All saved" : done ? done + " of " + m.batches.length + " saved" : "";
    s.querySelector(".mmeta").textContent = plural(m.files, "file") + " · " + mb(m.bytes) + " · " + plural(m.batches.length, "part");
    d.appendChild(s);
    const parts = document.createElement("div");
    parts.className = "parts";
    for (const b of m.batches) {
      const p = document.createElement("div");
      p.className = "part" + (isDone(m.month, b) ? " done" : "");
      p.innerHTML = '<div><div class="pname"></div><div class="pmeta"></div></div>';
      p.querySelector(".pname").textContent = "Part " + b.n + " of " + m.batches.length;
      p.querySelector(".pmeta").textContent = plural(b.files.length, "file") + " · " + mb(b.bytes) + (isDone(m.month, b) ? " · sent to your phone" : "");
      let act;
      if (data.shareable) {
        act = document.createElement("button");
        act.type = "button";
        act.textContent = isDone(m.month, b) ? "Again" : "Get";
        act.addEventListener("click", () => start(m.month, m, b));
        act.setAttribute("aria-label", "Get " + monthLabel(m.month) + " part " + b.n);
      } else {
        act = document.createElement("a");
        act.className = "folder";
        act.href = zipHref(m.month, b);
        act.textContent = "Zip";
        act.setAttribute("download", "");
        act.setAttribute("aria-label", "Download " + monthLabel(m.month) + " part " + b.n + " as a zip");
      }
      p.appendChild(act);
      parts.appendChild(p);
    }
    d.appendChild(parts);
    root.appendChild(d);
  }
}

function show(state, cls) {
  $("jobState").textContent = state;
  $("job").className = "job" + (cls ? " " + cls : "");
}

function uniqueNames(files) {
  const seen = new Map();
  for (const f of files) { const base = f.rel.split("/").pop(); seen.set(base, (seen.get(base) || 0) + 1); }
  return files.map((f) => { const base = f.rel.split("/").pop(); return seen.get(base) > 1 ? f.rel.replace(/\//g, " - ") : base; });
}

function typeOf(name) { return TYPES[(name.split(".").pop() || "").toLowerCase()] || "application/octet-stream"; }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function pull(url, type, signal, onBytes) {
  for (let attempt = 0; ; attempt++) {
    let counted = 0;
    try {
      const r = await fetch(url, { credentials: "same-origin", signal });
      if (!r.ok) { const e = new Error("HTTP " + r.status); e.status = r.status; throw e; }
      const reader = r.body.getReader();
      const chunks = [];
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        chunks.push(value);
        counted += value.length;
        onBytes(value.length);
      }
      return new Blob(chunks, { type });
    } catch (e) {
      onBytes(-counted);
      if (signal.aborted || attempt >= 3 || (e.status && e.status < 500 && e.status !== 408 && e.status !== 429)) throw e;
      await sleep(800 * 2 ** attempt);
    }
  }
}

async function keepAwake() {
  try { if (navigator.wakeLock) wake = await navigator.wakeLock.request("screen"); } catch (e) { wake = null; }
}
function release() { if (wake) { wake.release().catch(() => {}); wake = null; } }

async function start(month, m, b) {
  if (job && job.running) return;
  const ctl = new AbortController();
  job = { month, m, b, ctl, running: true, files: null };
  const title = monthLabel(month) + ", part " + b.n + " of " + m.batches.length;
  $("job").hidden = false;
  $("jobTitle").textContent = title + " · " + plural(b.files.length, "file") + ", " + mb(b.bytes);
  $("jobSave").hidden = true;
  $("jobZip").hidden = true;
  $("jobCancel").hidden = false;
  $("jobBar").style.width = "0%";
  show("Starting");
  keepAwake();
  let got = 0;
  const names = uniqueNames(b.files);
  const out = new Array(b.files.length);
  let next = 0;
  const onBytes = (n) => { got += n; $("jobBar").style.width = Math.max(0, Math.min(100, (got / Math.max(1, b.bytes)) * 100)) + "%"; show("Fetching " + mb(got) + " of " + mb(b.bytes), ""); };
  const worker = async () => {
    while (next < b.files.length) {
      const i = next++;
      const f = b.files[i];
      const blob = await pull(data.base + f.rel.split("/").map(encodeURIComponent).join("/"), typeOf(names[i]), ctl.signal, onBytes);
      out[i] = new File([blob], names[i], { type: typeOf(names[i]) });
    }
  };
  try {
    await Promise.all(Array.from({ length: PARALLEL }, worker));
  } catch (e) {
    job.running = false;
    release();
    ctl.abort();
    if (e.name === "AbortError" || ctl.signal.aborted) { $("job").hidden = true; return; }
    show("Stopped: " + e.message + ". Tap Get again; nothing was changed.", "failed");
    return;
  }
  job.running = false;
  job.files = out;
  release();
  $("jobBar").style.width = "100%";
  $("jobCancel").hidden = true;
  const zip = $("jobZip");
  zip.href = zipHref(month, b);
  zip.setAttribute("download", "");
  if (canShareFiles(out)) {
    const btn = $("jobSave");
    btn.textContent = "Save " + plural(out.length, "file") + " to Photos or Files";
    btn.hidden = false;
    zip.hidden = false;
    show("Ready. Tap the button, then choose Save Images (or Save to Files).", "ready");
  } else {
    zip.textContent = "Download the zip";
    zip.hidden = false;
    show("This browser can't send files straight to Photos. Download the zip and open it in Files.", "failed");
  }
}

$("jobSave").addEventListener("click", () => {
  if (!job || !job.files) return;
  const files = job.files;
  const { month, b } = job;
  let p;
  try { p = navigator.share({ files }); } catch (e) { p = Promise.reject(e); }
  p.then(() => {
    localStorage.setItem(doneKey(month, b), "1");
    job.files = null;
    $("jobSave").hidden = true;
    $("jobZip").hidden = true;
    show("Sent. Open Photos to check, then pick the next part.", "ready");
    render();
  }).catch((e) => {
    if (e && e.name === "AbortError") show("Cancelled. Tap Save when you want to try again.", "");
    else if (e && e.name === "NotAllowedError") show("Your phone wanted a fresh tap. Tap Save again.", "failed");
    else show("Couldn't open the save sheet. Use the zip instead.", "failed");
  });
});

$("jobCancel").addEventListener("click", () => {
  if (job && job.ctl) job.ctl.abort();
  $("job").hidden = true;
});

(async () => {
  try {
    const root = new URLSearchParams(location.search).get("root") || "photos/";
    const r = await fetch("/_save/api/list?root=" + encodeURIComponent(root), { credentials: "same-origin" });
    const j = await r.json().catch(() => ({}));
    if (r.status === 401) { $("who").textContent = "signed out"; $("lede").textContent = "Sign in first, then open this page again."; return; }
    if (!r.ok) { $("lede").textContent = r.status === 404 ? "There is no photos folder for your account yet." : "Can't list your photos: " + (j.error || r.status); return; }
    data = j;
    data.shareable = sharing();
    $("who").textContent = j.user;
    const total = j.months.reduce((n, m) => n + m.files, 0);
    const bytes = j.months.reduce((n, m) => n + m.bytes, 0);
    $("lede").textContent = total ? plural(total, "photo and video file") + ", " + mb(bytes) + ". " + (data.shareable ? "Save them back onto this phone." : "This browser can't save straight to Photos, so each part comes as a zip.") : "Nothing to save yet.";
    $("howto").hidden = !total;
    render();
  } catch (e) {
    $("lede").textContent = "Can't reach the server: " + e.message;
  }
})();
