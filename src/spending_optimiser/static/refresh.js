// Shows the Refresh as running at once; the server still decides when it has finished.
document.querySelectorAll("form[data-refresh]").forEach((form) => {
  const button = form.querySelector("button");
  const status = document.querySelector("[data-refresh-status]");
  const label = button.textContent;
  form.addEventListener("submit", () => {
    if (status) {
      status.textContent = "Refreshing. Loading and processing emails.";
      status.setAttribute("role", "status");
      status.className = "notice";
    }
    window.setTimeout(() => {
      button.disabled = true;
      button.textContent = "Refreshing...";
      form.setAttribute("aria-busy", "true");
    });
  });
  window.addEventListener("pageshow", () => {
    button.disabled = false;
    button.textContent = label;
    form.removeAttribute("aria-busy");
  });
});
