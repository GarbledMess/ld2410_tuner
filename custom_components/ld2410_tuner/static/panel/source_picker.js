// Render suggestions in the panel: native datalist popups vary across browsers.
const RESULT_LIMIT = 30;

function eligible(entityId, state, kind) {
  if (kind === "bermuda")
    return (
      entityId.startsWith("sensor.") && "area_id" in (state.attributes || {})
    );
  return (
    /^(binary_sensor|input_boolean|switch)\./.test(entityId) ||
    ["on", "off", "true", "false"].includes(String(state.state).toLowerCase())
  );
}

function matchingEntities(states, kind, query) {
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  return Object.entries(states || {}).filter(([id, state]) => {
    if (!eligible(id, state, kind)) return false;
    const text = `${id} ${state.attributes?.friendly_name || ""}`.toLowerCase();
    return words.every((word) => text.includes(word));
  });
}

export function sourcePickerHtml(escape, id, value) {
  return `<div class="source-picker" data-source-picker>
    <label for="${escape(id)}-input">Entity</label>
    <input id="${escape(id)}-input" data-source-entity type="search" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="${escape(id)}" autocomplete="off" autocapitalize="none" autocorrect="off" spellcheck="false" value="${escape(value || "")}" required placeholder="Search name or entity ID">
    <div data-source-suggestions hidden><div id="${escape(id)}" data-source-options role="listbox" aria-label="Matching entities"></div><div class="muted source-picker-status" data-source-search-status role="status"></div></div>
  </div>`;
}

export function wireSourcePicker(row, getStates, escape) {
  const picker = row.querySelector("[data-source-picker]");
  const input = picker.querySelector("input");
  const popup = picker.querySelector("[data-source-suggestions]");
  const list = picker.querySelector("[data-source-options]");
  const status = picker.querySelector("[data-source-search-status]");
  let active = -1;

  const close = () => {
    popup.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    active = -1;
  };
  const show = () => {
    const matches = matchingEntities(
      getStates(),
      row.querySelector("[data-source-kind]").value,
      input.value,
    );
    list.innerHTML = matches
      .slice(0, RESULT_LIMIT)
      .map(
        ([id, state], index) =>
          `<button type="button" role="option" tabindex="-1" aria-selected="false" id="${escape(list.id)}-${index}" data-source-option="${escape(id)}"><b>${escape(state.attributes?.friendly_name || id)}</b><span>${escape(id)}</span></button>`,
      )
      .join("");
    status.textContent = matches.length
      ? `${matches.length} matching entities${matches.length > RESULT_LIMIT ? `; showing the first ${RESULT_LIMIT}. Keep typing to narrow the search.` : "."}`
      : "No matching entities. You can enter an entity ID directly.";
    popup.hidden = false;
    list.scrollTop = 0;
    input.setAttribute("aria-expanded", "true");
    input.removeAttribute("aria-activedescendant");
    active = -1;
  };
  const choose = (option) => {
    if (!option || input.disabled) return;
    input.value = option.dataset.sourceOption;
    input.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
    input.focus({ preventScroll: true });
    close();
  };
  const move = (direction) => {
    if (popup.hidden) show();
    const options = [...list.querySelectorAll("[data-source-option]")];
    if (!options.length) return;
    active =
      active < 0
        ? direction > 0
          ? 0
          : options.length - 1
        : (active + direction + options.length) % options.length;
    options.forEach((option, index) =>
      option.setAttribute("aria-selected", String(index === active)),
    );
    input.setAttribute("aria-activedescendant", options[active].id);
    options[active].scrollIntoView({ block: "nearest" });
  };
  input.onfocus = show;
  input.oninput = show;
  // Mobile IMEs may finish a composition without a final ordinary key event.
  input.oncompositionend = show;
  input.onkeydown = (event) => {
    if (event.isComposing || event.keyCode === 229) return;
    if (["ArrowDown", "ArrowUp"].includes(event.key)) {
      event.preventDefault();
      move(event.key === "ArrowDown" ? 1 : -1);
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (!popup.hidden) choose(list.children[active]);
      close();
    } else if (event.key === "Escape") {
      event.preventDefault();
      close();
    } else if (event.key === "Tab") close();
  };
  list.onpointerdown = (event) => {
    // Keep keyboard focus for mouse clicks. Touch must retain native scrolling.
    if (event.pointerType === "mouse") event.preventDefault();
  };
  list.onclick = (event) =>
    choose(event.target.closest("[data-source-option]"));
  picker.onfocusout = () => {
    requestAnimationFrame(() => {
      if (!picker.contains(input.getRootNode().activeElement)) close();
    });
  };
}
