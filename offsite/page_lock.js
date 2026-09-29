// In-page submit lock for the offsite agent (OA5). Loaded by offsite/guards.py
// as a context init script: every page, every frame, every new document.
// Locked by default; window.__oaSetLock(false) (called by SubmitGuard.unlock)
// releases it for the human reviewer and survives same-tab navigation via
// sessionStorage until __oaSetLock(true).
(() => {
  if (window.__oaGuardInstalled) return;
  Object.defineProperty(window, '__oaGuardInstalled', {value: true});
  const SUBMIT = /\b(submit|send (my |your )?application|complete (my |your )?application|finish (my |your )?application|apply now)\b/i;
  const APPLY = /\bapply\b/i;
  const NEXT = /\b(next|continue|save|back|previous|add|upload|attach)\b/i;
  const KEY = '__oa_unlocked';
  let override = null;               // set by __oaSetLock from Python
  const session = () => { try { return sessionStorage.getItem(KEY); } catch (e) { return null; } };
  const locked = () => override !== null ? override : session() !== '1';
  Object.defineProperty(window, '__oaSetLock', {value: (on) => {
    override = !!on;
    try { on ? sessionStorage.removeItem(KEY) : sessionStorage.setItem(KEY, '1'); } catch (e) {}
    return locked();
  }});
  Object.defineProperty(window, '__oaIsLocked', {value: () => locked()});

  function toast(what) {
    try {
      let t = document.getElementById('__oa_guard_toast');
      if (!t) {
        t = document.createElement('div');
        t.id = '__oa_guard_toast';
        t.setAttribute('role', 'status');
        t.style.cssText = 'position:fixed;z-index:2147483647;bottom:12px;right:12px;' +
          'background:#b00020;color:#fff;padding:8px 12px;border-radius:6px;font:13px system-ui';
        (document.body || document.documentElement).appendChild(t);
      }
      t.textContent = 'BLOCKED by guard: ' + what + ' is reserved for the human reviewer.';
    } catch (e) {}
  }
  const label = (el) => ((el.innerText || el.value || el.getAttribute('aria-label') || el.title || '') + '').trim();
  function submitControl(target) {
    const el = target && target.closest && target.closest(
      'button, input[type=submit], input[type=image], input[type=button], [role=button], a');
    if (!el) return null;
    const text = label(el);
    if (!text) return null;
    if (SUBMIT.test(text)) return el;   // submit wording wins, even with "save"/"next"
    // bare "Apply": only when it is a form's submit button (not a listing-page opener)
    const isFormSubmit = el.form && (el.type === 'submit' || el.type === 'image');
    if (APPLY.test(text) && !NEXT.test(text) && isFormSubmit) return el;
    return null;
  }
  function stop(ev, what) { ev.preventDefault(); ev.stopImmediatePropagation(); toast(what); }
  for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click', 'auxclick', 'dblclick']) {
    window.addEventListener(type, (ev) => {
      if (locked() && submitControl(ev.target)) stop(ev, 'submitting');
    }, true);
  }
  window.addEventListener('keydown', (ev) => {
    if (!locked() || (ev.key !== 'Enter' && ev.code !== 'NumpadEnter')) return;
    const t = ev.target;
    if (submitControl(t)) return stop(ev, 'submitting');
    const tag = t && t.tagName;
    const combo = t && (t.getAttribute('role') === 'combobox' || t.hasAttribute('aria-autocomplete'));
    if (tag === 'INPUT' && !combo) stop(ev, 'pressing Enter in a form field');
  }, true);
  window.addEventListener('submit', (ev) => { if (locked()) stop(ev, 'submitting'); }, true);
  const P = HTMLFormElement.prototype;
  for (const m of ['submit', 'requestSubmit']) {
    const orig = P[m];
    Object.defineProperty(P, m, {configurable: false, writable: false, value: function (...a) {
      if (locked()) { toast('submitting'); return; }
      return orig.apply(this, a);
    }});
  }
})();
