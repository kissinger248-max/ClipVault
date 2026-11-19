// ClipVault Web: the upload form, and the run page that fills in as the clips are made.
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (text) => String(text ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const mb = (bytes) => `${(bytes / 1048576).toFixed(bytes < 10485760 ? 1 : 0)} MB`;
  // A nudge toward the app was on screen: counted; nothing about the visitor is sent.
  const shown = new Set();
  const beacon = (where) => {
    if (shown.has(where)) return;
    shown.add(where);
    const body = new Blob([JSON.stringify({ name: "nudge_shown", detail: where })], { type: "application/json" });
    try { navigator.sendBeacon("/api/event", body); } catch { /* counting is optional */ }
  };
  document.querySelectorAll("[data-nudge]").forEach((el) => beacon(el.dataset.nudge));

  // --- starting a run: the quick check, errors, the daily limit ------------------------------
  let human = "";
  window.cvHuman = (token) => { human = token; };
  const humanHeaders = () => (human ? { "X-Turnstile": human } : {});
  const used = () => {
    human = "";
    try { if (window.turnstile) window.turnstile.reset(); } catch { /* no check on this page */ }
  };
  const showError = (message, kind) => {
    const error = $("error");
    const limit = $("limit");
    if (kind === "limit" && limit) {
      limit.innerHTML = `${esc(message)} <a href="/go/app?from=limit">Get the free app →</a>`;
      limit.hidden = false;
      if (error) error.hidden = true;
      beacon("limit");
      return;
    }
    if (limit) limit.hidden = true;
    if (error) {
      error.textContent = message;
      error.hidden = false;
    }
  };
  const clearError = () => {
    if ($("error")) $("error").hidden = true;
    if ($("limit")) $("limit").hidden = true;
  };
  async function start(url, body, headers) {
    const reply = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...humanHeaders(), ...headers },
      body: JSON.stringify(body),
    });
    used();
    let data = {};
    try { data = await reply.json(); } catch { /* not JSON */ }
    if (reply.ok && data.url) {
      window.location.assign(data.url);
      return true;
    }
    showError(data.error || "That didn't work. Please try again.", data.kind);
    return false;
  }

  // --- a Twitch link or a streamer's name, with suggestions --------------------------------------
  const twitch = $("twitch");
  if (twitch) {
    const link = $("link");
    const list = $("suggest");
    const goLink = $("link-go");
    let results = [];
    let active = -1;
    let timer = null;
    let asked = "";

    const close = () => {
      list.hidden = true;
      link.setAttribute("aria-expanded", "false");
      active = -1;
    };
    const draw = () => {
      if (!results.length) { close(); return; }
      list.innerHTML = results.map((r, i) => {
        const face = r.avatar ? `<img src="${esc(r.avatar)}" alt="" width="32" height="32">` : `<span class="face">${esc(r.name[0] || "?")}</span>`;
        const live = r.live ? '<span class="live-pill">LIVE</span>' : "";
        return `<li role="option" id="opt-${i}" data-i="${i}" aria-selected="${i === active}">${face}<span class="who">${esc(r.name)}</span>${live}</li>`;
      }).join("");
      list.hidden = false;
      link.setAttribute("aria-expanded", "true");
    };
    const pick = (i) => {
      const r = results[i];
      if (!r) return;
      link.value = `twitch.tv/${r.login}`;
      close();
      twitch.requestSubmit();
    };
    link.addEventListener("input", () => {
      clearError();
      window.clearTimeout(timer);
      const q = link.value.trim();
      if (q.length < 2 || /[/.:]/.test(q)) { results = []; close(); return; }
      timer = window.setTimeout(async () => {
        asked = q;
        try {
          const reply = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
          const data = await reply.json();
          if (asked !== link.value.trim()) return; // typed on meanwhile
          results = data.results || [];
          active = -1;
          draw();
        } catch { results = []; close(); }
      }, 250);
    });
    link.addEventListener("keydown", (e) => {
      if (list.hidden) return;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        active = (active + (e.key === "ArrowDown" ? 1 : -1) + results.length) % results.length;
        draw();
        link.setAttribute("aria-activedescendant", `opt-${active}`);
      } else if (e.key === "Enter" && active >= 0) {
        e.preventDefault();
        pick(active);
      } else if (e.key === "Escape") {
        close();
      }
    });
    list.addEventListener("mousedown", (e) => {
      const item = e.target.closest("li[data-i]");
      if (item) { e.preventDefault(); pick(Number(item.dataset.i)); }
    });
    link.addEventListener("blur", () => window.setTimeout(close, 150));
    twitch.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!link.value.trim()) { link.focus(); return; }
      clearError();
      goLink.disabled = true;
      goLink.textContent = "Finding moments…";
      const ok = await start("/api/twitch", { link: link.value.trim() }, {});
      if (!ok) {
        goLink.disabled = false;
        goLink.textContent = "Make clips";
      }
    });
  }

  // --- "My channel" ------------------------------------------------------------------------------
  const mine = $("mine-go");
  if (mine) {
    mine.addEventListener("click", async () => {
      clearError();
      mine.disabled = true;
      mine.textContent = "Finding your best clips…";
      const ok = await start("/api/mine", {}, { "X-Form": mine.dataset.form });
      if (!ok) {
        mine.disabled = false;
        mine.textContent = "Make clips from my channel";
      }
    });
  }

  // --- upload ------------------------------------------------------------------------------------
  const form = $("upload");
  const box = $("start");
  if (form && box) {
    const input = $("file");
    const drop = $("drop");
    const go = $("go");
    const rights = $("rights");
    const maxBytes = Number(box.dataset.maxBytes);
    const allowed = /\.(mp4|mov|mkv|webm|m4v)$/i;
    let file = null;

    const ready = () => { go.disabled = !(file && rights.checked); };
    const choose = (picked) => {
      clearError();
      if (!picked) return;
      if (!allowed.test(picked.name)) return showError("Please choose an MP4, MOV, MKV or WebM video.");
      if (picked.size > maxBytes) return showError(`That file is ${mb(picked.size)}: the limit is ${box.dataset.maxMb} MB.`);
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
      clearError();
      go.disabled = true;
      go.textContent = "Uploading…";
      $("progress").hidden = false;
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/upload");
      xhr.setRequestHeader("X-File-Name", encodeURIComponent(file.name));
      xhr.setRequestHeader("X-Who", encodeURIComponent($("who").value.trim()));
      xhr.setRequestHeader("X-Rights", "yes");
      if (human) xhr.setRequestHeader("X-Turnstile", human);
      used();
      xhr.upload.addEventListener("progress", (p) => {
        if (!p.lengthComputable) return;
        const share = p.loaded / p.total;
        $("bar").style.width = `${Math.round(share * 100)}%`;
        $("progress-amount").textContent = `${mb(p.loaded)} of ${mb(p.total)}`;
        if (share >= 1) $("progress-label").textContent = "Checking the video…";
      });
      const again = (message, kind) => {
        showError(message, kind);
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
          again(reply.error || "The upload didn't work. Please try again.", reply.kind);
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
  const STEP_NAMES = ["Found", "Saved", "Subtitles", "Phone version"];
  const ICONS = {
    finding: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>',
    fetching: '<path d="M12 4v11M7 10.5l5 5 5-5M5 20h14"/>',
    listening: '<rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M7 11h6M15 11h2M7 15h3M12 15h5"/>',
    making: '<rect x="7" y="2.5" width="10" height="19" rx="2.5"/><path d="M11 9.5l3.5 2.5-3.5 2.5z"/>',
    waiting: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    failed: '<circle cx="12" cy="12" r="8.5"/><path d="M9 9l6 6M15 9l-6 6"/>',
  };
  const LABELS = {
    finding: "Finding the best moments",
    waiting: "Up next: the phone version",
    ready: "Up next: the phone version",
    fetching: "Saving the moment",
    listening: "Writing the captions",
    making: "Adding the headline and captions",
    failed: "This one couldn't be made",
  };
  let run = JSON.parse(first.textContent);
  let skew = (run.now || Date.now() / 1000) - Date.now() / 1000;
  let timer = null;
  const now = () => Date.now() / 1000 + skew;
  const icon = (name) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ICONS.waiting}</svg>`;

  function loadingCard(key, state, clip) {
    const el = document.createElement("article");
    el.className = "clip loading";
    el.dataset.n = key;
    el.dataset.status = state;
    const steps = clip ? clip.steps : 0;
    const idle = state === "waiting" || state === "ready" || state === "finding";
    const stepList = STEP_NAMES.map((name, i) => {
      let cls = "";
      if (i < steps) cls = "done";
      else if (i === steps && state !== "failed") cls = idle ? "next" : "now";
      const timing = cls === "now" && clip && clip.started
        ? ` data-started="${clip.started}" data-expected="${clip.expected || 30}"` : "";
      return `<li class="${cls}" title="${name}"><i${timing}></i></li>`;
    }).join("");
    const shownIcon = state === "ready" ? "waiting" : state;
    el.innerHTML = `<div class="frame sk" data-state="${state}">${state === "failed" ? "" : '<span class="shine" aria-hidden="true"></span>'}`
      + `<div class="sk-center"><span class="sk-icon">${icon(shownIcon)}</span><span class="sk-label">${esc(key === "p1" || clip ? LABELS[state] || "" : "")}</span></div>`
      + `<ol class="sk-steps" aria-label="Progress">${stepList}</ol></div>`
      + `<div class="headline">${esc((clip && clip.headline) || "")}</div>`
      + (clip ? `<div class="meta"><span>${clip.seconds} s</span></div>` : "");
    el.setAttribute("aria-label", LABELS[state] || "");
    return el;
  }

  function embedCard(clip) {
    const el = document.createElement("article");
    el.className = "clip";
    el.dataset.n = clip.n;
    el.dataset.status = clip.status;
    const player = clip.embed
      ? `<iframe src="${esc(clip.embed)}" title="${esc(clip.headline || "Twitch clip")}" allowfullscreen loading="lazy"></iframe>`
      : `<a class="wait" href="${esc(clip.link || "#")}" target="_blank" rel="noopener">Watch on Twitch</a>`;
    el.innerHTML = `<div class="frame wide">${player}</div>`
      + `<div class="headline">${esc(clip.headline || "")}</div>`
      + `<div class="meta"><span>${clip.views ? `${clip.views.toLocaleString()} views` : ""}</span>${clip.link ? `<a href="${esc(clip.link)}" target="_blank" rel="noopener">On Twitch</a>` : ""}</div>`;
    return el;
  }

  function doneCard(clip) {
    const el = document.createElement("article");
    el.className = "clip arrive";
    el.dataset.n = clip.n;
    el.dataset.status = clip.status;
    el.innerHTML = `<div class="frame"><video src="${esc(clip.video)}" poster="${esc(clip.thumb || "")}" controls playsinline preload="none"></video></div>`
      + `<div class="headline">${esc(clip.headline)}</div>`
      + `<div class="meta"><span>${clip.seconds} s</span><a class="pill primary" href="${esc(clip.download)}" download>Download</a></div>`;
    return el;
  }

  // The one nudge toward the app on this screen: while waiting, then once the clips are made.
  function nudgeCard(kind) {
    const el = document.createElement("article");
    el.className = "clip nudge";
    el.dataset.n = `nudge-${kind}`;
    let title;
    let text;
    if (kind === "working") {
      title = "While you wait…";
      text = "The free ClipVault app does this on its own for every streamer you follow, all day, even while you sleep.";
    } else if (kind === "live") {
      title = `${run.streamer || "They"} ${run.streamer ? "is" : "are"} live`;
      text = "right now. The free ClipVault app clips live moments as they happen, the second chat goes wild.";
    } else if (kind === "embed") {
      title = "Make these vertical";
      text = "The free ClipVault app saves these moments on your computer and turns them into phone-ready clips with headlines and captions.";
    } else if (run.more_found > 0) {
      title = `+${run.more_found} more moment${run.more_found === 1 ? "" : "s"}`;
      text = `found in this video. The website makes the top ${run.clips.length}; the free app makes all of them and keeps every clip in a folder.`;
    } else {
      title = "Want this for every stream?";
      text = "The free ClipVault app watches your streamers all day, clips the best moments, even live, and saves them in a folder.";
    }
    el.innerHTML = `<div class="frame nudge-frame"><div class="nudge-body"><span class="nudge-icon" aria-hidden="true">${icon("making")}</span>`
      + `<b>${esc(title)}</b><p>${esc(text)}</p><a class="pill primary" href="/go/app?from=${kind}">Get the free app</a></div></div>`;
    return el;
  }

  function render() {
    const title = $("title");
    if (run.embed_only) title.innerHTML = `${esc(run.streamer || "The")}'s best <em>moments</em>`;
    else if (run.status === "done") title.innerHTML = "Your clips are <em>ready</em>";
    else if (run.status === "failed") title.textContent = "Something went wrong";
    let status = run.step || "";
    if (run.status === "queued" && run.ahead) status = `Waiting for its turn: ${run.ahead} ahead of you`;
    else if (run.step_total && run.status !== "done") status = `${run.step} (${run.step_done} of ${run.step_total})`;
    else if (run.embed_only) status = "Watch them here, or make them into vertical clips with the free app.";
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
    $("expires").textContent = run.embed_only
      ? `This page stops working ${run.expires}.`
      : `These clips are deleted ${run.expires}. Download the ones you want to keep.`;

    if (run.status === "failed" && !run.clips.length) {
      grid.innerHTML = "";
      return;
    }
    const wanted = run.clips.length
      ? run.clips.map((c) => [String(c.n), c.status, c])
      : Array.from({ length: WAITING }, (_, i) => [`p${i + 1}`, "finding", null]);
    const keep = new Set(wanted.map(([key]) => key));
    if (run.nudge) keep.add(`nudge-${run.nudge}`);
    [...grid.children].forEach((el) => { if (!keep.has(el.dataset.n)) el.remove(); });
    wanted.forEach(([key, state, clip], i) => {
      const old = grid.querySelector(`[data-n="${key}"]`);
      const started = clip ? String(clip.started) : "";
      // A finished card is never drawn again (that would restart a playing video).
      if (old && old.dataset.status === state && (state === "done" || state === "embed" || old.dataset.started === started)) return;
      let fresh;
      if (state === "done" && clip.video) fresh = doneCard(clip);
      else if (state === "embed") fresh = embedCard(clip);
      else fresh = loadingCard(key, state, clip);
      fresh.dataset.started = started;
      if (old) old.replaceWith(fresh);
      else grid.insertBefore(fresh, grid.children[i] || null);
    });
    if (run.nudge && !grid.querySelector(`[data-n="nudge-${run.nudge}"]`)) {
      grid.append(nudgeCard(run.nudge));
      beacon(run.nudge);
    }
    tick();
  }

  // Every second: the current step's bar fills to about 90% in the usual time, then creeps on.
  function tick() {
    grid.querySelectorAll(".sk-steps li.now i[data-started]").forEach((barEl) => {
      const share = (now() - Number(barEl.dataset.started)) / Number(barEl.dataset.expected || 30);
      const pct = share < 1 ? share * 90 : 98 - 8 / share;
      barEl.style.width = `${Math.min(98, Math.max(4, pct))}%`;
    });
  }

  async function poll() {
    try {
      const reply = await fetch(`/api/runs/${encodeURIComponent(run.id)}`, { cache: "no-store" });
      if (reply.ok) {
        run = await reply.json();
        skew = run.now - Date.now() / 1000;
      }
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
  window.setInterval(tick, 1000);
  if (run.status !== "done" && run.status !== "failed") timer = window.setTimeout(poll, 1500);
})();
