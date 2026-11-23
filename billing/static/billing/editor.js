/* Live totals and line amounts come from the server's billing arithmetic; the browser does no money math. */
(() => {
  const form = document.getElementById('draft-editor');
  if (!form) return;
  const total = document.getElementById('id_lines-TOTAL_FORMS');
  const list = document.getElementById('line-forms');
  const undo = document.getElementById('line-undo');
  form.classList.add('js-lines');
  let lastRemoved = null;

  const rows = () => [...list.querySelectorAll('[data-line]')];
  const visibleRows = () => rows().filter(r => !r.classList.contains('is-removed'));
  const field = (row, name) => row.querySelector(`[name$="-${name}"]`);
  const prefixOf = row => field(row, 'description').name.replace(/-description$/, '');
  // Show the discount fields when the line already has one (or an error in it).
  const hasDiscount = row => {
    const amount = (field(row, 'discount').value || '').replace(/[$,\s]/g, '');
    return (amount !== '' && Number(amount) !== 0) || field(row, 'discount_label').value.trim() !== '' || !!row.querySelector('.lc-discount .errorlist');
  };
  function setDiscount(row, open) {
    row.classList.toggle('has-discount', open);
    row.querySelector('[data-toggle-discount]').setAttribute('aria-expanded', String(open));
  }
  function renumber() {
    visibleRows().forEach((row, i) => {
      const n = i + 1;
      row.setAttribute('aria-label', `Line ${n}`);
      row.querySelector('[data-remove-line]').setAttribute('aria-label', `Remove line ${n}`);
      row.querySelector('[data-line-amount]').setAttribute('aria-label', `Line ${n} amount`);
    });
  }
  rows().forEach(row => {
    setDiscount(row, hasDiscount(row));
    if (field(row, 'DELETE') && field(row, 'DELETE').checked) row.classList.add('is-removed');
  });
  renumber();

  document.getElementById('add-line').addEventListener('click', () => {
    const count = Number(total.value);
    if (count >= 200) return;
    const template = document.getElementById('empty-line');
    const container = document.createElement('div');
    container.innerHTML = template.innerHTML.replaceAll('__prefix__', String(count));
    const row = container.querySelector('[data-line]');
    list.append(row);
    total.value = String(count + 1);
    renumber();
    field(row, 'product').focus();
  });

  form.addEventListener('click', event => {
    const button = event.target.closest('button[type=button]');
    if (!button) return;
    const row = button.closest('[data-line]');
    if (button.matches('[data-toggle-discount]')) {
      setDiscount(row, true);
      field(row, 'discount').focus();
    } else if (button.matches('[data-clear-discount]')) {
      field(row, 'discount').value = '';
      field(row, 'discount_label').value = '';
      setDiscount(row, false);
      row.querySelector('[data-toggle-discount]').focus();
      schedule();
    } else if (button.matches('[data-remove-line]')) {
      if (visibleRows().length === 1) {
        // An invoice needs at least one line: clear it instead of removing it.
        ['product', 'description', 'rate', 'discount', 'discount_label'].forEach(n => { field(row, n).value = ''; });
        field(row, 'quantity').value = '1';
        field(row, 'taxable').checked = false;
        setDiscount(row, false);
        field(row, 'product').focus();
      } else {
        field(row, 'DELETE').checked = true;
        row.classList.add('is-removed');
        lastRemoved = row;
        const text = field(row, 'description').value.trim();
        undo.querySelector('span').textContent = text ? `Removed “${text}”.` : 'Line removed.';
        undo.hidden = false;
        undo.querySelector('[data-undo-remove]').focus();
        renumber();
      }
      schedule();
    } else if (button.matches('[data-undo-remove]') && lastRemoved) {
      field(lastRemoved, 'DELETE').checked = false;
      lastRemoved.classList.remove('is-removed');
      field(lastRemoved, 'description').focus();
      lastRemoved = null;
      undo.hidden = true;
      renumber();
      schedule();
    }
  });

  let timer, pending, version = 0;
  function showAmounts(map) {
    rows().forEach(row => { row.querySelector('[data-line-amount]').textContent = (map && map[prefixOf(row)]) || '—'; });
  }
  async function preview() {
    const current = ++version;
    if (pending) pending.abort();
    pending = new AbortController();
    const data = new FormData(form);
    data.set('action', 'preview');
    try {
      const response = await fetch(form.dataset.previewUrl, {method:'POST', body:data, credentials:'same-origin', signal:pending.signal, headers:{'X-Requested-With':'XMLHttpRequest'}});
      if (response.ok && current === version && !response.redirected) {
        const box = document.getElementById('live-totals');
        box.innerHTML = await response.text();
        const amounts = box.querySelector('#line-amounts');
        showAmounts(amounts ? JSON.parse(amounts.textContent) : null);
      }
    } catch (error) {
      if (error.name !== 'AbortError') document.getElementById('live-totals').textContent = 'Preview unavailable. Save or try again.';
    }
  }
  function schedule() { clearTimeout(timer); timer = setTimeout(preview, 350); }

  const products = JSON.parse(document.getElementById('editor-products').textContent);
  form.addEventListener('change', event => {
    if (!event.target.name.endsWith('-product')) return;
    const product = products.find(p => String(p.id) === event.target.value);
    if (!product) return;
    const row = event.target.closest('[data-line]');
    field(row, 'description').value = product.description;
    field(row, 'rate').value = product.rate.replace(/^\$/, '');
    field(row, 'taxable').checked = product.taxable;
    preview();
  });
  form.addEventListener('input', schedule);
  preview();
})();
