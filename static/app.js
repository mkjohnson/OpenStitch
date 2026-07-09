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

  const threadWeight = document.querySelector("[data-thread-weight]");
  const fillSpacing = document.querySelector("[data-fill-spacing]");
  let spacingEdited = false;
  if (fillSpacing) {
    fillSpacing.addEventListener("input", () => {
      spacingEdited = true;
    });
  }
  if (threadWeight && fillSpacing) {
    threadWeight.addEventListener("change", () => {
      const option = threadWeight.selectedOptions[0];
      if (!spacingEdited && option && option.dataset.spacing) {
        fillSpacing.value = option.dataset.spacing;
      }
    });
  }

  const unitsSelect = document.querySelector("[data-units-select]");
  const fabricColor = document.querySelector("[data-fabric-color]");
  const unitLabels = [...document.querySelectorAll("[data-unit-label]")];
  const mmFields = [...document.querySelectorAll("[data-mm-field]")];
  const uploadForm = document.querySelector(".upload-form");
  const fieldMetricAttrs = new Map(mmFields.map((field) => [field, {
    min: field.getAttribute("min"),
    max: field.getAttribute("max"),
    step: field.getAttribute("step"),
  }]));
  let activeUnits = "metric";

  function convertFieldValue(field, fromUnits, toUnits) {
    const value = Number(field.value);
    if (!Number.isFinite(value)) return;
    let next = value;
    if (fromUnits === "metric" && toUnits === "sae") next = value / 25.4;
    if (fromUnits === "sae" && toUnits === "metric") next = value * 25.4;
    const decimals = toUnits === "sae" ? 3 : 2;
    field.value = String(Number(next.toFixed(decimals)));
  }

  function convertAttr(value, fromUnits, toUnits, decimals) {
    if (value === null || value === "" || value === "any") return value;
    const number = Number(value);
    if (!Number.isFinite(number)) return value;
    if (fromUnits === "metric" && toUnits === "sae") return String(Number((number / 25.4).toFixed(decimals)));
    if (fromUnits === "sae" && toUnits === "metric") return String(Number((number * 25.4).toFixed(decimals)));
    return value;
  }

  function syncFieldConstraints(units) {
    for (const field of mmFields) {
      const attrs = fieldMetricAttrs.get(field);
      if (!attrs) continue;
      if (units === "sae") {
        field.min = convertAttr(attrs.min, "metric", "sae", 4) || "";
        if (attrs.max !== null) field.max = convertAttr(attrs.max, "metric", "sae", 4) || "";
        field.step = convertAttr(attrs.step, "metric", "sae", 4) || "any";
      } else {
        if (attrs.min !== null) field.min = attrs.min;
        if (attrs.max !== null) field.max = attrs.max;
        if (attrs.step !== null) field.step = attrs.step;
      }
    }
  }

  function syncUnitLabels() {
    const suffix = unitsSelect && unitsSelect.value === "sae" ? "in" : "mm";
    for (const label of unitLabels) {
      label.textContent = `${label.dataset.unitLabel}, ${suffix}`;
    }
  }

  if (unitsSelect) {
    const savedUnits = localStorage.getItem("openstitch-measurement-units");
    if (savedUnits === "metric" || savedUnits === "sae") {
      unitsSelect.value = savedUnits;
    }
    for (const field of mmFields) {
      convertFieldValue(field, "metric", unitsSelect.value);
    }
    syncFieldConstraints(unitsSelect.value);
    activeUnits = unitsSelect.value;
    syncUnitLabels();
    unitsSelect.addEventListener("change", () => {
      for (const field of mmFields) {
        convertFieldValue(field, activeUnits, unitsSelect.value);
      }
      syncFieldConstraints(unitsSelect.value);
      activeUnits = unitsSelect.value;
      localStorage.setItem("openstitch-measurement-units", unitsSelect.value);
      syncUnitLabels();
    });
  }

  if (fabricColor) {
    const savedColor = localStorage.getItem("openstitch-fabric-color");
    if (/^#[0-9a-f]{6}$/i.test(savedColor || "")) {
      fabricColor.value = savedColor;
    }
    fabricColor.addEventListener("input", () => {
      localStorage.setItem("openstitch-fabric-color", fabricColor.value);
    });
  }

  if (uploadForm && unitsSelect) {
    uploadForm.addEventListener("submit", () => {
      if (activeUnits === "sae") {
        for (const field of mmFields) {
          convertFieldValue(field, "sae", "metric");
        }
        syncFieldConstraints("metric");
        activeUnits = "metric";
      }
    });
  }
});
