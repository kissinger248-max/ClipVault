// ClipVault's live view on Home and Clips: what the app is doing right now.
//
// The first answer is written into the page (#activity-initial); after that it asks
// /api/activity every few seconds and shows:
// - "Right now": what is being worked on, a chat meter for each streamer who is live,
//   and a short list of what just happened;
// - loading cards for clips on their way (found, saved, subtitled) that turn into the
//   real card the moment the phone version is ready;
// - the numbers on Home's tiles.
// Pages work without this file; they just don't move. Reduce Motion stops the animation.
(() => {
  const first = document.getElementById("activity-initial");
  const root = document.querySelector("[data-activity-now]");
  const grid = document.querySelector("[data-activity-grid]");
  if (!first || (!root && !grid)) return;

  const VISIBLE_MS = 2500;
  const HIDDEN_MS = 15000;
  const ERROR_MS = 8000;
  const FEED_SHOWN = 5;
  const STEP_NAMES = ["Found", "Saved", "Subtitles", "Phone version"];
  const ICONS = {
    downloading: '<path d="M12 4v11M7 10.5l5 5 5-5M5 20h14"/>',
    cutting: '<circle cx="6.5" cy="17.5" r="2.5"/><circle cx="17.5" cy="17.5" r="2.5"/><path d="M8.3 15.7 19 4M15.7 15.7 5 4"/>',
    subtitles: '<rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M7 11h6M15 11h2M7 15h3M12 15h5"/>',
    editing: '<rect x="7" y="2.5" width="10" height="19" rx="2.5"/><path d="M11 9.5l3.5 2.5-3.5 2.5z"/>',
    waiting: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    power: '<path d="M9 3v5M15 3v5M6.5 8h11v3a5.5 5.5 0 0 1-11 0zM12 16.5V21"/>',
    paused: '<rect x="7" y="5" width="3.5" height="14" rx="1"/><rect x="13.5" y="5" width="3.5" height="14" rx="1"/>',
    retry: '<path d="M4.5 12a7.5 7.5 0 1 0 2.2-5.3M4.5 4.5v4h4"/>',
  };

  let since = 0;
  let after = grid ? grid.dataset.after || "" : "";
  let skew = 0; // server clock minus this clock, in seconds
  let timer = null;
  let stopped = false;
  let data = null;
  const feedItems = []; // newest first
  const summary = document.querySelector("[data-activity-summary]");
  const summaryStart = summary ? summary.innerHTML : "";

  // --- small helpers --------------------------------------------------------------------
  const esc = (text) => String(text ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const now = () => Date.now() / 1000 + skew;
  const clock = (seconds) => {
    const s = Math.max(0, Math.round(seconds));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  };
  const ago = (t) => {
    const s = now() - t;
    if (s < 45) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    return `${Math.round(s / 3600)} h ago`;
  };
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const whose = (name) => (name.endsWith("s") ? `${name}’` : `${name}’s`);
  const compact = (n) => {
    const v = Number(n || 0);
    if (v < 1000) return String(v);
    if (v < 1e6) return `${(v / 1000).toFixed(1).replace(/\.0$/, "")}K`;
    return `${(v / 1e6).toFixed(1).replace(/\.0$/, "")}M`;
  };
  const icon = (name) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ICONS.waiting}</svg>`;
  const form = (action, fields, label, primary) => {
    const csrf = root ? root.dataset.csrf : "";
    const inputs = Object.entries({ csrf, ...fields })
      .map(([k, v]) => `<input type="hidden" name="${esc(k)}" value="${esc(v)}">`).join("");
    return `<form method="post" action="${action}" class="inline">${inputs}<button class="btn small${primary ? " primary" : ""}">${esc(label)}</button></form>`;
  };

  // --- "Right now" ------------------------------------------------------------------------
  function headline(d) {
    const names = (list) => list.map((c) => c.name || c.login).join(", ");
    if (d.paused) {
      return { state: "paused", title: "Paused", sub: "Nothing new is looked for, downloaded or edited until you resume." };
    }
    const work = [...d.work].sort((a, b) => b.started - a.started)[0];
    if (work) {
      const who = whose(work.name || work.login || "a streamer");
      const titles = {
        downloading: `Downloading ${who} clip`,
        cutting: `Cutting a moment from ${who} stream`,
        subtitles: `Writing subtitles for ${who} clip`,
        editing: `Making the phone version of ${who} clip`,
      };
      const more = d.waiting - 1;
      return { state: "busy", title: titles[work.stage] || "Working", sub: more > 0 ? `${plural(more, "more clip")} on the way` : "Nearly caught up" };
    }
    if (d.steps.length) {
      const live = d.live.channels.length ? ` · watching ${names(d.live.channels)} live` : "";
      return { state: "busy", title: `${d.steps[0].label}…`, sub: d.waiting ? `${plural(d.waiting, "clip")} on the way${live}` : `Started ${ago(d.steps[0].since)}${live}` };
    }
    if (d.live.channels.length) {
      return { state: "live", title: `Watching ${names(d.live.channels)} live`, sub: `A moment is saved when chat gets ${d.live.min_surge}× busier than usual.` };
    }
    if (d.waiting && !d.power.heavy_ok) {
      return { state: "waiting", title: `${plural(d.waiting, "clip")} waiting for the charger`, sub: "On battery, subtitles and phone versions wait for mains power.", action: form("/settings/heavy-on-battery", { next: "/" }, "Edit on Battery Too", false) };
    }
    if (d.waiting) {
      return { state: "busy", title: `${plural(d.waiting, "clip")} on the way`, sub: "They are worked on one after another." };
    }
    const next = d.next && d.next.discover;
    const when = next ? Math.round((next - now()) / 60) : null;
    return { state: "idle", title: "All caught up", sub: when !== null && when >= 0 ? `Next look for new clips ${when < 1 ? "in a moment" : `in ${when} min`}.` : "New clips appear here as soon as they’re found." };
  }

  function renderNow(d) {
    if (!root) return;
    const h = headline(d);
    root.querySelector("[data-orb]").dataset.state = h.state;
    setText(root.querySelector("[data-now-title]"), h.title);
    setText(root.querySelector("[data-now-sub]"), h.sub);
    const action = root.querySelector("[data-now-action]");
    if (action.dataset.html !== (h.action || "")) {
      action.innerHTML = h.action || "";
      action.dataset.html = h.action || "";
    }
    const next = document.querySelector("[data-next]");
    if (next) setText(next, d.live.channels.length ? plural(d.live.channels.length, "streamer") + " live" : "");
    renderLive(d);
    renderFeed(d);
  }

  function setText(el, text) {
    if (el && el.textContent !== text) el.textContent = text;
  }

  // --- live chat meters -------------------------------------------------------------------
  function renderLive(d) {
    const box = root.querySelector("[data-live-rows]");
    const live = d.live;
    if (!live.channels.length) {
      let note = "";
      if (d.paused) note = "";
      else if (!live.on || live.status === "off") note = `<span>Live moments are off.</span>${form("/settings/live", { on: "1", next: "/" }, "Turn On", false)}`;
      else if (live.status === "no_login") note = '<span>Sign in with Twitch to catch moments while your streamers are live.</span><a class="btn small primary" href="/settings#twitch">Sign In</a>';
      else if (live.status === "idle") note = `<span>No one on your list is live right now${live.checked ? ` · checked ${ago(live.checked)}` : ""}. Live chat is watched as soon as they start.</span>`;
      else if (live.status === "starting") note = "<span>Getting ready to watch live chat…</span>";
      const html = note ? `<div class="live-note">${note}</div>` : "";
      if (box.dataset.note !== html) {
        box.innerHTML = html;
        box.dataset.note = html;
      }
      return;
    }
    box.dataset.note = "";
    box.querySelectorAll(".live-note").forEach((el) => el.remove());
    const seen = new Set();
    for (const channel of live.channels) {
      seen.add(channel.user_id);
      let row = box.querySelector(`[data-live="${CSS.escape(channel.user_id)}"]`);
      if (!row) {
        row = document.createElement("div");
        row.className = "live-row cv-enter";
        row.dataset.live = channel.user_id;
        const face = channel.avatar
          ? `<img class="avatar" src="${esc(channel.avatar)}" alt="" width="40" height="40">`
          : `<span class="avatar initial">${esc((channel.name || "?")[0].toUpperCase())}</span>`;
        const bars = Array.from({ length: 31 }, (_, i) => `<span class="bar${i === 30 ? " now" : ""}"></span>`).join("");
        row.innerHTML = `${face}<div class="live-who"><div class="name"><span>${esc(channel.name)}</span><span class="live-pill"><i></i>LIVE</span></div><div class="sub" data-sub></div></div>`
          + `<div class="meter" role="img" aria-label="Chat activity over the last five minutes">${bars}<span class="line base"></span><span class="line goal"></span></div>`;
        box.append(row);
      }
      drawMeter(row, channel, live.min_surge);
    }
    box.querySelectorAll("[data-live]").forEach((row) => {
      if (!seen.has(row.dataset.live)) row.remove();
    });
  }

  function drawMeter(row, channel, minSurge) {
    const windows = channel.windows.slice(-30);
    const values = [...Array(30 - windows.length).fill(0), ...windows, channel.current];
    const base = channel.baseline;
    const goal = base * minSurge;
    const top = Math.max(1, ...values, goal * 1.15);
    const bars = row.querySelectorAll(".bar");
    values.forEach((value, i) => {
      bars[i].style.height = `${Math.max(3, (value / top) * 100)}%`;
      bars[i].classList.toggle("hot", base > 0 && value >= goal);
    });
    row.querySelector(".line.base").style.bottom = `${(base / top) * 100}%`;
    row.querySelector(".line.goal").style.bottom = `${(goal / top) * 100}%`;
    const last = windows.length ? windows[windows.length - 1] : 0;
    const ratio = base > 0 ? last / base : 0;
    const hot = base > 0 && Math.max(last, channel.current) >= goal;
    row.classList.toggle("hot", hot);
    const parts = [];
    if (!channel.recording) parts.push("Not recorded, so moments can’t be saved");
    else if (base > 0) parts.push(hot ? `Chat is going wild: ${ratio.toFixed(1)}× usual` : `Chat at ${ratio.toFixed(1)}× its usual level`);
    else parts.push("Getting to know this chat…");
    parts.push(plural(channel.moments, "moment"));
    if (channel.stream_started) {
      const mins = Math.max(0, Math.round((now() - channel.stream_started) / 60));
      parts.push(mins >= 60 ? `live ${Math.floor(mins / 60)} h ${mins % 60} min` : `live ${mins} min`);
    }
    setText(row.querySelector("[data-sub]"), parts.join(" · "));
  }

  // --- what just happened ---------------------------------------------------------------------
  function renderFeed(d) {
    const list = root.querySelector("[data-feed]");
    const fresh = d.events.filter((e) => !feedItems.some((x) => x.seq === e.seq));
    if (!fresh.length && list.children.length) {
      list.querySelectorAll("time[data-at]").forEach((t) => setText(t, ago(Number(t.dataset.at))));
      return;
    }
    feedItems.unshift(...fresh.reverse());
    feedItems.splice(FEED_SHOWN);
    const known = new Set([...list.children].map((li) => li.dataset.seq));
    list.innerHTML = feedItems.map((e) => {
      const who = e.name || e.login;
      const isNew = !known.has(String(e.seq)) && known.size ? " new" : "";
      const cut = e.cut_at && e.cut_at > now() ? ` · saved in <span data-until="${e.cut_at}">${clock(e.cut_at - now())}</span>` : "";
      return `<li class="${esc(e.kind)}${isNew}" data-seq="${e.seq}"><span class="dot" aria-hidden="true"></span><span>${who ? `<b>${esc(who)}</b> · ` : ""}${esc(e.text)}${cut}</span><time data-at="${e.at}">${ago(e.at)}</time></li>`;
    }).join("");
  }

  // --- loading cards ---------------------------------------------------------------------------
  function status(p) {
    const cut = p.source === "live_spike" || p.source === "vod_spike";
    const waitText = p.wait_until ? clock(p.wait_until - now()) : "";
    switch (p.status) {
      case "downloading": return { state: "active", icon: "downloading", label: "Downloading" };
      case "cutting": return { state: "active", icon: "cutting", label: "Cutting the moment" };
      case "subtitles": return { state: "active", icon: "subtitles", label: "Writing subtitles" };
      case "editing": return { state: "active", icon: "editing", label: "Making the phone version" };
      case "waiting_stream": return { state: "waiting", icon: "waiting", label: "Letting the stream catch up", sub: `cut in ${waitText}`, until: p.wait_until };
      case "retry": return { state: "waiting", icon: "retry", label: "Trying again soon", sub: `in ${waitText}`, until: p.wait_until };
      case "power": return { state: "waiting", icon: "power", label: "Waiting for the charger" };
      case "paused": return { state: "waiting", icon: "paused", label: "Paused" };
      default: {
        const next = p.steps === 1 ? (cut ? "Up next: cutting" : "Up next: download")
          : p.steps === 2 && p.subtitles ? "Up next: subtitles" : "Up next: editing";
        return { state: "queued", icon: "waiting", label: next };
      }
    }
  }

  function makeCard(p) {
    const el = document.createElement("div");
    el.className = "clip-card sk cv-enter";
    el.dataset.sk = p.id;
    const tag = p.source === "live_spike" ? '<span class="tag kind">Live moment</span>'
      : p.source === "vod_spike" ? '<span class="tag kind">Chat moment</span>' : "";
    el.innerHTML = `<div class="cv-thumb sk-tile"><span class="sk-shine" aria-hidden="true"></span>${tag}`
      + '<div class="sk-center"><span class="sk-icon"></span><span class="sk-label"></span><span class="sk-sub"></span></div>'
      + `<ol class="sk-steps" aria-label="Progress">${STEP_NAMES.map((n) => `<li title="${n}"><i></i></li>`).join("")}</ol></div>`
      + '<div><div class="who"></div><div class="meta"></div></div>';
    return el;
  }

  function updateCard(el, p) {
    const tile = el.querySelector(".sk-tile");
    if (p.has_file && !tile.querySelector(".sk-peek")) {
      const img = new Image();
      img.className = "sk-peek";
      img.alt = "";
      img.onload = () => img.classList.add("ready");
      img.src = `/thumb/${encodeURIComponent(p.id)}`;
      tile.prepend(img);
    }
    const s = status(p);
    el.dataset.state = s.state;
    const iconBox = el.querySelector(".sk-icon");
    if (iconBox.dataset.icon !== s.icon) {
      iconBox.innerHTML = icon(s.icon);
      iconBox.dataset.icon = s.icon;
    }
    setText(el.querySelector(".sk-label"), s.label);
    const sub = el.querySelector(".sk-sub");
    setText(sub, s.sub || "");
    if (s.until) sub.dataset.until = s.until; else delete sub.dataset.until;
    if (sub.dataset.until) sub.dataset.prefix = p.status === "waiting_stream" ? "cut in " : "in ";
    el.querySelectorAll(".sk-steps li").forEach((li, i) => {
      li.className = i < p.steps ? "done" : i === p.steps ? (s.state === "active" ? "now" : "next") : "";
    });
    const bar = el.querySelector(".sk-steps li.now i");
    if (bar && p.started) {
      bar.dataset.started = p.started;
      bar.dataset.expected = p.expected_s || 20;
    }
    setText(el.querySelector(".who"), p.name);
    const meta = p.source === "live_spike" ? `Live${p.surge ? ` · chat ${p.surge.toFixed(1)}×` : ""}`
      : p.source === "vod_spike" ? `Found in chat${p.surge ? ` · ${p.surge.toFixed(1)}×` : ""}`
        : `${compact(p.views)} view${p.views === 1 ? "" : "s"}`;
    setText(el.querySelector(".meta"), meta);
    el.setAttribute("aria-label", `${p.name}: ${s.label}`);
  }

  function leave(el) {
    if (el.classList.contains("leaving")) return;
    el.classList.add("leaving");
    window.setTimeout(() => el.remove(), 300);
  }

  function renderGrid(d) {
    if (!grid) return;
    const limit = Number(grid.dataset.limit || 0);
    const loading = Number(grid.dataset.loading || 3);
    const firstReal = () => [...grid.children].find((el) => !el.dataset.sk);
    // Finished: the loading card turns into the real one (or the new card joins the front).
    for (const item of [...d.done].reverse()) {
      if (grid.querySelector(`a.clip-card[href="/clips/${CSS.escape(encodeURIComponent(item.id))}"]`)) continue;
      const box = document.createElement("div");
      box.innerHTML = item.html.trim();
      const card = box.firstElementChild;
      if (!card) continue;
      card.classList.add("cv-arrive");
      const waiting = grid.querySelector(`[data-sk="${CSS.escape(item.id)}"]`);
      if (waiting) waiting.replaceWith(card);
      else grid.insertBefore(card, firstReal() || null);
    }
    // On their way: up to `loading` cards at the front, in the server's order.
    const wanted = d.pipeline.slice(0, loading);
    const keep = new Set(wanted.map((p) => p.id));
    grid.querySelectorAll("[data-sk]").forEach((el) => { if (!keep.has(el.dataset.sk)) leave(el); });
    wanted.forEach((p, i) => {
      let el = grid.querySelector(`[data-sk="${CSS.escape(p.id)}"]:not(.leaving)`);
      if (!el) el = makeCard(p);
      updateCard(el, p);
      if (grid.children[i] !== el) grid.insertBefore(el, grid.children[i] || null);
    });
    if (limit) { // Home shows one row
      const shown = [...grid.children].filter((el) => !el.classList.contains("leaving"));
      shown.forEach((el, i) => { el.hidden = i >= limit; });
      shown.slice(limit + 6).forEach((el) => el.remove());
    }
    const empty = document.querySelector("[data-empty]");
    if (empty) empty.hidden = grid.querySelector(":scope > :not([hidden])") !== null;
  }

  // --- tiles and summary -------------------------------------------------------------------------
  function renderCounts(d) {
    document.querySelectorAll("[data-count]").forEach((el) => {
      const value = d.counts[el.dataset.count];
      if (value === undefined || el.textContent === String(value)) return;
      el.textContent = String(value);
      el.classList.remove("bump");
      void el.offsetWidth; // restart the animation
      el.classList.add("bump");
    });
    if (summary) {
      const html = d.waiting
        ? `<span class="spinner cv-spin" aria-hidden="true"></span><span>${plural(d.waiting, "clip")} on the way<span class="desk-only">&nbsp;· each appears here when its phone version is ready</span>.</span>`
        : summaryStart;
      if (summary.dataset.html !== html) {
        summary.innerHTML = html;
        summary.dataset.html = html;
      }
    }
  }

  // --- every second: countdowns and progress bars ---------------------------------------------------
  function tick() {
    document.querySelectorAll("[data-until]").forEach((el) => {
      const left = Number(el.dataset.until) - now();
      const text = left > 0 ? clock(left) : "any moment";
      setText(el, el.dataset.prefix ? `${el.dataset.prefix}${text}` : text);
    });
    document.querySelectorAll(".sk-steps li.now i[data-started]").forEach((bar) => {
      const share = (now() - Number(bar.dataset.started)) / Number(bar.dataset.expected);
      // Fills to about 90% in the usual time, then creeps on until the work is done.
      const pct = share < 1 ? share * 90 : 98 - 8 / share;
      bar.style.width = `${Math.min(98, Math.max(4, pct))}%`;
    });
  }

  // --- asking the app ---------------------------------------------------------------------------------
  function render(d) {
    data = d;
    skew = d.now - Date.now() / 1000;
    since = Math.max(since, d.seq);
    after = d.after || after;
    renderNow(d);
    renderGrid(d);
    renderCounts(d);
    tick();
  }

  function schedule(ms) {
    window.clearTimeout(timer);
    if (!stopped) timer = window.setTimeout(poll, ms);
  }

  async function poll() {
    try {
      const reply = await fetch(`/api/activity?since=${since}&after=${encodeURIComponent(after)}`,
        { cache: "no-store", headers: { Accept: "application/json" } });
      if (!reply.ok || !(reply.headers.get("content-type") || "").includes("json")) {
        if (reply.redirected || reply.status === 401 || reply.status === 403) stopped = true; // signed out
        schedule(ERROR_MS);
        return;
      }
      render(await reply.json());
      schedule(document.hidden ? HIDDEN_MS : VISIBLE_MS);
    } catch {
      schedule(ERROR_MS); // the app is restarting or busy: try again shortly
    }
  }

  document.addEventListener("visibilitychange", () => { if (!document.hidden) schedule(0); });
  try {
    render(JSON.parse(first.textContent));
  } catch {
    // a broken first answer only means waiting for the next one
  }
  window.setInterval(tick, 1000);
  schedule(VISIBLE_MS);
})();
