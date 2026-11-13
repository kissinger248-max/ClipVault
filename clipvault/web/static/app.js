// Small conveniences on top of plain forms; every page also works without this file.
document.addEventListener("DOMContentLoaded", () => {
  // Ask before destructive actions.
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });
  // Starring from a list comes back to the same list.
  document.querySelectorAll("form.star input[name=next]").forEach((input) => {
    input.value = window.location.pathname + window.location.search;
  });
  // Pages waiting for background work refresh themselves.
  const busy = document.querySelector("[data-reload]");
  if (busy) {
    window.setTimeout(() => window.location.reload(), Number(busy.dataset.reload) * 1000);
  }
  // Look editor: refresh the three previews when a control changes.
  const look = document.querySelector("[data-look-form]");
  if (look) {
    const update = () => {
      const data = new FormData(look);
      const query = new URLSearchParams({
        style: data.get("style"), accent: data.get("accent"), size: data.get("size"),
        credit: data.get("credit") ? "1" : "0",
      });
      const label = look.querySelector("[data-size-label]");
      if (label) label.textContent = `${data.get("size")} px`;
      look.querySelectorAll("img[data-layout]").forEach((img) => {
        img.src = `/look/preview/${img.dataset.layout}.jpg?${query}`;
      });
    };
    look.querySelectorAll("select, input[type=color], input[type=range], input[type=checkbox]").forEach((input) => {
      input.addEventListener("change", update);
    });
  }
  // Keep the flash message out of the address bar after reading it.
  if (window.location.search.includes("msg=")) {
    const url = new URL(window.location.href);
    url.searchParams.delete("msg");
    window.history.replaceState(null, "", url.toString());
  }
});
