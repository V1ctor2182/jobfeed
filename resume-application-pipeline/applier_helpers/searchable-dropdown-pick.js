async ({ input, option }) => {
  const box = document.querySelector(input);
  if (!box) return { ok: false, error: `no element matches ${input}` };
  box.focus();
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  setter.call(box, option.slice(0, Math.max(1, Math.min(3, option.length))));
  box.dispatchEvent(new Event('input', { bubbles: true }));
  const deadline = Date.now() + 3000;
  let choice;
  while (!choice && Date.now() < deadline) {
    choice = [...document.querySelectorAll('[role=option]')].find(o => o.textContent.trim() === option && o.offsetParent !== null);
    if (!choice) await new Promise(r => setTimeout(r, 100));
  }
  if (!choice) return { ok: false, error: `option "${option}" never appeared` };
  for (const type of ['mousedown', 'mouseup', 'click']) choice.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true }));
  await new Promise(r => setTimeout(r, 150));
  return { ok: box.value === option, readback: box.value };
}
