/* Small, bounded views: one list page or one cluster layer at a time. */
'use strict';

const $ = (selector) => document.querySelector(selector);
const initialParams = new URLSearchParams(location.search);
const state = {
  view: document.body.dataset.view,
  query: $('#query').value.trim(), field: $('#field').value,
  page: Math.max(1, Number(initialParams.get('page')) || 1), totalPages: 0,
  area: initialParams.get('bbox') || null, controller: null, mapTimer: null, ignoreMapMove: false, popupPan: false,
  map: null, clusters: null, mapPromise: null, detailController: null,
};
const money = (value) => value == null ? 'Not reported' : new Intl.NumberFormat('en-US', {
  style: 'currency', currency: 'USD', maximumFractionDigits: 0,
}).format(value);
const compactMoney = (value) => value == null ? 'Not reported' : new Intl.NumberFormat('en-US', {
  style: 'currency', currency: 'USD', notation: 'compact', maximumFractionDigits: 1,
}).format(value);
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function notice(message = '') {
  $('#notice').textContent = message;
  $('#notice').hidden = !message;
}
async function fetchJSON(url, signal) {
  const response = await fetch(url, { signal, headers: { Accept: 'application/json' } });
  if (!response.headers.get('content-type')?.includes('application/json')) {
    throw new Error('The server could not complete this request. Please try again.');
  }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Something went wrong. Please try again.');
  return data;
}
function updateURL(replace = false) {
  const params = new URLSearchParams();
  if (state.query) { params.set('q', state.query); params.set('field', state.field); }
  if (state.query && state.view === 'list' && state.page > 1) params.set('page', state.page);
  if (state.query && state.area) params.set('bbox', state.area);
  const url = (state.view === 'map' ? '/map' : '/') + (params.size ? `?${params}` : '');
  if (url !== location.pathname + location.search) history[replace ? 'replaceState' : 'pushState']({}, '', url);
  $('#search-form').action = state.view === 'map' ? '/map' : '/';
}
function updateView() {
  updateSearchHint();
  document.body.classList.toggle('has-query', !!state.query);
  $('#list-view').setAttribute('aria-pressed', state.view === 'list');
  $('#map-view').setAttribute('aria-pressed', state.view === 'map');
  $('#map-panel').hidden = state.view !== 'map';
  $('#results').hidden = state.view !== 'list' || !state.query;
  $('#welcome').hidden = !!state.query || state.view === 'map';
  document.querySelectorAll('.pagination').forEach((bar) => { bar.hidden = true; });
  $('#area-filter').hidden = !state.area || !state.query;
  $('#results-heading').textContent = state.query
    ? state.field === 'zip_code' ? `Mailing ZIP “${state.query}”` : `Results for “${state.query}”`
    : 'Discover the bigger picture';
}
function updateSearchHint() {
  const zip = $('#field').value === 'zip_code';
  $('#search-hint').hidden = !zip;
  $('#query').placeholder = zip ? 'e.g. 34947 or 34947-2528' : 'Search an organization, person, EIN, or location…';
  $('#query').inputMode = zip ? 'numeric' : 'search';
  if (zip) $('#query').setAttribute('aria-describedby', 'search-hint');
  else $('#query').removeAttribute('aria-describedby');
}
$('#field').addEventListener('change', updateSearchHint);
function card(row) {
  const article = element('article', 'filing-card');
  const top = element('div', 'card-top');
  const icon = element('span', 'organization-icon', '▤');
  icon.setAttribute('aria-hidden', 'true');
  top.append(icon, element('span', 'tax-year', row.tax_year ? `TAX YEAR ${row.tax_year}` : 'YEAR NOT REPORTED'));
  const funding = element('div', 'card-funding');
  const amount = element('div');
  amount.append(element('small', '', 'Government funding'), element('strong', '', compactMoney(row.govt_amt)));
  amount.title = money(row.govt_amt);
  const button = element('button', '', 'View filing ↗');
  button.type = 'button';
  button.setAttribute('aria-label', `View filing for ${row.filer_name || 'organization'}`);
  button.addEventListener('click', () => showDetail(row.id));
  funding.append(amount, button);
  article.append(top, element('h3', '', row.filer_name || 'Unnamed organization'),
    element('p', 'card-address', row.corp_address || 'Address not reported'), funding);
  return article;
}
function renderList(data) {
  const fragment = document.createDocumentFragment();
  data.results.forEach((row) => fragment.append(card(row)));
  if (!data.results.length) {
    const empty = element('div', 'empty-state');
    empty.append(element('h3', '', 'No filings found'), element('p', '', 'Try a shorter name, a different search field, or a ZIP code.'));
    fragment.append(empty);
  }
  $('#results').replaceChildren(fragment);
  state.page = data.page;
  state.totalPages = data.total_pages;
  updateURL(true);
  renderPagination(data);
  $('#results-status').textContent = data.results.length
    ? `${data.total.toLocaleString()} matching filings · Showing ${data.start.toLocaleString()}–${data.end.toLocaleString()} · Page ${data.page.toLocaleString()} of ${data.total_pages.toLocaleString()}`
    : 'No matching filings in this dataset.';
}
function renderPagination(data) {
  const pages = new Set([1, data.total_pages]);
  if (data.total_pages <= 9) {
    for (let page = 1; page <= data.total_pages; page++) pages.add(page);
  } else {
    const start = Math.max(1, Math.min(data.page - 2, data.total_pages - 4));
    for (let page = start; page <= Math.min(data.total_pages, start + 4); page++) pages.add(page);
  }
  document.querySelectorAll('.pagination').forEach((bar) => {
    bar.hidden = !data.total;
    const summary = element('span', 'page-summary', `Page ${data.page.toLocaleString()} of ${data.total_pages.toLocaleString()}`);
    summary.id = bar.id === 'pagination' ? 'page-label' : 'page-label-top';
    const navigation = element('div', 'page-numbers');
    function pageButton(label, page, disabled = false) {
      const button = element('button', 'page-button', label); button.type = 'button';
      button.dataset.page = page; button.disabled = disabled; return button;
    }
    const previous = pageButton('←', data.page - 1, data.page <= 1);
    previous.id = bar.id === 'pagination' ? 'previous-page' : 'previous-page-top';
    previous.setAttribute('aria-label', 'Previous page'); navigation.append(previous);
    let prior = 0;
    for (const page of [...pages].sort((a, b) => a - b)) {
      if (page < 1) continue;
      if (prior && page > prior + 1) navigation.append(element('span', 'page-gap', '…'));
      const button = pageButton(String(page), page);
      button.setAttribute('aria-label', `Page ${page}`);
      if (page === data.page) button.setAttribute('aria-current', 'page');
      navigation.append(button); prior = page;
    }
    const next = pageButton('→', data.page + 1, data.page >= data.total_pages);
    next.id = bar.id === 'pagination' ? 'next-page' : 'next-page-top';
    next.setAttribute('aria-label', 'Next page'); navigation.append(next);
    const jump = element('form', 'page-jump');
    const label = element('label', '', 'Go to page');
    const input = element('input'); input.type = 'number'; input.min = 1; input.max = Math.max(1, data.total_pages);
    input.value = data.page; input.required = true; input.name = 'page';
    input.id = `${bar.id}-jump`; label.htmlFor = input.id;
    const go = element('button', 'button button-secondary', 'Go'); go.type = 'submit';
    jump.append(label, input, go); bar.replaceChildren(summary, navigation, jump);
  });
}
function goToPage(page) {
  if (!Number.isInteger(page) || page < 1 || page > state.totalPages || page === state.page) return;
  state.page = page; updateURL(); runSearch();
  $('.results-toolbar').scrollIntoView({ block: 'start' });
}
document.querySelectorAll('.pagination').forEach((bar) => {
  bar.addEventListener('click', (event) => {
    const button = event.target.closest('[data-page]');
    if (button && !button.disabled) goToPage(Number(button.dataset.page));
  });
  bar.addEventListener('submit', (event) => {
    event.preventDefault(); goToPage(Number(new FormData(event.target).get('page')));
  });
});
function addScript(src) {
  return new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = src;
    script.onload = resolve;
    script.onerror = () => { script.remove(); reject(new Error('The map could not load. Please retry or use List view.')); };
    document.head.append(script);
  });
}
async function ensureMap() {
  if (state.map) {
    state.ignoreMapMove = true; state.map.invalidateSize(); state.ignoreMapMove = false; return;
  }
  if (state.mapPromise) return state.mapPromise;
  state.mapPromise = (async () => {
    for (const href of ['/static/vendor/leaflet.css']) {
      if (!document.querySelector(`link[href="${href}"]`)) {
        const link = document.createElement('link');
        link.rel = 'stylesheet'; link.href = href; document.head.append(link);
      }
    }
    if (!window.L) await addScript('/static/vendor/leaflet.js');
    state.map = L.map('map', { scrollWheelZoom: false, minZoom: 2, maxZoom: 18,
      maxBounds: [[-85, -180], [85, 180]], maxBoundsViscosity: 1,
    }).setView([38.5, -97], 4);
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19, noWrap: true,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(state.map);
    state.map.attributionControl.addAttribution('Postal estimates: <a href="https://www.geonames.org/" target="_blank" rel="noopener noreferrer">GeoNames</a> · <a href="https://creativecommons.org/licenses/by/4.0/" target="_blank" rel="noopener noreferrer">CC BY 4.0</a>');
    state.clusters = L.layerGroup();
    state.map.addLayer(state.clusters);
    L.control.scale({ imperial: true, metric: false }).addTo(state.map);
    state.map.on('autopanstart', () => { state.popupPan = true; });
    state.map.on('moveend', () => {
      // Leaflet may pan to keep a popup readable. Preserve its controls until
      // the user chooses an action instead of replacing them with a new layer.
      if (state.popupPan) { state.popupPan = false; return; }
      if (state.ignoreMapMove || state.view !== 'map' || !state.query) return;
      clearTimeout(state.mapTimer);
      state.mapTimer = setTimeout(() => {
        if (state.view === 'map' && state.query) runSearch({ bbox: mapArea(), fit: false });
      }, 180);
    });
  })();
  try { await state.mapPromise; } finally { state.mapPromise = null; }
}
function renderMap(data, fit) {
  // Every matching point contributes to a server cluster. Only the bounded
  // cluster/single-marker representation reaches Leaflet.
  state.clusters.clearLayers();
  const markers = data.results.map((row) => {
    if (row.kind === 'cluster') {
      const label = element('span', '', row.count.toLocaleString());
      const marker = L.marker([row.lat, row.lon], {
        title: `${row.count.toLocaleString()} filings · click to explore`,
        zIndexOffset: 1000 + Math.round(Math.log10(row.count) * 1000),
        icon: L.divIcon({ html: label, className: 'glow-cluster', iconSize: [48, 48] }),
      });
      marker.bindPopup(() => {
        const content = element('div', 'map-popup');
        const button = element('button', '', 'View these filings →'); button.type = 'button';
        button.addEventListener('click', () => listArea(row.bounds.join(',')));
        content.append(element('strong', '', `${row.count.toLocaleString()} filings in this cluster`),
          element('p', '', 'Zoom in to separate locations, or open the complete paginated list.'));
        const [west, south, east, north] = row.bounds;
        if ((west !== east || south !== north) && state.map.getZoom() < 18) {
          const zoomButton = element('button', '', 'Zoom in'); zoomButton.type = 'button';
          zoomButton.addEventListener('click', () => {
            const bounds = L.latLngBounds([[south, west], [north, east]]);
            const zoom = Math.min(18, Math.max(state.map.getZoom() + 1, state.map.getBoundsZoom(bounds, false, [50, 50])));
            state.map.closePopup();
            state.map.setView(bounds.getCenter(), zoom, { animate: false });
          });
          content.append(zoomButton, document.createTextNode(' · '));
        }
        content.append(button);
        return content;
      });
      return marker;
    }
    const marker = L.marker([row.lat, row.lon], { title: row.filer_name || 'Organization',
      icon: L.divIcon({ className: (row.location_precision === 'postal' || row.location_precision === 'city') ? 'glow-pin glow-pin-approximate' : 'glow-pin', iconSize: [15, 15] }),
    });
    marker.bindPopup(() => {
      const content = element('div', 'map-popup');
      const button = element('button', '', 'View filing ↗');
      button.type = 'button'; button.addEventListener('click', () => showDetail(row.id));
      content.append(element('strong', '', row.filer_name || 'Unnamed organization'),
        element('p', '', row.corp_address || 'Address not reported'),
        element('p', '', `${money(row.govt_amt)} government funding · ${row.tax_year || 'Year not reported'}`), button);
      content.insertBefore(element('p', 'location-quality', row.location_precision === 'postal'
        ? 'Approximate postal area — not a building location'
        : row.location_precision === 'city' ? 'Approximate city area — not a building location'
        : row.location_precision === 'street' ? 'Street address · Census estimate' : 'Original dataset coordinates · unverified'), button);
      return content;
    });
    return marker;
  });
  markers.forEach((marker) => state.clusters.addLayer(marker));
  if (data.bounds && fit) {
    const [west, south, east, north] = data.bounds;
    state.ignoreMapMove = true;
    state.map.fitBounds([[south, west], [north, east]], { padding: [40, 40], maxZoom: 13, animate: false });
    state.ignoreMapMove = false;
  }
  $('#map-count').textContent = data.visible_count === data.total_mapped
    ? `All ${data.total_mapped.toLocaleString()} mapped filings represented`
    : `${data.visible_count.toLocaleString()} of ${data.total_mapped.toLocaleString()} mapped filings in this view`;
  if (data.approximate_count) $('#map-count').textContent += ` · ${data.approximate_count.toLocaleString()} postal-area estimates`;
  $('#results-status').textContent = `${data.total_matching.toLocaleString()} matching filings · ${data.total_mapped.toLocaleString()} have map locations${data.missing_coordinates ? ` · ${data.missing_coordinates.toLocaleString()} without usable map coordinates` : ''}. Zoom or open a cluster to explore all results.`;
}
function mapArea() {
  const bounds = state.map.getBounds();
  return [Math.max(-180, bounds.getWest()), Math.max(-90, bounds.getSouth()),
    Math.min(180, bounds.getEast()), Math.min(90, bounds.getNorth())].join(',');
}
function listArea(bbox) {
  state.area = bbox; state.page = 1; state.view = 'list'; updateURL(); runSearch();
}
async function runSearch({ bbox = state.area, fit = true } = {}) {
  clearTimeout(state.mapTimer);
  state.controller?.abort();
  const controller = new AbortController();
  state.controller = controller;
  notice(); updateView();
  $('#results').setAttribute('aria-busy', 'true');
  $('#search-area').disabled = true;
  $('#reset-map').disabled = true;
  document.querySelectorAll('.pagination button, .pagination input').forEach((control) => { control.disabled = true; });
  try {
    if (state.view === 'map') {
      await ensureMap();
      if (controller.signal.aborted) return;
      if (fit) state.clusters.clearLayers();
    }
    if (!state.query) {
      $('#results').replaceChildren();
      $('#results-status').textContent = 'Search to explore the organizations in your dataset.';
      $('#map-count').textContent = 'Search for a place or organization to add locations.';
      return;
    }
    $('#results-status').textContent = 'Searching your dataset…';
    if (state.view === 'list') $('#results').replaceChildren(...Array.from({ length: 6 }, () => {
      const skeleton = element('div', 'skeleton'); skeleton.setAttribute('aria-hidden', 'true'); return skeleton;
    }));
    const params = new URLSearchParams({ q: state.query, field: state.field });
    if (bbox) params.set('bbox', bbox);
    if (state.view === 'list') params.set('page', state.page);
    const data = await fetchJSON(`/api/${state.view === 'map' ? 'map' : 'search'}?${params}`, controller.signal);
    if (controller.signal.aborted) return;
    if (state.view === 'map') renderMap(data, fit); else renderList(data);
  } catch (error) {
    if (controller.signal.aborted) return;
    $('#results').replaceChildren();
    $('#results-status').textContent = 'Search could not be completed.';
    if (state.view === 'map') $('#map-count').textContent = 'No locations loaded. Retry your search.';
    notice(error.message || 'Unable to connect. Check that GlowSearch is running and retry.');
  } finally {
    if (state.controller === controller) {
      $('#results').setAttribute('aria-busy', 'false');
      $('#search-area').disabled = !state.query;
      $('#reset-map').disabled = !state.query;
    }
  }
}
function beginSearch(query, field, view = state.view) {
  state.query = query.trim(); state.field = field; state.view = view;
  state.page = 1; state.totalPages = 0; state.area = null;
  $('#query').value = state.query; $('#field').value = field;
  updateURL(); runSearch();
}
$('#search-form').addEventListener('submit', (event) => {
  event.preventDefault(); beginSearch($('#query').value, $('#field').value);
});
document.querySelectorAll('[data-query]').forEach((button) => button.addEventListener('click', () => {
  beginSearch(button.dataset.query, button.dataset.field, button.dataset.view || state.view);
}));
for (const view of ['list', 'map']) $(`#${view}-view`).addEventListener('click', () => {
  if (state.view === view) return;
  state.view = view; updateURL(); runSearch();
});
$('#search-area').addEventListener('click', () => {
  listArea(mapArea());
});
function clearArea() { state.area = null; state.page = 1; updateURL(); runSearch(); }
$('#reset-map').addEventListener('click', clearArea);
$('#clear-area').addEventListener('click', clearArea);
$('#start-search').addEventListener('click', () => $('#query').focus());
$('#find-person').addEventListener('click', () => { $('#field').value = 'officials'; updateSearchHint(); $('#query').focus(); });
document.addEventListener('keydown', (event) => {
  if (event.key === '/' && !/INPUT|TEXTAREA|SELECT/.test(event.target.tagName) && !$('#filing-dialog').open) {
    event.preventDefault(); $('#query').focus();
  }
});
window.addEventListener('popstate', () => {
  const params = new URLSearchParams(location.search);
  state.query = params.get('q') || ''; state.field = params.get('field') || 'all';
  state.view = location.pathname === '/map' ? 'map' : 'list';
  state.page = Math.max(1, Number(params.get('page')) || 1); state.area = params.get('bbox') || null;
  $('#query').value = state.query; $('#field').value = state.field; runSearch();
});

async function showDetail(id) {
  state.detailController?.abort();
  const controller = new AbortController(); state.detailController = controller;
  const dialog = $('#filing-dialog');
  $('#detail-content').replaceChildren(element('h2', '', 'Loading filing…'));
  $('#detail-content').firstChild.id = 'detail-title';
  if (!dialog.open) dialog.showModal();
  try {
    const row = await fetchJSON(`/api/filings/${id}`, controller.signal);
    if (controller.signal.aborted) return;
    const title = element('h2', '', row.filer_name || 'Unnamed organization'); title.id = 'detail-title';
    const metrics = element('div', 'detail-metrics');
    for (const [label, value] of [['Government funding', row.govt_amt], ['Total receipts', row.receipt_amt], ['Contributions', row.contrib_amt]]) {
      const metric = element('div'); metric.append(element('small', '', label), element('strong', '', money(value))); metrics.append(metric);
    }
    const content = [title, element('p', '', `EIN ${row.filer_ein || 'not reported'} · Tax year ${row.tax_year || 'not reported'}`),
      element('p', '', row.corp_address || 'Address not reported'), metrics];
    if (row.corp_description) content.push(element('p', '', row.corp_description));
    if (row.location) {
      const location = element('section', 'detail-section');
      location.append(element('h3', '', 'Map location'), element('p', 'location-quality', row.location.label));
      if (row.location.matched_address) location.append(element('p', '', `Mapped address: ${row.location.matched_address}`));
      if (row.location.lat != null) location.append(element('p', '', `${row.location.lat.toFixed(6)}, ${row.location.lon.toFixed(6)}`));
      if (row.location.reason) location.append(element('p', '', row.location.reason));
      for (const [label, value] of [['Coordinate source ↗', row.location.source_url], ['Address source ↗', row.location.address_source_url]]) {
        if (!value || !/^https:\/\//.test(value)) continue;
        const link = element('a', 'text-button', label); link.href = value; link.target = '_blank'; link.rel = 'noopener noreferrer'; location.append(link);
      }
      content.push(location);
    }
    const officers = element('section', 'detail-section');
    officers.append(element('h3', '', 'Officers in this filing'));
    const list = element('ul');
    row.officers.forEach((person) => list.append(element('li', '', `${person.name || 'Name not reported'}${person.title ? ` — ${person.title}` : ''}`)));
    officers.append(row.officers.length ? list : element('p', '', 'No officers listed.'));
    if (row.officers_truncated) officers.append(element('p', '', 'Showing the first 100 officers. See the original filing for the complete list.'));
    content.push(officers);
    for (const [label, data] of [['Founder', row.ceo_home1], ['Related organization', row.shell_game]]) {
      const values = [data.name || data.corp_name, data.corp_ein, data.address, data.phone].filter(Boolean);
      if (values.length) {
        const section = element('section', 'detail-section'); section.append(element('h3', '', label));
        values.forEach((value) => section.append(element('p', '', String(value)))); content.push(section);
      }
    }
    const links = element('div', 'detail-links');
    for (const [label, url] of [['Organization source ↗', row.source_url], ['Original filing ↗', row.filing_url], ['Connections graph ↗', row.graph_url]]) {
      if (!url) continue;
      const link = element('a', 'button button-secondary', label); link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; links.append(link);
    }
    content.push(links); $('#detail-content').replaceChildren(...content);
  } catch (error) {
    if (controller.signal.aborted) return;
    const title = element('h2', '', 'Unable to load filing'); title.id = 'detail-title';
    $('#detail-content').replaceChildren(title, element('p', '', error.message));
  }
}
$('#close-dialog').addEventListener('click', () => $('#filing-dialog').close());
$('#filing-dialog').addEventListener('close', () => state.detailController?.abort());
$('#filing-dialog').addEventListener('click', (event) => {
  if (event.target !== $('#filing-dialog')) return;
  const box = event.target.getBoundingClientRect();
  if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) event.target.close();
});
async function checkStatus() {
  try {
    const data = await fetchJSON('/api/status');
    $('#database-status').classList.toggle('ready', data.ready);
    $('#connection-label').textContent = data.ready ? 'Local dataset connected' : 'Waiting for database';
    if (!data.ready && !state.query) notice(data.message);
  } catch { $('#connection-label').textContent = 'Connection unavailable'; }
}
checkStatus(); runSearch();
window.addEventListener('focus', checkStatus);
