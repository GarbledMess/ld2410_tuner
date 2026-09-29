// Patch generated markup without replacing unchanged controls, text or charts.
function nodeKey(node) {
  if (node.nodeType !== Node.ELEMENT_NODE) return String(node.nodeType);
  const identity =
    node.id ||
    node.dataset.section ||
    node.dataset.action ||
    node.dataset.detail ||
    node.dataset.pattern ||
    node.classList[0] ||
    "";
  return `${node.tagName}:${identity}`;
}

function patchAttributes(target, source) {
  for (const { name } of [...target.attributes])
    if (!source.hasAttribute(name)) target.removeAttribute(name);
  for (const { name, value } of source.attributes)
    if (target.getAttribute(name) !== value) target.setAttribute(name, value);
}

function patchChildren(target, source) {
  const desired = [...source.childNodes];
  for (const [index, next] of desired.entries()) {
    let current = target.childNodes[index];
    if (!current || nodeKey(current) !== nodeKey(next)) {
      const match = [...target.childNodes]
        .slice(index + 1)
        .find((node) => nodeKey(node) === nodeKey(next));
      current = match || next.cloneNode(true);
      target.insertBefore(current, target.childNodes[index] || null);
    }
    patchNode(current, next);
  }
  while (target.childNodes.length > desired.length) target.lastChild.remove();
}

function patchNode(target, source) {
  if (target.nodeType !== Node.ELEMENT_NODE) {
    if (target.nodeValue !== source.nodeValue)
      target.nodeValue = source.nodeValue;
    return;
  }
  // The chart renderer owns its canvas, event handlers and selection state.
  if (target.hasAttribute("data-chart-canvas")) return;
  patchAttributes(target, source);
  patchChildren(target, source);
  if (target instanceof HTMLInputElement) {
    if (target.value !== source.value) target.value = source.value;
    if (target.checked !== source.checked) target.checked = source.checked;
  }
  if (target instanceof HTMLSelectElement && target.value !== source.value)
    target.value = source.value;
}

export const panelRendering = {
  _patchElement(target, source) {
    patchNode(target, source);
  },

  _updateHtml(target, html) {
    const template = document.createElement("template");
    template.innerHTML = html;
    patchChildren(target, template.content);
  },
};
