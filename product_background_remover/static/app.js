const state = { products: [], selected: null, visualSignature: '' };
const $ = (selector) => document.querySelector(selector);

async function request(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `Errore ${response.status}`);
  return response.json();
}
function notice(text, error = false) { const el = $('#notice'); el.textContent = text; el.classList.toggle('error', error); }
function addImage(container, image) {
  const original = container.querySelector('.original-image');
  const processed = container.querySelector('.processed-image');
  original.loading = 'lazy'; original.decoding = 'async';
  original.src = image.original_local_url || image.original_url;
  if (image.processed_local_url) {
    processed.loading = 'lazy'; processed.decoding = 'async';
    processed.src = image.processed_local_url;
    container.classList.add('has-processed');
  } else {
    processed.remove();
  }
  container.querySelector('.empty-image').remove();
}
function action(button, callback) {
  button.addEventListener('click', async () => { const before = button.textContent; button.disabled = true; button.textContent = 'Attendi…'; try { await callback(); await refresh(); button.textContent = '✓ Fatto'; setTimeout(() => button.textContent = before, 1200); } catch (error) { notice(error.message, true); button.textContent = 'Errore'; setTimeout(() => button.textContent = before, 1600); } finally { button.disabled = false; } });
}
function matchesFilters(product) {
  const selectedStatus = $('#status-filter').value;
  const hasProcessedImage = product.images.some(image => image.processing_status === 'completed');
  const hasUploadedImage = product.images.some(image => Boolean(image.uploaded_at));
  const wantsUploaded = $('#uploaded-filter').checked;
  const wantsNotUploaded = $('#not-uploaded-filter').checked;
  const uploadMatches = !wantsUploaded && !wantsNotUploaded || wantsUploaded && hasUploadedImage || wantsNotUploaded && !hasUploadedImage;
  return (!selectedStatus || product.status === selectedStatus) && (!$('#processed-filter').checked || hasProcessedImage) && uploadMatches;
}
function render() {
  const grid = $('#product-grid'); grid.replaceChildren();
  if (!state.products.length) { grid.textContent = 'Nessun prodotto locale: premi “Sincronizza prodotti”.'; $('#filter-count').textContent = ''; return; }
  const visibleProducts = state.products.filter(matchesFilters);
  $('#filter-count').textContent = `${visibleProducts.length.toLocaleString('it-IT')} di ${state.products.length.toLocaleString('it-IT')} prodotti`;
  if (!visibleProducts.length) { grid.textContent = 'Nessun prodotto corrisponde ai filtri selezionati.'; return; }
  visibleProducts.forEach(product => {
    const card = $('#product-template').content.cloneNode(true); const root = card.querySelector('article');
    const primary = product.images[0]; if (primary) addImage(root.querySelector('.image-comparison'), primary); else { root.querySelector('.original-image').remove(); root.querySelector('.processed-image').remove(); }
    root.querySelector('h2').textContent = product.title; const badge = root.querySelector('.status-badge'); badge.textContent = product.status; badge.classList.toggle('active', product.status === 'active');
    root.querySelector('.upload-badge').hidden = !product.images.some(image => Boolean(image.uploaded_at));
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
  product.images.forEach(image => { const card = $('#photo-template').content.cloneNode(true); const root = card.querySelector('article'); addImage(root.querySelector('.image-comparison'), image); root.querySelector('.photo-state').textContent = ({new:'DA PROCESSARE',queued:'IN CODA',processing:'IN ELABORAZIONE',completed:'COMPLETATO',error:'ERRORE'})[image.processing_status] || image.processing_status; root.querySelector('.photo-state').classList.add(image.processing_status); const uploadBadge = root.querySelector('.photo-upload-badge'); uploadBadge.hidden = !image.uploaded_at; if (!image.uploaded_at && ['queued', 'uploading', 'error'].includes(image.upload_status)) { uploadBadge.hidden = false; uploadBadge.textContent = ({queued:'Upload in coda',uploading:'Upload Shopify…',error:'Errore upload'})[image.upload_status]; uploadBadge.classList.toggle('error', image.upload_status === 'error'); } root.querySelector('.photo-error').textContent = image.processing_error || image.upload_error || ''; action(root.querySelector('.process-button'), () => request(`/api/products/${id}/images/${image.id}/process`, {method:'POST'})); action(root.querySelector('.upload-button'), () => request(`/api/products/${id}/images/${image.id}/upload`, {method:'POST'})); grid.append(root); });
  if (show && !$('#photo-modal').open) $('#photo-modal').showModal();
}
function buildVisualSignature(products) {
  return products.map(product => `${product.id}:${product.processed_count}:${product.images.map(image => `${image.id}:${image.processing_status}:${image.processed_local_url || ''}:${image.uploaded_at || ''}:${image.upload_status || ''}`).join(',')}`).join('|');
}
async function refresh() { try { const [products, queue, uploadQueue] = await Promise.all([request('/api/products'), request('/api/queue'), request('/api/upload_queue')]); const signature = buildVisualSignature(products); state.products = products; if (signature !== state.visualSignature) { state.visualSignature = signature; render(); } const processing = queue.filter(job => job.status === 'queued' || job.status === 'processing'); const uploads = uploadQueue.filter(job => job.status === 'queued' || job.status === 'uploading'); const fragments = []; if (processing.length) fragments.push(`Elaborazione: ${processing.filter(j => j.status === 'processing').length} attiva · ${processing.filter(j => j.status === 'queued').length} in attesa`); if (uploads.length) fragments.push(`Upload Shopify: ${uploads.filter(j => j.status === 'uploading').length} attivo · ${uploads.filter(j => j.status === 'queued').length} in attesa`); $('#queue-summary').textContent = fragments.join('   |   '); } catch (error) { notice(error.message, true); } }
$('#sync-button').addEventListener('click', async event => { const button = event.currentTarget; button.disabled = true; button.textContent = 'Sincronizzazione…'; try { const result = await request('/api/sync', {method:'POST'}); notice(`Catalogo sincronizzato: ${result.synced} prodotti.`); await refresh(); } catch (error) { notice(error.message, true); } finally { button.disabled = false; button.textContent = '↻ Sincronizza prodotti'; } });
$('#process-unprocessed-button').addEventListener('click', async event => { const button = event.currentTarget; button.disabled = true; button.textContent = 'Messa in coda…'; try { const result = await request('/api/process_unprocessed_products', {method:'POST'}); notice(`${result.queued.toLocaleString('it-IT')} foto di ${result.products.toLocaleString('it-IT')} prodotti aggiunte alla coda.`); await refresh(); } catch (error) { notice(error.message, true); } finally { button.disabled = false; button.textContent = 'Processa prodotti senza foto processate'; } });
document.querySelectorAll('#status-filter, #processed-filter, #uploaded-filter, #not-uploaded-filter').forEach(control => control.addEventListener('change', render));
$('#close-modal').addEventListener('click', () => { $('#photo-modal').close(); state.selected = null; });
$('#photo-modal').addEventListener('click', event => { if (event.target === $('#photo-modal')) { $('#photo-modal').close(); state.selected = null; } });
refresh(); setInterval(refresh, 2500);
