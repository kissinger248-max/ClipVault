// ClipVault Web: the upload form, and the run page that fills in as the clips are made.
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (text) => String(text ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const mb = (bytes) => `${(bytes / 1048576).toFixed(bytes < 10485760 ? 1 : 0)} MB`;

  // --- upload ----------------------------------------------------------------------------------
  const form = $("upload");
  if (form) {
    const input = $("file");
    const drop = $("drop");
    const go = $("go");
    const rights = $("rights");
    const error = $("error");
    const maxBytes = Number(form.dataset.maxBytes);
    const allowed = /\.(mp4|mov|mkv|webm|m4v)$/i;
    let file = null;

    const ready = () => { go.disabled = !(file && rights.checked); };
    const fail = (message) => {
      error.textContent = message;
      error.hidden = false;
    };
    const choose = (picked) => {
      error.hidden = true;
      if (!picked) return;
      if (!allowed.test(picked.name)) return fail("Please choose an MP4, MOV, MKV or WebM video.");
      if (picked.size > maxBytes) return fail(`That file is ${mb(picked.size)}: the limit is ${form.dataset.maxMb} MB.`);
      file = picked;
      drop.classList.add("has-file");
      $("drop-title").textContent = picked.name;
      $("drop-sub").textContent = `${mb(picked.size)} · choose another`;
      ready();
    };

    input.addEventListener("change", () => choose(input.files[0]));
    rights.addEventListener("change", ready);
    ["dragenter", "dragover"].forEach((type) => drop.addEventListener(type, (e) => {
      e.preventDefault();
      drop.classList.add("over");
    }));
    ["dragleave", "drop"].forEach((type) => drop.addEventListener(type, () => drop.classList.remove("over")));
    drop.addEventListener("drop", (e) => {
      e.preventDefault();
      choose(e.dataTransfer.files[0]);
    });

    form.addEventListener("submit", (e) => {
      e.preventDefault();
      if (!file || !rights.checked) return;
      error.hidden = true;
      go.disabled = true;
      go.textContent = "Uploading…";
      $("progress").hidden = false;
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/upload");
      xhr.setRequestHeader("X-File-Name", encodeURIComponent(file.name));
      xhr.setRequestHeader("X-Who", encodeURIComponent($("who").value.trim()));
      xhr.setRequestHeader("X-Rights", "yes");
      xhr.upload.addEventListener("progress", (p) => {
        if (!p.lengthComputable) return;
        const share = p.loaded / p.total;
        $("bar").style.width = `${Math.round(share * 100)}%`;
        $("progress-amount").textContent = `${mb(p.loaded)} of ${mb(p.total)}`;
        if (share >= 1) $("progress-label").textContent = "Checking the video…";
      });
      const again = (message) => {
        fail(message);
        go.disabled = false;
        go.textContent = "Make my clips";
        $("progress").hidden = true;
      };
      xhr.addEventListener("load", () => {
        let reply = {};
        try { reply = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
        if (xhr.status === 200 && reply.url) {
          window.location.assign(reply.url);
        } else {
          again(reply.error || "The upload didn't work. Please try again.");
        }
      });
      xhr.addEventListener("error", () => again("The connection dropped. Please try again."));
      xhr.send(file);
    });
    window.addEventListener("beforeunload", (e) => {
      if (!$("progress").hidden && go.disabled) e.preventDefault(); // uploading: are you sure?
    });
  }

  // --- a run -----------------------------------------------------------------------------------
  const first = $("run-initial");
  if (!first) return;
  const grid = $("clips");
  const WAITING = 5; // loading cards before the moments are known
  let run = JSON.parse(first.textContent);
  let timer = null;

  const waitCard = (label, failed) => `<div class="frame">${failed ? "" : '<span class="shine"></span>'}<div class="wait">${failed ? "" : '<span class="spinner" aria-hidden="true"></span>'}<span>${esc(label)}</span></div></div>`;

  function card(clip) {
    const el = document.createElement("article");
    el.className = "clip";
    el.dataset.n = clip.n;
    el.dataset.status = clip.status;
    if (clip.status === "done" && clip.video) {
      el.innerHTML = `<div class="frame"><video src="${esc(clip.video)}" poster="${esc(clip.thumb || "")}" controls playsinline preload="none"></video></div>`
        + `<div class="headline">${esc(clip.headline)}</div>`
        + `<div class="meta"><span>${clip.seconds} s</span><a class="pill primary" href="${esc(clip.download)}" download>Download</a></div>`;
    } else if (clip.status === "failed") {
      el.classList.add("failed");
      el.innerHTML = waitCard("This one couldn't be made", true) + `<div class="headline">${esc(clip.headline || "")}</div>`;
    } else {
      const label = clip.status === "making" ? "Adding the headline and captions" : "Up next";
      el.innerHTML = waitCard(label, false) + `<div class="headline">${esc(clip.headline || "")}</div><div class="meta"><span>${clip.seconds} s</span></div>`;
    }
    return el;
  }

  function placeholder(i) {
    const el = document.createElement("article");
    el.className = "clip";
    el.dataset.n = `p${i}`;
    el.dataset.status = "finding";
    el.innerHTML = waitCard(i === 1 ? "Finding the best moments" : "", false);
    return el;
  }

  function render() {
    const title = $("title");
    if (run.status === "done") title.innerHTML = "Your clips are <em>ready</em>";
    else if (run.status === "failed") title.textContent = "Something went wrong";
    let status = run.step || "";
    if (run.status === "queued" && run.ahead) status = `Waiting for its turn: ${run.ahead} ahead of you`;
    else if (run.step_total && run.status !== "done") status = `${run.step} (${run.step_done} of ${run.step_total})`;
    else if (run.status === "done") status = `${run.clips.filter((c) => c.status === "done").length} clips made. Tap one to play it.`;
    $("status").textContent = status;
    const bar = $("run-bar");
    bar.hidden = run.status === "done" || run.status === "failed";
    bar.classList.toggle("busy", !run.step_total);
    if (run.step_total) bar.querySelector("i").style.width = `${Math.round((run.step_done / run.step_total) * 100)}%`;
    $("run-error").hidden = !run.error;
    $("run-error").textContent = run.error || "";
    $("zip").hidden = !run.zip;
    if (run.zip) $("zip").href = run.zip;
    $("expires").textContent = `These clips are deleted ${run.expires}. Download the ones you want to keep.`;

    if (!run.clips.length) {
      if (run.status === "failed") { grid.innerHTML = ""; return; }
      if (!grid.querySelector("[data-n^=p]")) {
        grid.innerHTML = "";
        for (let i = 1; i <= WAITING; i += 1) grid.append(placeholder(i));
      }
      return;
    }
    grid.querySelectorAll("[data-n^=p]").forEach((el) => el.remove());
    for (const clip of run.clips) {
      const old = grid.querySelector(`[data-n="${clip.n}"]`);
      if (old && old.dataset.status === clip.status) continue; // never restart a playing video
      const fresh = card(clip);
      if (old) old.replaceWith(fresh); else grid.append(fresh);
    }
  }

  async function poll() {
    try {
      const reply = await fetch(`/api/runs/${encodeURIComponent(run.id)}`, { cache: "no-store" });
      if (reply.ok) run = await reply.json();
    } catch { /* try again shortly */ }
    render();
    if (run.status !== "done" && run.status !== "failed" && run.status !== "expired") {
      timer = window.setTimeout(poll, document.hidden ? 8000 : 2000);
    }
  }

  $("copy").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(window.location.href);
      $("copy").textContent = "Link copied";
    } catch {
      window.prompt("Copy this link to come back to your clips:", window.location.href);
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && timer) { window.clearTimeout(timer); poll(); }
  });
  render();
  if (run.status !== "done" && run.status !== "failed") timer = window.setTimeout(poll, 1500);
})();
