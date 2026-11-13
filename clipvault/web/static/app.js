// Small conveniences on top of plain forms; every page also works without this file
// (forms that save as you change them show a Save button when scripts are off).
document.addEventListener("DOMContentLoaded", () => {
  // Ask before destructive actions.
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  // Look editor: redraw the previews as soon as a control changes.
  const look = document.querySelector("[data-look-form]");
  const updateLook = () => {
    const data = new FormData(look);
    const query = new URLSearchParams({
      style: data.get("style") || "",
      accent: data.get("accent") || "",
      size: data.get("size") || "",
      credit: data.get("credit") ? "1" : "0",
    });
    look.querySelectorAll("img[data-layout]").forEach((img) => {
      img.src = `/look/preview/${img.dataset.layout}.jpg?${query}`;
    });
  };
  if (look) look.addEventListener("change", updateLook);

  // Forms that save as soon as a choice changes (switches, menus, segmented controls).
  document.querySelectorAll("form[data-autosubmit]").forEach((form) => {
    form.addEventListener("change", (event) => {
      const field = event.target;
      if (field.matches("input[type=text], input[type=search], input[type=password], .more-toggle")) return;
      form.requestSubmit();
    });
  });

  // Copy buttons.
  document.querySelectorAll("[data-copy]").forEach((button) => {
    button.addEventListener("click", async () => {
      const field = document.querySelector(button.dataset.copy);
      try {
        await navigator.clipboard.writeText(field.value);
        button.textContent = "Copied";
      } catch {
        field.select();
      }
    });
  });

  // Today's date on Home, in the viewer's own words.
  const today = document.querySelector("[data-today]");
  if (today) {
    today.textContent = new Date().toLocaleDateString(undefined, {
      weekday: "long", month: "long", day: "numeric",
    });
  }

  // Pages waiting for background work refresh themselves.
  const busy = document.querySelector("[data-reload]");
  if (busy) window.setTimeout(() => window.location.reload(), Number(busy.dataset.reload) * 1000);

  // Clip page: the line being spoken is highlighted; picking a line moves the video there.
  const video = document.querySelector("[data-transcript-video]");
  const lines = [...document.querySelectorAll(".line[data-start]")];
  if (video && lines.length) {
    lines.forEach((line) => line.addEventListener("click", () => {
      video.currentTime = Number(line.dataset.start);
      video.play();
    }));
    video.addEventListener("timeupdate", () => {
      const t = video.currentTime;
      lines.forEach((line) => line.classList.toggle(
        "now", t >= Number(line.dataset.start) && t < Number(line.dataset.end) + 0.3));
    });
  }

  // Close the More menu when clicking elsewhere.
  document.addEventListener("click", (event) => {
    document.querySelectorAll("details.menu-wrap[open]").forEach((menu) => {
      if (!menu.contains(event.target)) menu.removeAttribute("open");
    });
  });

  // Keep the flash message out of the address bar after reading it.
  if (window.location.search.includes("msg=")) {
    const url = new URL(window.location.href);
    url.searchParams.delete("msg");
    window.history.replaceState(null, "", url.toString());
  }
});
