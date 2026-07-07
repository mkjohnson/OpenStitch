document.addEventListener("DOMContentLoaded", () => {
  const menuButton = document.querySelector("[data-menu-toggle]");
  const overlay = document.querySelector("[data-menu-overlay]");

  function closeMenu() {
    document.body.classList.remove("nav-open");
  }

  if (menuButton) {
    menuButton.addEventListener("click", () => {
      document.body.classList.toggle("nav-open");
    });
  }
  if (overlay) {
    overlay.addEventListener("click", closeMenu);
  }
  for (const link of document.querySelectorAll(".nav a")) {
    link.addEventListener("click", closeMenu);
  }

  const previewFrame = document.querySelector("[data-library-preview]");
  const previewTitle = document.querySelector("[data-library-preview-title]");
  const previewButtons = [...document.querySelectorAll("[data-preview-url]")];

  function setPreview(button) {
    if (!previewFrame || !button) return;
    const url = button.dataset.previewUrl;
    previewFrame.src = url;
    if (previewTitle) {
      previewTitle.textContent = button.dataset.previewTitle || "Preview";
    }
    for (const item of previewButtons) {
      item.classList.toggle("active", item === button);
    }
  }

  for (const button of previewButtons) {
    button.addEventListener("click", () => setPreview(button));
  }

  if (previewButtons.length > 0 && previewFrame && !previewFrame.getAttribute("src")) {
    setPreview(previewButtons[0]);
  }
});
