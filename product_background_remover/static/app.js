const state = { products: [], selected: null };
const $ = (selector) => document.querySelector(selector);

async function request(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `Errore ${response.status}`);
  return response.json();
}
function notice(text, error = false) { const el = $('#notice'); el.textContent = text; el.classList.toggle('error', error); }
function imageSource(image) { return image.processed_local_url || image.original_local_url || image.original_url; }
function addImage(container, image, className) {
  const tag = container.querySelector(className);
  tag.src = imageSource(image); tag.alt = 'Foto prodotto';
  if (image.processed_local_url && (image.original_local_url || image.original_url)) {
    tag.classList.add('processed'); container.classList.add('has-original');
    tag.addEventListener('mouseenter', () => { tag.dataset.processed = tag.src; tag.src = image.original_local_url || image.original_url; });
    tag.addEventListener('mouseleave', () => { tag.src = tag.dataset.processed; });
  }
}
function action(button, callback) {
  button.addEventListener('click', async () => { const before = button.textContent; button.disabled = true; button.textContent = 'Attendi…'; try { await callback(); await refresh(); button.textContent = '✓ Fatto'; setTimeout(() => button.textContent = before, 1200); } catch (error) { notice(error.message, true); button.textContent = 'Errore'; setTimeout(() => button.textContent = before, 1600); } finally { button.disabled = false; } });
}
function render() {
  const grid = $('#product-grid'); grid.replaceChildren();
  if (!state.products.length) { grid.textContent = 'Nessun prodotto locale: premi “Sincronizza prodotti”.'; return; }
  state.products.forEach(product => {
    const card = $('#product-template').content.cloneNode(true); const root = card.querySelector('article');
    const primary = product.images[0]; if (primary) addImage(root.querySelector('.image-comparison'), primary, '.product-image'); else root.querySelector('.product-image').remove();
    root.querySelector('h2').textContent = product.title; const badge = root.querySelector('.status-badge'); badge.textContent = product.status; badge.classList.toggle('active', product.status === 'active');
    root.querySelector('.counter').textContent = `Foto processate: ${product.processed_count}/${product.total_images}`;
    root.querySelector('.view-button').addEventListener('click', () => openModal(product.id));
    action(root.querySelector('.process-all'), () => request(`/api/products/${product.id}/process_all`, { method: 'POST' }));
    action(root.querySelector('.upload-all'), () => request(`/api/products/${product.id}/upload_all`, { method: 'POST' })); grid.append(root);
  });
  if (state.selected) openModal(state.selected, false);
}
function openModal(id, show = true) {
  state.selected = id; const product = state.products.find(item => item.id === id); if (!product) return;
  $('#modal-title').textContent = product.title; const grid = $('#modal-grid'); grid.replaceChildren();
  product.images.forEach(image => { const card = $('#photo-template').content.cloneNode(true); const root = card.querySelector('article'); addImage(root.querySelector('.image-comparison'), image, '.photo-image'); root.querySelector('.photo-state').textContent = ({new:'DA PROCESSARE',queued:'IN CODA',processing:'IN ELABORAZIONE',completed:'COMPLETATO',error:'ERRORE'})[image.processing_status] || image.processing_status; root.querySelector('.photo-state').classList.add(image.processing_status); root.querySelector('.photo-error').textContent = image.processing_error || ''; action(root.querySelector('.process-button'), () => request(`/api/products/${id}/images/${image.id}/process`, {method:'POST'})); action(root.querySelector('.upload-button'), () => request(`/api/products/${id}/images/${image.id}/upload`, {method:'POST'})); grid.append(root); });
  if (show && !$('#photo-modal').open) $('#photo-modal').showModal();
}
async function refresh() { try { state.products = await request('/api/products'); const queue = await request('/api/queue'); const active = queue.filter(job => job.status === 'queued' || job.status === 'processing'); $('#queue-summary').textContent = active.length ? `Coda: ${active.filter(j => j.status === 'processing').length} in elaborazione · ${active.filter(j => j.status === 'queued').length} in attesa` : ''; render(); } catch (error) { notice(error.message, true); } }
$('#sync-button').addEventListener('click', async event => { const button = event.currentTarget; button.disabled = true; button.textContent = 'Sincronizzazione…'; try { const result = await request('/api/sync', {method:'POST'}); notice(`Catalogo sincronizzato: ${result.synced} prodotti.`); await refresh(); } catch (error) { notice(error.message, true); } finally { button.disabled = false; button.textContent = '↻ Sincronizza prodotti'; } });
$('#close-modal').addEventListener('click', () => { $('#photo-modal').close(); state.selected = null; });
$('#photo-modal').addEventListener('click', event => { if (event.target === $('#photo-modal')) { $('#photo-modal').close(); state.selected = null; } });
refresh(); setInterval(refresh, 2500);
