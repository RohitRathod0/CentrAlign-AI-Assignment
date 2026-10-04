"""
WHAT: The observation layer's core: JavaScript that turns the live DOM into an
      indexed, text-only page description, plus the Python formatter for it.

        Inbox
        From | Subject | Date | Attachment |
        Acme Corp <billing@acme.test> | [14]<a href="/email/10">Invoice INV-2026-1047 - October</a> | ...
        [21]<input type="text" name="invoice_number" label="Invoice number" value="">
        [25]<button>Save invoice</button>

WHY:  PRD D2. An indexed DOM snapshot is cheaper and far more reliable than
      screenshots + vision coordinates: the LLM picks `[25]`, and we click
      exactly the element we tagged with data-wi="25". Visible text is kept in
      reading order (table cells joined with "|") because the information the
      agent needs, such as an amount in a table, is usually plain text, not an
      interactive element.

      Per element we also record machine-readable facts the LLM never needs
      but the policy engine does: for submit buttons, the form's action,
      method and current field values. That lets policy judge what a click
      will actually submit. role="alert" texts and open modal dialogs are
      reported separately for the error classifier and the LLM.
"""

SNAPSHOT_JS = r"""
() => {
  const out = { url: location.href, title: document.title, lines: [], elements: {}, alerts: [], dialog: null };
  document.querySelectorAll('[data-wi]').forEach(e => e.removeAttribute('data-wi'));
  let next = 1;
  let buf = [];
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const flush = () => { const line = clean(buf.join(' ')); if (line) out.lines.push(line); buf = []; };
  const visible = el => {
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    if (s.display === 'contents') return true;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0;
  };
  const INTERACTIVE = new Set(['A', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA']);
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'SVG', 'HEAD']);
  const BLOCK = new Set(['block', 'flex', 'grid', 'table', 'table-row', 'list-item',
                         'table-row-group', 'table-header-group']);
  const absUrl = u => { try { return new URL(u, location.href).href; } catch (e) { return u; } };
  const labelOf = el => (el.labels && el.labels.length) ? clean(el.labels[0].innerText)
                                                          : clean(el.getAttribute('aria-label'));
  const describe = el => {
    const i = next++;
    el.setAttribute('data-wi', String(i));
    const tag = el.tagName.toLowerCase();
    const type = (el.type || '').toLowerCase();
    const info = { index: i, tag, type, name: el.name || '', id: el.id || '', label: labelOf(el),
                   text: clean(el.innerText || '').slice(0, 80) };
    let s;
    if (tag === 'a') {
      info.href = absUrl(el.getAttribute('href') || '');
      s = `[${i}]<a href="${el.getAttribute('href') || ''}">${info.text}</a>`;
    } else if (tag === 'select') {
      info.text = info.label;
      info.options = Array.from(el.options).map(o => clean(o.text));
      info.value = el.selectedIndex >= 0 ? clean(el.options[el.selectedIndex].text) : '';
      s = `[${i}]<select name="${info.name}" label="${info.label}" selected="${info.value}" options=${JSON.stringify(info.options)}>`;
    } else if ((tag === 'input' && !['submit', 'button', 'reset'].includes(type)) || tag === 'textarea') {
      info.text = info.label || info.name;
      info.value = type === 'password' ? (el.value ? '***' : '') : (el.value || '');
      s = `[${i}]<${tag} type="${type || 'text'}" name="${info.name}" label="${info.label}" value="${info.value}"` +
          (el.placeholder ? ` placeholder="${el.placeholder}"` : '') + '>';
    } else {
      info.text = info.text || clean(el.value);
      s = `[${i}]<button>${info.text}</button>`;
    }
    if (el.form && type === 'submit') {
      info.is_submit = true;
      info.form_action = absUrl(el.form.getAttribute('action') || location.href);
      info.form_method = (el.form.getAttribute('method') || 'GET').toUpperCase();
      const values = {};
      for (const [k, v] of new FormData(el.form).entries()) if (typeof v === 'string') values[k] = v;
      info.form_values = values;
    }
    if (el.disabled) s += ' (disabled)';
    out.elements[i] = info;
    return s;
  };
  const walk = node => {
    for (const child of node.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) { buf.push(child.textContent); continue; }
      if (child.nodeType !== Node.ELEMENT_NODE) continue;
      const tag = child.tagName.toUpperCase();
      if (SKIP.has(tag) || (tag === 'INPUT' && child.type === 'hidden')) continue;
      if (tag === 'LABEL' && child.control) continue;   // the control carries its label
      if (!visible(child)) continue;
      const display = getComputedStyle(child).display;
      const block = BLOCK.has(display);
      if (block) flush();
      if (INTERACTIVE.has(tag) || child.getAttribute('role') === 'button') buf.push(describe(child));
      else { walk(child); if (display === 'table-cell') buf.push('|'); }
      if (block) flush();
    }
  };
  walk(document.body);
  flush();
  document.querySelectorAll('[role=alert]').forEach(a => {
    const t = clean(a.innerText);
    if (t && visible(a)) out.alerts.push(t);
  });
  const dialog = Array.from(document.querySelectorAll('[role=dialog], [aria-modal=true], dialog[open]')).find(visible);
  if (dialog) out.dialog = clean(dialog.innerText).slice(0, 200);
  return out;
}
"""

# Puts form values back after the browser navigated back from a failed submit.
RESTORE_FORM_JS = r"""
(values) => {
  for (const [name, value] of Object.entries(values)) {
    const el = document.querySelector(`[name="${CSS.escape(name)}"]`);
    if (!el || el.type === 'hidden') continue;
    el.value = value;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }
}
"""


def format_snapshot(raw: dict, max_chars: int = 6000) -> str:
    head = [f"URL: {raw.get('url', '')}", f"Title: {raw.get('title', '')}"]
    if raw.get("dialog"):
        head.append(f"!! A MODAL DIALOG IS OPEN and blocks the page: \"{raw['dialog']}\". "
                    "Dismiss it before interacting with anything else.")
    if raw.get("alerts"):
        head.append("Alerts on page: " + " | ".join(raw["alerts"]))
    head.append("--- page content (interactive elements are [index]) ---")
    text = "\n".join(head) + "\n" + "\n".join(raw.get("lines", []))
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... (page truncated)"
    return text


def element_label(info: dict | None) -> str:
    """Short human description of a snapshot element, for observation summaries."""
    if not info:
        return "element"
    name = info.get("text") or info.get("label") or info.get("name") or info.get("id") or ""
    return f"[{info['index']}] <{info['tag']}> \"{name}\""
