(() => {
  "use strict";

  const rootPath = location.pathname === "/" || location.pathname === "/index.html";
  const legacyShare = /(?:^|&)r=/.test(location.hash.slice(1));
  if (rootPath && legacyShare) {
    location.replace("/playground" + location.search + location.hash);
    return;
  }

  const status = document.getElementById("copy-status");
  let statusTimer;

  function announce(message) {
    if (!status) return;
    status.textContent = message;
    status.hidden = false;
    clearTimeout(statusTimer);
    statusTimer = setTimeout(() => { status.hidden = true; }, 1800);
  }

  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return;
    }
    const field = document.createElement("textarea");
    field.value = text;
    field.setAttribute("readonly", "");
    field.style.position = "fixed";
    field.style.opacity = "0";
    document.body.appendChild(field);
    field.select();
    const copied = document.execCommand("copy");
    field.remove();
    if (!copied) throw new Error("copy unavailable");
  }

  for (const button of document.querySelectorAll("[data-copy]")) {
    button.addEventListener("click", async () => {
      const target = document.getElementById(button.dataset.copy);
      if (!target) return;
      const original = button.textContent;
      try {
        await copyText(target.textContent.trim());
        button.textContent = "Copied";
        announce("Command copied");
      } catch (_) {
        button.textContent = "Select text";
        announce("Copy was unavailable. Select the command manually.");
      }
      setTimeout(() => { button.textContent = original; }, 1600);
    });
  }
})();
