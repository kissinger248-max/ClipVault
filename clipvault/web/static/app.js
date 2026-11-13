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
  // Keep the flash message out of the address bar after reading it.
  if (window.location.search.includes("msg=")) {
    const url = new URL(window.location.href);
    url.searchParams.delete("msg");
    window.history.replaceState(null, "", url.toString());
  }
});
