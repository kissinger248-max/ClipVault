/* ClipVault website: OS-aware download button and the hero tilt. No dependencies. */
(function () {
  "use strict";
  var base = "https://github.com/kissinger248-max/ClipVault/releases/latest/download/";
  var files = { mac: "ClipVault-macOS.dmg", windows: "ClipVault-Setup-Windows.exe" };

  function detect() {
    var ua = navigator.userAgent || "";
    var platform = (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || "";
    var mobile = (navigator.userAgentData && navigator.userAgentData.mobile) || /iphone|ipad|ipod|android/i.test(ua);
    if (mobile) return "mobile";
    if (/win/i.test(platform) || /windows/i.test(ua)) return "windows";
    if (/mac/i.test(platform) || /macintosh/i.test(ua)) return "mac";
    if (/linux|x11|cros/i.test(platform + " " + ua)) return "linux";
    return "unknown";
  }

  // 1. The hero button and the matching download card.
  var os = detect();
  var hero = document.getElementById("hero-download");
  var label = document.getElementById("hero-download-label");
  if (hero && label) {
    if (files[os]) {
      hero.href = base + files[os];
      label.textContent = os === "mac" ? "Download for macOS" : "Download for Windows";
    } else if (os === "mobile") {
      label.textContent = "Get it for your computer";
      var note = document.getElementById("mobile-note");
      if (note) note.hidden = false;
    } else {
      label.textContent = "Download";
    }
  }
  var card = document.querySelector('[data-platform="' + os + '"]');
  if (card) {
    card.classList.add("is-recommended");
    var badge = document.createElement("span");
    badge.className = "rec-badge";
    badge.textContent = "For this computer";
    card.insertBefore(badge, card.firstChild);
  }

  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // 2. After a download starts, show what to do next (the browser keeps downloading).
  var next = document.getElementById("next-steps");
  document.addEventListener("click", function (event) {
    var link = event.target.closest && event.target.closest("a[href]");
    if (!link || !next) return;
    var kind = /\.dmg$/.test(link.href) ? "mac" : /\.exe$/.test(link.href) ? "windows" : null;
    if (!kind) return;
    next.setAttribute("data-os", kind);
    next.hidden = false;
    window.setTimeout(function () {
      next.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "center" });
      next.querySelector("h3").focus({ preventScroll: true });
    }, 150);
  });

  // 3. Sections rise in as they scroll into view.
  var reveals = document.querySelectorAll(".reveal");
  if ("IntersectionObserver" in window && !reduce) {
    var watcher = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-in");
          watcher.unobserve(entry.target);
        }
      });
    }, { rootMargin: "0px 0px -8% 0px" });
    reveals.forEach(function (el) { watcher.observe(el); });
  } else {
    reveals.forEach(function (el) { el.classList.add("is-in"); });
  }

  // 4. Copy buttons for the checksums.
  if (navigator.clipboard) {
    document.querySelectorAll(".checksums dd").forEach(function (dd) {
      var value = dd.textContent.trim();
      if (!/^[0-9a-f]{64}$/i.test(value)) return;
      var button = document.createElement("button");
      button.type = "button";
      button.className = "copy";
      button.textContent = "Copy";
      button.setAttribute("aria-label", "Copy checksum");
      button.addEventListener("click", function () {
        navigator.clipboard.writeText(value).then(function () {
          button.textContent = "Copied";
          window.setTimeout(function () { button.textContent = "Copy"; }, 1600);
        });
      });
      dd.appendChild(button);
    });
  }

  // 5. Hero tilt, throttled to one update per frame; off under reduced motion.
  var stage = document.querySelector(".cv-stage");
  var rig = document.querySelector(".cv-rig");
  if (stage && rig && !reduce) {
    var pending = null;
    stage.addEventListener("mousemove", function (event) {
      var box = stage.getBoundingClientRect();
      var x = (event.clientX - box.left) / box.width - 0.5;
      var y = (event.clientY - box.top) / box.height - 0.5;
      if (pending) return;
      pending = window.requestAnimationFrame(function () {
        pending = null;
        rig.style.setProperty("--tx", (6 - y * 14).toFixed(1) + "deg");
        rig.style.setProperty("--ty", (-14 + x * 26).toFixed(1) + "deg");
      });
    });
    stage.addEventListener("mouseleave", function () {
      rig.style.removeProperty("--tx");
      rig.style.removeProperty("--ty");
    });
  }
})();
