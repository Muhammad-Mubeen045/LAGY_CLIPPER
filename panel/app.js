// Live_Clipping panel logic — polls /api/status, no raw JSON shown.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

async function api(path, opts) {
  const r = await fetch(path, opts);
  return r.json();
}

function fmtDur(s) {
  if (!s) return "live / unknown length";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
}

$("fetchBtn").onclick = async () => {
  const url = $("url").value.trim();
  if (!url) return;
  $("fetchError").classList.add("hidden");
  $("fetchBtn").disabled = true;
  $("fetchBtn").textContent = "Fetching…";
  try {
    const res = await api("/api/fetch", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    if (!res.ok) throw new Error(res.error || "Fetch failed");
    const m = res.meta;
    $("meta").classList.remove("hidden");
    $("metaThumb").src = m.thumbnail || "";
    $("metaTitle").textContent = m.title;
    $("metaSub").textContent = `${m.channel} · ${fmtDur(m.duration)}${m.view_count ? " · " + m.view_count.toLocaleString() + " views" : ""}`;
    const live = m.live_status === "is_live";
    $("metaStatus").textContent = live
      ? "● LIVE NOW — Process will record it for the time you chose, then cut the best moments."
      : m.live_status === "was_live" || m.live_status === "post_live"
        ? "Ended stream / VOD — full video will be downloaded and analysed."
        : "Video — full video will be downloaded and analysed.";
  } catch (e) {
    $("fetchError").textContent = e.message;
    $("fetchError").classList.remove("hidden");
    $("meta").classList.add("hidden");
  } finally {
    $("fetchBtn").disabled = false;
    $("fetchBtn").textContent = "Fetch";
  }
};

$("processBtn").onclick = async () => {
  const url = $("url").value.trim();
  if (!url) { alert("Paste a YouTube link first."); return; }
  $("processBtn").disabled = true;
  const res = await api("/api/process", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      url,
      // Never send NaN: JSON turns NaN into null, and the server's
      // int(null) crashed the panel (2026-11-18). `|| default` turns
      // a bad value into the default instead.
      length: +$("length").value || 60,
      clips: +$("clips").value || 5,
      sensitivity: $("sensitivity").value,
      record_minutes: +$("recordMinutes").value || 10,
      quality: $("quality").value,
      focus: $("focus").value,
      vod_range: $("vodRange").value,
      free_space: $("freeSpace").checked,
      delete_source: $("deleteSource").checked,
    }),
  });
  if (!res.ok) {
    alert(res.error || "Could not start the job.");
    $("processBtn").disabled = false;
  }
};

async function markPosted(name, btn) {
  btn.disabled = true;
  await api(`/api/clips/${encodeURIComponent(name)}/posted`, { method: "POST" });
  loadClips();
}
async function deleteClip(name, btn) {
  if (!confirm("Delete this clip from the panel, the laptop, AND Drive? Rejected clips stay in Drive — delete removes it everywhere.")) return;
  btn.disabled = true;
  const res = await api(`/api/clips/${encodeURIComponent(name)}/delete`, { method: "POST" });
  if (!res.ok) { alert(res.error || "Delete failed."); btn.disabled = false; return; }
  if (res.drive_deleted === false) alert("Clip deleted locally, but the Drive copy is still there: " + (res.drive_message || ""));
  loadClips();
}
async function retryUpload(name, btn) {
  btn.disabled = true;
  btn.textContent = "Retrying…";
  const res = await api(`/api/clips/${encodeURIComponent(name)}/retry-upload`, { method: "POST" });
  if (!res.ok) alert(res.error || "Retry failed.");
  loadClips();
}
async function approveClip(name, btn) {
  btn.disabled = true;
  const res = await api(`/api/clips/${encodeURIComponent(name)}/approve`, { method: "POST" });
  if (!res.ok) { alert(res.error || "Approve failed."); btn.disabled = false; return; }
  loadClips();
}
async function rejectClip(name, btn) {
  if (!confirm("Reject this clip as not worthy? It stays listed as rejected.")) return;
  btn.disabled = true;
  const res = await api(`/api/clips/${encodeURIComponent(name)}/reject`, { method: "POST" });
  if (!res.ok) { alert(res.error || "Reject failed."); btn.disabled = false; return; }
  loadClips();
}
async function changeTypeClip(name, target, btn) {
  btn.disabled = true;
  btn.textContent = "Re-cutting…";
  const res = await api(`/api/clips/${encodeURIComponent(name)}/change-type`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ type: target }),
  });
  if (!res.ok) alert(res.error || "Change type failed.");
  loadClips();
}
async function reframeClip(name, body, btn) {
  if (btn) { btn.disabled = true; btn.textContent = "Re-framing…"; }
  const res = await api(`/api/clips/${encodeURIComponent(name)}/reframe`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) alert(res.error || "Reframe failed.");
  loadClips();
}
async function editClip(name, idx, btn) {
  btn.disabled = true;
  const title = document.getElementById(`t-${idx}`).value;
  const desc = document.getElementById(`d-${idx}`).value;
  const tags = document.getElementById(`h-${idx}`).value;
  const start = document.getElementById(`s-${idx}`).value;
  const end = document.getElementById(`e-${idx}`).value;
  const body = { title, description: desc, hashtags: tags };
  if (start !== "" && end !== "") { body.start = +start; body.end = +end; }
  const res = await api(`/api/clips/${encodeURIComponent(name)}/edit`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) alert(res.error || "Edit failed.");
  btn.disabled = false;
  loadClips();
}
function copyText(t) { navigator.clipboard?.writeText(t); }

// --- Review & filter (v1.5) ------------------------------------------
// A clip "needs review" when it is neither approved, rejected nor
// posted — that is the queue the user works through. Filters are
// client-side over the clips the server returns.
let allClips = [];
function clipStatus(c) {
  if (c.posted) return "posted";
  if (c.approved) return "approved";
  if (c.rejected) return "rejected";
  return "needs";
}
function clipMatchesFilter(c, f) {
  if (f.status !== "all" && clipStatus(c) !== f.status) return false;
  if (f.type !== "all" && (c.cut_type || "SHORT") !== f.type) return false;
  if (f.source !== "all" && (c.stream_folder || "") !== f.source) return false;
  if (f.minScore && (c.score || 0) < f.minScore) return false;
  return true;
}
function currentFilter() {
  return {
    status: $("fStatus").value,
    type: $("fType").value,
    source: $("fSource").value,
    minScore: +$("fScore").value,
  };
}
function visibleClips() {
  const f = currentFilter();
  return allClips.filter((c) => clipMatchesFilter(c, f));
}
async function bulkAction(action, files, confirmMsg) {
  if (!files.length) { alert("No clips shown by the current filters."); return; }
  if (confirmMsg && !confirm(confirmMsg)) return;
  const res = await api("/api/clips/bulk", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, files }),
  });
  if (!res.ok) { alert(res.error || "Bulk action failed."); return; }
  const failed = (res.results || []).filter((r) => !r.ok);
  if (failed.length) alert(`${failed.length} clip(s) failed: ` + failed.map((r) => r.file).join(", "));
  loadClips();
}
async function bulkApproveShown(btn) {
  const files = visibleClips().map((c) => c.file);
  btn.disabled = true;
  await bulkAction("approve", files, `Approve ${files.length} clip(s) shown by the filters?`);
  btn.disabled = false;
}
async function bulkDeleteRejected(btn) {
  const files = allClips.filter((c) => c.rejected).map((c) => c.file);
  btn.disabled = true;
  await bulkAction("delete", files, `Delete ${files.length} rejected clip(s) from the panel, laptop, AND Drive? This cannot be undone.`);
  btn.disabled = false;
}
["fStatus", "fType", "fSource", "fScore"].forEach((id) => {
  $(id).addEventListener("change", renderClips);
});
$("bulkApproveBtn").onclick = function () { bulkApproveShown(this); };
$("bulkDeleteRejectedBtn").onclick = function () { bulkDeleteRejected(this); };

async function loadClips() {
  const res = await api("/api/clips");
  allClips = res.clips || [];
  renderClips();
}
function renderClips() {
  const clips = allClips;
  const counts = { needs: 0, approved: 0, rejected: 0, posted: 0 };
  clips.forEach((c) => { counts[clipStatus(c)] += 1; });
  $("reviewCounts").textContent =
    `${counts.needs} need review · ${counts.approved} approved · ${counts.rejected} rejected · ${counts.posted} posted`;
  // Source dropdown: one entry per stream folder, keep the selection.
  const srcSel = $("fSource");
  const prev = srcSel.value;
  const sources = [...new Set(clips.map((c) => c.stream_folder || "").filter(Boolean))].sort();
  srcSel.innerHTML = '<option value="all">All sources</option>' +
    sources.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
  srcSel.value = sources.includes(prev) ? prev : "all";
  const shown = visibleClips();
  $("clipCount").textContent = clips.length ? `(${shown.length} of ${clips.length})` : "";
  const box = $("clips");
  if (!clips.length) {
    box.innerHTML = '<p class="muted">No clips yet. Process a stream and they will appear here with previews.</p>';
    return;
  }
  if (!shown.length) {
    box.innerHTML = '<p class="muted">No clips match the current filters. Try “All clips” or clear the score filter.</p>';
    return;
  }
  box.innerHTML = shown.map((c, idx) => {
    const ds = c.drive_status || "pending";
    const badge = ds === "uploaded"
      ? `<span class="drive-badge up">✓ Drive uploaded</span>`
      : ds === "failed"
        ? `<span class="drive-badge fail">✗ Drive failed — ${esc(c.drive_error || "see log")}</span>`
        : `<span class="drive-badge pend">… Drive pending</span>`;
    const freed = c.local_freed
      ? `<span class="freed-badge">· local copy freed (${esc(c.freed_mb)} MB) — in Drive now</span>`
      : "";
    const preview = c.local_freed
      ? `<div class="clip-body"><p class="muted">Preview not on this laptop — the clip was freed after its verified Drive upload. Open it from Drive → 07-cuts-sequence.</p></div>`
      : `<video controls preload="metadata" src="/api/clip-file?name=${encodeURIComponent(c.file)}"></video>`;
    const dl = c.local_freed ? "" : `<a href="/api/clip-file?name=${encodeURIComponent(c.file)}" download="${esc(c.file)}">⬇ Download</a>`;
    const type = c.cut_type || "SHORT";
    const typeBadge = type === "VIDEO"
      ? `<span class="type-badge video">🎬 VIDEO · long-form</span>`
      : `<span class="type-badge short">⚡ SHORT · 9:16</span>`;
    const reasons = (c.reasons || []).length ? `<br><span class="muted">Why: ${esc(c.reasons.join(" · "))}</span>` : "";
    const frame = c.preview
      ? `<img class="preview-frame" src="/api/preview?name=${encodeURIComponent(c.preview)}" alt="preview frame">`
      : "";
    const otherType = type === "VIDEO" ? "SHORT" : "VIDEO";
    const framing = type === "SHORT" ? `
        <div class="framing">
          <b>Framing</b> — focus: ${esc(c.focus || "gameplay")}${c.focus === "facecam" ? " (" + esc(c.facecam_corner || "") + ")" : ""}${c.focus === "face" && c.framing ? " (" + esc(c.framing.source) + " " + esc(c.framing.anchor_x) + ", conf " + esc(c.framing.confidence) + ")" : ""} · nudge (${esc(c.nudge_x || 0)}, ${esc(c.nudge_y || 0)})
          <div class="clip-actions">
            <button class="btn ghost" onclick="reframeClip(${JSON.stringify(c.file)}, {nudge_dx:-100}, this)">← Left</button>
            <button class="btn ghost" onclick="reframeClip(${JSON.stringify(c.file)}, {nudge_dx:100}, this)">Right →</button>
            <button class="btn ghost" onclick="reframeClip(${JSON.stringify(c.file)}, {nudge_dy:-100}, this)">↑ Up</button>
            <button class="btn ghost" onclick="reframeClip(${JSON.stringify(c.file)}, {nudge_dy:100}, this)">↓ Down</button>
          </div>
          <div class="clip-actions">
            <button class="btn ghost" onclick="reframeClip(${JSON.stringify(c.file)}, {focus:'gameplay'}, this)">🎮 Gameplay focus (centre)</button>
            <button class="btn ghost" onclick="reframeClip(${JSON.stringify(c.file)}, {focus:'face'}, this)">🙂 Face focus (auto-detect)</button>
            <button class="btn ghost" onclick="reframeClip(${JSON.stringify(c.file)}, {focus:'facecam', facecam_corner: document.getElementById('fc-${idx}').value}, this)">📷 Facecam focus</button>
            <select id="fc-${idx}"><option value="top-right">facecam top-right</option><option value="top-left">facecam top-left</option><option value="bottom-right">facecam bottom-right</option><option value="bottom-left">facecam bottom-left</option></select>
          </div>
        </div>` : `<div class="framing"><b>Framing</b> — full-frame 16:9, no crop (gameplay + facecam kept).</div>`;
    return `
    <div class="clip">
      ${preview}
      <div class="clip-body">
        <div class="clip-title">${esc(c.title)}</div>
        <div class="clip-meta">
          ${typeBadge} <span class="score">Score ${esc(c.score)}</span> · ${esc(c.timestamp)} · ${esc(c.length)}s · ${esc(c.size_mb)} MB · ${esc(c.resolution || "")} ${esc(c.quality_label || "")}${reasons}<br>
          ${esc(c.credit)}<br>
          ${c.keywords?.length ? "Keywords: " + esc(c.keywords.join(", ")) : ""}
          ${c.posted ? '<span class="posted-badge">✓ POSTED</span>' : ""}
          ${c.rejected ? '<span class="rejected-badge">✗ REJECTED</span>' : ""}
          <br>${badge} ${freed}
          ${c.stream_folder ? `<br><span class="muted">Drive: 07-cuts-sequence/${esc(c.stream_folder)}/</span>` : ""}
        </div>
        ${frame}
        <div class="clip-actions">
          ${dl}
          <button class="btn ghost" onclick="copyText(${JSON.stringify(c.title + "\n\n" + c.description)})">Copy title + description</button>
          ${ds === "failed" ? `<button class="btn warn" onclick="retryUpload(${JSON.stringify(c.file)}, this)">↻ Retry Drive upload</button>` : ""}
          ${c.posted ? "" : `<button class="btn warn" onclick="markPosted(${JSON.stringify(c.file)}, this)">Mark as Posted</button>`}
          <button class="btn danger" onclick="deleteClip(${JSON.stringify(c.file)}, this)">Delete</button>
        </div>
        <div class="edit-box">
          <b>Review &amp; edit</b>
          ${c.approved ? `<span class="approved-badge">✓ APPROVED for upload — ${esc(c.approved_at || "")}</span>` : `<span class="muted">Not approved yet. Approving only records approval in Drive — it does not publish anything.</span>`}
          <input id="t-${idx}" value="${esc(c.title)}" placeholder="Title">
          <textarea id="d-${idx}" rows="2" placeholder="Description">${esc(c.description)}</textarea>
          <input id="h-${idx}" value="${esc((c.hashtags || []).join(" "))}" placeholder="Hashtags (space separated)">
          <div class="trim-row">Trim (seconds in source): start <input id="s-${idx}" type="number" min="0" value="${esc(c.start)}"> end <input id="e-${idx}" type="number" min="0" value="${esc(c.end)}"> <span class="muted">5–60s · needs the source still on the laptop</span></div>
          <div class="clip-actions">
            <button class="btn ghost" onclick="editClip(${JSON.stringify(c.file)}, ${idx}, this)">Save edits / apply trim</button>
            ${c.approved ? "" : `<button class="btn" onclick="approveClip(${JSON.stringify(c.file)}, this)">✓ Approve for upload</button>`}
            ${c.rejected ? "" : `<button class="btn warn" onclick="rejectClip(${JSON.stringify(c.file)}, this)">✗ Reject</button>`}
            <button class="btn ghost" onclick="changeTypeClip(${JSON.stringify(c.file)}, ${JSON.stringify(otherType)}, this)">⇄ Make it ${otherType}</button>
          </div>
        </div>
        ${framing}
      </div>
    </div>`;
  }).join("");
}
window.markPosted = markPosted;
window.deleteClip = deleteClip;
window.copyText = copyText;
window.retryUpload = retryUpload;
window.approveClip = approveClip;
window.rejectClip = rejectClip;
window.changeTypeClip = changeTypeClip;
window.reframeClip = reframeClip;
window.editClip = editClip;

async function loadDrive() {
  try {
    const d = await api("/api/drive-status");
    $("rcloneState").textContent = d.rclone ? "rclone found ✓" : "rclone NOT found ✗ — clips stay local as Pending";
    if (d.stream_folder) $("driveFolder").textContent = ` — this job: ${d.stream_folder}/`;
  } catch (e) { /* server starting */ }
}

function fmtMB(bytes) {
  if (bytes == null) return "?";
  return (bytes / 1e6).toFixed(1) + " MB";
}
async function poll() {
  try {
    const st = await api("/api/status");
    $("bar").style.width = (st.progress || 0) + "%";
    $("stage").textContent = st.message || st.stage || "";
    $("log").textContent = (st.log_tail || []).join("\n");
    $("log").scrollTop = $("log").scrollHeight;
    renderPipeline(st);
    renderJobDetails(st);
    renderMoments(st);
    // LIVE indicator — pulsing dot while a job runs.
    const ind = $("liveIndicator");
    const lbl = $("liveLabel");
    if (st.status === "running") {
      ind.className = "live-indicator live";
      lbl.textContent = "● LIVE — working now";
    } else if (st.status === "done") {
      ind.className = "live-indicator done";
      lbl.textContent = "Done";
    } else if (st.status === "error") {
      ind.className = "live-indicator failed";
      lbl.textContent = "Failed";
    } else {
      ind.className = "live-indicator idle";
      lbl.textContent = "Idle";
    }
    // Download progress — real bytes / % / speed, never a fake %.
    const dl = st.download;
    if (dl && (dl.downloaded_bytes != null || dl.percent != null)) {
      $("dlWrap").classList.remove("hidden");
      const pct = dl.percent != null ? dl.percent.toFixed(1) + "%" : "—% (live / unknown total)";
      const total = dl.total_bytes ? " of " + fmtMB(dl.total_bytes) : "";
      $("dlText").textContent = `⬇ Downloading: ${fmtMB(dl.downloaded_bytes)}${total} (${pct})${dl.speed ? " — " + dl.speed : ""}`;
      $("dlBar").style.width = (dl.percent != null ? dl.percent : 0) + "%";
    } else {
      $("dlWrap").classList.add("hidden");
    }
    if (st.tools) {
      const t = st.tools;
      $("tools").textContent = `ffmpeg ${t.ffmpeg ? "✓" : "✗"} · yt-dlp ${t.yt_dlp ? "✓" : "✗"} · rclone ${t.rclone ? "✓" : "✗"} · Python ${t.python}`;
      $("tools").style.color = (t.ffmpeg && t.yt_dlp) ? "#00e5ff" : "#ff3b30";
    }
    const lc = st.live_capture;
    if (lc) {
      const el = $("liveCapture");
      el.classList.remove("hidden");
      const mins = Math.floor((lc.elapsed_seconds || 0) / 60);
      const secs = (lc.elapsed_seconds || 0) % 60;
      // Recording ETA: the job records record_minutes, so show how
      // much recording time is left (v1.7).
      const recMin = st.settings && st.settings.record_minutes;
      const remain = recMin ? Math.max(0, recMin * 60 - (lc.elapsed_seconds || 0)) : null;
      const remainTxt = remain != null ? ` · ${Math.floor(remain / 60)}m ${remain % 60}s recording left` : "";
      el.textContent = `🔴 LIVE CAPTURE — ${lc.method || "capturing live now"} · elapsed ${mins}m ${secs}s${remainTxt} · segments: ${lc.segments ?? 0} · moments so far: ${lc.moments_so_far ?? 0} · ${lc.status || "recording live now"}`;
    } else {
      $("liveCapture").classList.add("hidden");
    }
    if (st.status === "done" || st.status === "error") $("processBtn").disabled = false;
    if (st.status === "running") $("processBtn").disabled = true;
  } catch (e) { /* server starting */ }
  loadClipsThrottled();
}
let lastClipLoad = 0;
function loadClipsThrottled() {
  const now = Date.now();
  if (now - lastClipLoad > 5000) { lastClipLoad = now; loadClips(); }
}

// --- v1.7: progress details -------------------------------------------------
// Stage pipeline: Fetch → Record/Download → Audio → Moments → Cutting → Done.
// The backend stage is one of fetch/download/audio/moments/clipping/done/error.
const PIPELINE_STEPS = [
  ["fetch", "1 · Fetch"],
  ["download", "2 · Record / Download"],
  ["audio", "3 · Audio"],
  ["moments", "4 · Moments"],
  ["clipping", "5 · Cutting + Upload"],
  ["done", "6 · Done"],
];
const PIPELINE_ORDER = PIPELINE_STEPS.map(s => s[0]);
function renderPipeline(st) {
  const el = $("pipeline");
  if (!st.status || st.status === "idle" || !st.stage) { el.classList.add("hidden"); el.innerHTML = ""; return; }
  el.classList.remove("hidden");
  const cur = st.stage === "error" ? "error" : st.stage;
  const curIdx = PIPELINE_ORDER.indexOf(cur === "error" ? "done" : cur);
  el.innerHTML = PIPELINE_STEPS.map(([key, label]) => {
    const idx = PIPELINE_ORDER.indexOf(key);
    let cls = "";
    if (st.status === "error" && key === "done") cls = "failed";
    else if (key === cur) cls = "now";
    else if (curIdx >= 0 && idx < curIdx) cls = "done";
    else if (st.status === "done") cls = "done";
    const mark = cls === "done" ? "✓ " : cls === "failed" ? "✗ " : "";
    return `<div class="step ${cls}">${mark}${esc(label)}</div>`;
  }).join("");
}
// Job details: started at, elapsed time, source, and the job's settings.
function fmtElapsed(startedAt) {
  if (!startedAt) return "";
  const s = Math.max(0, Math.floor((Date.now() - new Date(startedAt).getTime()) / 1000));
  const m = Math.floor(s / 60), ss = s % 60;
  return m > 0 ? `${m}m ${ss}s` : `${ss}s`;
}
function renderJobDetails(st) {
  const el = $("jobDetails");
  if (!st.status || st.status === "idle" || !st.started_at) { el.classList.add("hidden"); el.innerHTML = ""; return; }
  el.classList.remove("hidden");
  const meta = st.meta || {};
  const src = meta.title ? `${esc(meta.channel || "")} — ${esc(meta.title)}` : esc(st.url || "");
  const s = st.settings || {};
  const focusLabel = { face: "Face-centred", noface: "No face", gameplay: "Gameplay", facecam: "Facecam" }[s.focus] || s.focus || "";
  const parts = [`Started <b>${esc(st.started_at.slice(11, 19))}</b>`, `elapsed <b>${fmtElapsed(st.started_at)}</b>`];
  if (src) parts.push(src);
  if (s.quality) parts.push(`<b>${esc(s.quality)}</b> quality`);
  if (focusLabel) parts.push(focusLabel);
  if (s.clip_length) parts.push(`<b>${s.num_clips || ""}×${s.clip_length}s</b>`);
  if (s.record_minutes && st.live_capture) parts.push(`record <b>${s.record_minutes} min</b>`);
  if (st.drive_folder) parts.push(`Drive: <b>${esc(st.drive_folder)}</b>`);
  if (st.finished_at) parts.push(`finished <b>${esc(st.finished_at.slice(11, 19))}</b>`);
  el.innerHTML = parts.join(" · ");
}
// Moments found: what the job picked, with score + signals, before the
// clips are cut — so the user sees what is coming.
function renderMoments(st) {
  const el = $("momentsBox");
  const moments = st.moments || [];
  if (!moments.length) { el.classList.add("hidden"); el.innerHTML = ""; return; }
  el.classList.remove("hidden");
  el.innerHTML = `<h3>🎯 Moments found (${moments.length}) — cutting now</h3>` + moments.map(m => {
    const sigs = (m.signals || []).join(" · ");
    return `<div class="moment">#${m.rank} · <b>${esc(m.timestamp || "")}</b> · <span class="score">${m.score}</span> · ${m.length}s <span class="sig">${esc(sigs)}</span></div>`;
  }).join("");
}
setInterval(poll, 2000);
setInterval(loadDrive, 10000);
poll();
loadClips();
loadDrive();
