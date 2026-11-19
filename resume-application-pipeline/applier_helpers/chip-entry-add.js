async ({ input, values, state }) => {
  const box = document.querySelector(input);
  if (!box) return { ok: false, error: `no element matches ${input}` };
  const store = state ? document.querySelector(state) : null;
  if (state && !store) return { ok: false, error: `no element matches ${state}` };
  const saved = () => {
    try { return JSON.parse(store.value || '[]').map(String); } catch { return null; }
  };
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  const enter = { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true };
  for (const value of values) {
    if (store && (saved() || []).includes(value)) continue;
    box.focus();
    setter.call(box, value);
    box.dispatchEvent(new Event('input', { bubbles: true }));
    for (const type of ['keydown', 'keypress', 'keyup']) box.dispatchEvent(new KeyboardEvent(type, enter));
    await new Promise(r => setTimeout(r, 150));
  }
  if (!store) return { ok: box.value === '', readback: null, note: 'no state selector, so the saved list was not read back' };
  const readback = saved();
  return { ok: Array.isArray(readback) && values.every(v => readback.includes(v)), readback };
}
