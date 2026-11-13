/* Live totals come from the server's billing arithmetic; the browser does no money math. */
(() => {
  const form = document.getElementById('draft-editor');
  if (!form) return;
  const total = document.getElementById('id_lines-TOTAL_FORMS');
  document.getElementById('add-line').addEventListener('click', () => {
    const count = Number(total.value);
    if (count >= 200) return;
    const template = document.getElementById('empty-line');
    const container = document.createElement('div');
    container.innerHTML = template.innerHTML.replaceAll('__prefix__', String(count));
    document.getElementById('line-forms').append(...container.children);
    total.value = String(count + 1);
  });
  let timer, pending, version = 0;
  async function preview() {
    const current = ++version;
    if (pending) pending.abort();
    pending = new AbortController();
    const data = new FormData(form);
    data.set('action', 'preview');
    try {
      const response = await fetch(form.dataset.previewUrl, {method:'POST', body:data, credentials:'same-origin', signal:pending.signal, headers:{'X-Requested-With':'XMLHttpRequest'}});
      if (response.ok && current === version && !response.redirected) {
        document.getElementById('live-totals').innerHTML = await response.text();
      }
    } catch (error) {
      if (error.name !== 'AbortError') document.getElementById('live-totals').textContent = 'Preview unavailable. Save or try again.';
    }
  }
  const products = JSON.parse(document.getElementById('editor-products').textContent);
  form.addEventListener('change', event => {
    if (!event.target.name.endsWith('-product')) return;
    const product = products.find(p => String(p.id) === event.target.value);
    if (!product) return;
    const row = event.target.closest('.line-form');
    row.querySelector('[name$="-description"]').value = product.description;
    row.querySelector('[name$="-rate"]').value = product.rate;
    row.querySelector('[name$="-taxable"]').checked = product.taxable;
    preview();
  });
  form.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(preview, 350); });
  preview();
})();
