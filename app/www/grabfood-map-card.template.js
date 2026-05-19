// GrabFood Tracker — Lovelace map card v0.2.3
// Displays home pin, active driver pins, and OSRM routes for all simultaneous orders.

const _esc = s => s == null ? '' : String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const _VERSION = '0.2.3';
const _ORDER_COLORS = ['#00B14F', '#FF6B35', '#4ECDC4', '#45B7D1', '#9B59B6'];
const _OSRM_BASE = 'https://router.project-osrm.org/route/v1/driving/';
const _LEAFLET_IMG = 'https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/images/';
const _TILE_URL = 'https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png';
// Exact dark filter from HA's ha-map.ts — applied to tile pane over standard light tiles.
const _DARK_FILTER = 'invert(0.9) hue-rotate(170deg) brightness(1.5) contrast(1.2) saturate(0.3)';
const _TILE_ATTR = '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions" target="_blank">CARTO</a>';

class GrabFoodMapCard extends HTMLElement {
  constructor() {
    super();
    this._map = null;
    this._mapDiv = null;
    this._infoDiv = null;
    this._tileLayer = null;
    this._homeMarker = null;
    this._driverMarkers = {};
    this._routeLines = {};
    this._routeCache = {};
    this._ready = false;
    this._pendingUpdate = false;
    this._viewInitialized = false;
    this._fittedOrderIds = new Set();
    this._showInfo = true;
    this._osrmBase = _OSRM_BASE;

    // Prevent Leaflet map interactions (zoom, pan, fit-all) from bubbling
    // out of the shadow DOM to HA's card layer. Shadow DOM composed events
    // are re-dispatched at the host element in the outer DOM, so HA sees
    // them here. Stopping propagation on the host keeps them inside the card.
    const _stop = e => e.stopPropagation();
    this.addEventListener('click', _stop);
    this.addEventListener('pointerdown', _stop);
    this.addEventListener('pointerup', _stop);
  }

  setConfig(config) {
    this._config = config;
    this._entity = config.entity || 'sensor.grabfood_orders';
    this._aspectRatio = config.aspect_ratio || null;
    this._height = config.height || 400;
    this._showInfo = config.show_info !== false;
    this._osrmBase = config.osrm_url || _OSRM_BASE;
    // If card is already built and hass is set, refresh the info header immediately.
    if (this._ready && this._hass) {
      this._renderInfo(this._hass.states[this._entity]?.attributes?.orders || []);
    }
  }

  _isDark(hass) {
    // hass.themes.darkMode is set when HA dark mode is active (including auto/follow-system).
    // Fall back to matchMedia for cases where themes.darkMode is undefined.
    return hass?.themes?.darkMode ?? window.matchMedia('(prefers-color-scheme: dark)').matches;
  }

  set hass(hass) {
    const prevDark = this._isDark(this._hass);
    this._hass = hass;
    if (!this._ready) {
      this._build();
    } else {
      if (this._map && this._isDark(hass) !== prevDark) {
        this._applyTiles(this._isDark(hass));
      }
      this._update();
    }
  }

  _build() {
    if (this._ready) return;
    this._ready = true;

    const shadow = this.attachShadow({ mode: 'open' });

    const style = document.createElement('style');
    // aspect_ratio applies to the MAP only, not the header.
    // :host is a flex column so the header stacks naturally above the map.
    const mapSize = this._aspectRatio
      ? `width: 100%; aspect-ratio: ${this._aspectRatio};`
      : `width: 100%; height: ${this._height}px;`;
    style.textContent = LEAFLET_CSS_PLACEHOLDER + `
      :host { display: flex; flex-direction: column; border-radius: var(--ha-card-border-radius, 12px); overflow: hidden; position: relative; z-index: 0; }
      #map { ${mapSize} }
      #map.dark .leaflet-bar a {
        background-color: #2d2d2d;
        color: #c8c8c8;
        border-bottom-color: #555;
      }
      #map.dark .leaflet-bar a:hover {
        background-color: #404040;
        color: #fff;
      }
      #map.dark .leaflet-bar a.leaflet-disabled {
        background-color: #252525;
        color: #666;
      }
      #map.dark .leaflet-control-attribution {
        background: rgba(30,30,30,0.8);
        color: #999;
      }
      #map.dark .leaflet-control-attribution a { color: #7ab; }
      .leaflet-control a { cursor: pointer; }
      #info {
        display: flex;
        flex-direction: column;
        padding: 10px 16px 8px;
        gap: 4px;
        background: var(--card-background-color, #fff);
        border-top: 1px solid var(--divider-color, rgba(0,0,0,0.12));
      }
      #info:empty { display: none; }
      .no-orders {
        font-size: 13px;
        color: var(--secondary-text-color, #727272);
        padding: 2px 0;
      }
      .order-row {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 12px;
        min-width: 0;
      }
      .order-name {
        font-size: 14px;
        font-weight: 500;
        color: var(--primary-text-color, #212121);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        min-width: 0;
      }
      .order-eta {
        font-size: 13px;
        color: var(--secondary-text-color, #727272);
        white-space: nowrap;
        display: flex;
        align-items: center;
        gap: 4px;
        flex-shrink: 0;
      }
    `;
    shadow.appendChild(style);

    // Map first so its position never shifts when order count changes.
    // Info header below — it grows/shrinks without pushing the map around.
    const mapDiv = document.createElement('div');
    mapDiv.id = 'map';
    // Stop click/pointer events from bubbling out of the shadow DOM.
    // Without this, every Leaflet button click reaches HA's card layer
    // and can trigger a card re-render that destroys the element state.
    mapDiv.addEventListener('click', e => e.stopPropagation());
    mapDiv.addEventListener('pointerdown', e => e.stopPropagation());
    shadow.appendChild(mapDiv);
    this._mapDiv = mapDiv;

    const infoDiv = document.createElement('div');
    infoDiv.id = 'info';
    shadow.appendChild(infoDiv);
    this._infoDiv = infoDiv;
    // Pre-populate synchronously so the header is never empty during build.
    if (this._hass) {
      const orders = this._hass.states[this._entity]?.attributes?.orders || [];
      this._renderInfo(orders);
    }

    requestAnimationFrame(() => {
      L.Icon.Default.imagePath = _LEAFLET_IMG;

      this._map = L.map(mapDiv, { zoomControl: true }).setView([3.1390, 101.6869], 13);

      this._applyTiles(this._isDark(this._hass));

      // Fit-to-all button — mirrors the refocus button on HA's built-in map card.
      const FitControl = L.Control.extend({
        options: { position: 'topleft' },
        onAdd: () => {
          const container = L.DomUtil.create('div', 'leaflet-bar leaflet-control');
          const btn = L.DomUtil.create('a', '', container);
          btn.title = 'Fit to home and drivers';
          btn.style.cssText = 'display:flex;align-items:center;justify-content:center;width:26px;height:26px;';
          btn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
            <path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7zm0 9.5c-1.38 0-2.5-1.12-2.5-2.5s1.12-2.5 2.5-2.5 2.5 1.12 2.5 2.5-1.12 2.5-2.5 2.5z"/>
          </svg>`;
          L.DomEvent.on(btn, 'click', L.DomEvent.stop);
          L.DomEvent.on(btn, 'click', () => this._fitAll());
          return container;
        },
      });
      new FitControl().addTo(this._map);

      // Strip href="#" from all Leaflet control anchors so that clicking them
      // cannot trigger window.location.hash changes (which HA's router may
      // interpret as a navigation event and replace the card element).
      mapDiv.querySelectorAll('.leaflet-control a').forEach(a => a.removeAttribute('href'));

      // Re-render info header after any map movement (defensive backup).
      // Always re-render regardless of _pendingUpdate — if something clears
      // the header while routes are loading, panning/zooming restores it.
      this._map.on('zoomend moveend', () => {
        if (this._hass && this._infoDiv) {
          const orders = this._hass.states?.[this._entity]?.attributes?.orders || [];
          this._renderInfo(orders);
        }
      });

      // Matches HA's own ha-map.ts pattern: plain ResizeObserver with debounceMoveend.
      // debounceMoveend coalesces rapid fires into a single tile load (200ms debounce
      // on the moveend event) without cancelling in-flight requests.
      const ro = new ResizeObserver(() => {
        if (this._map) this._map.invalidateSize({ debounceMoveend: true });
      });
      ro.observe(this);

      this._update();
    });
  }

  _applyTiles(dark) {
    if (this._mapDiv) this._mapDiv.classList.toggle('dark', dark);
    if (!this._tileLayer) {
      this._tileLayer = L.tileLayer(_TILE_URL, {
        attribution: _TILE_ATTR,
        subdomains: 'abcd',
        maxZoom: 20,
        errorTileUrl: 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=',
      }).addTo(this._map);
    }
    // Apply HA's exact dark filter to the tile pane (same technique as ha-map.ts).
    const pane = this._map.getPanes().tilePane;
    if (pane) pane.style.filter = dark ? _DARK_FILTER : '';
  }

  _fitAll() {
    if (!this._map) return;
    const bounds = [];
    if (this._homeMarker) bounds.push(this._homeMarker.getLatLng());
    Object.values(this._driverMarkers).forEach(m => bounds.push(m.getLatLng()));
    if (bounds.length > 1) {
      this._map.fitBounds(bounds, { padding: [48, 48], maxZoom: 16 });
    } else if (bounds.length === 1) {
      this._map.setView(bounds[0], 15);
    }
  }

  _homeIcon() {
    // Same teardrop + white icon style as driver markers for visual consistency.
    // Neutral blue-grey pin so it doesn't compete with the coloured driver pins.
    return L.divIcon({
      html: `<div style="filter:drop-shadow(0 2px 3px rgba(0,0,0,0.4))">
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 40" width="32" height="40">
          <ellipse cx="16" cy="38.5" rx="5" ry="1.5" fill="rgba(0,0,0,0.2)"/>
          <path fill="#546E7A" d="M16 0 C8.268 0 2 6.268 2 14 C2 24 16 38 16 38 C16 38 30 24 30 14 C30 6.268 23.732 0 16 0Z"/>
          <path fill="white" transform="translate(8,5) scale(0.67)"
                d="M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z"/>
        </svg>
      </div>`,
      className: '',
      iconSize: [32, 40],
      iconAnchor: [16, 40],
      popupAnchor: [0, -40],
    });
  }

  _driverIcon(color) {
    // Teardrop pin with white motorbike (MDI mdi:motorbike) inside.
    // CSS drop-shadow on wrapper div avoids SVG <filter id> conflicts
    // between multiple markers sharing the same document.
    return L.divIcon({
      html: `<div style="filter:drop-shadow(0 2px 3px rgba(0,0,0,0.4))">
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 40" width="32" height="40">
          <ellipse cx="16" cy="38.5" rx="5" ry="1.5" fill="rgba(0,0,0,0.2)"/>
          <path fill="${_esc(color)}" d="M16 0 C8.268 0 2 6.268 2 14 C2 24 16 38 16 38 C16 38 30 24 30 14 C30 6.268 23.732 0 16 0Z"/>
          <g fill="white" transform="translate(7.5,5.5) scale(0.72)">
            <path d="M19,7C19,4.7,17.1,3,14.8,3H10.1V5H14.8C16,5 17,5.9 17,7.1V12C18.1,12 19,12.9 19,14H5.3L6,12H8V10H2L0,16H2A3,3 0 0,0 5,19A3,3 0 0,0 8,16H16A3,3 0 0,0 19,19A3,3 0 0,0 22,16H24V14C24,11.4 21.8,9.3 19.2,9L19,7M5,17.5A1.5,1.5 0 0,1 3.5,16A1.5,1.5 0 0,1 5,14.5A1.5,1.5 0 0,1 6.5,16A1.5,1.5 0 0,1 5,17.5M19,17.5A1.5,1.5 0 0,1 17.5,16A1.5,1.5 0 0,1 19,14.5A1.5,1.5 0 0,1 20.5,16A1.5,1.5 0 0,1 19,17.5Z"/>
          </g>
        </svg>
      </div>`,
      className: '',
      iconSize: [32, 40],
      iconAnchor: [16, 40],
      popupAnchor: [0, -40],
    });
  }

  async _fetchRoute(driverLon, driverLat, homeLon, homeLat) {
    const key = `${driverLon.toFixed(3)},${driverLat.toFixed(3)}`;
    if (this._routeCache[key]) return this._routeCache[key];
    try {
      const url = `${this._osrmBase}${driverLon},${driverLat};${homeLon},${homeLat}?overview=full&geometries=geojson`;
      const r = await fetch(url);
      const d = await r.json();
      if (d.code === 'Ok' && d.routes && d.routes[0]) {
        const coords = d.routes[0].geometry.coordinates;
        this._routeCache[key] = coords;
        return coords;
      }
    } catch (e) {
      console.warn('[grabfood-map-card] OSRM fetch failed:', e);
    }
    return null;
  }

  async _update() {
    if (!this._map || !this._hass) return;
    if (this._pendingUpdate) return;
    this._pendingUpdate = true;
    await this._doUpdate();
    this._pendingUpdate = false;
  }

  _formatEta(etaIso, etaMinutes) {
    try {
      const t = new Date(etaIso);
      const timeStr = t.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
      return etaMinutes ? `${timeStr} (${etaMinutes} min)` : timeStr;
    } catch (_) { return ''; }
  }

  _renderInfo(orders) {
    if (!this._infoDiv) return;
    if (!this._showInfo) { this._infoDiv.innerHTML = ''; return; }
    const active = orders.filter(o => o.active_order && o.restaurant);
    if (!active.length) {
      this._infoDiv.innerHTML = '<span class="no-orders">No active orders</span>';
      return;
    }
    const clockSvg = `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="currentColor" style="flex-shrink:0">
      <path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10 10-4.5 10-10S17.5 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8zm.5-13H11v6l5.25 3.15.75-1.23-4.5-2.67V7z"/>
    </svg>`;
    this._infoDiv.innerHTML = active.map(o => {
      const eta = this._formatEta(o.eta, o.eta_minutes);
      return `<div class="order-row">
        <span class="order-name">${_esc(o.restaurant)}</span>
        ${eta ? `<span class="order-eta">${clockSvg}${_esc(eta)}</span>` : ''}
      </div>`;
    }).join('');
  }

  async _doUpdate() {
    const homeState = this._hass.states['zone.home'];
    const homeLat = homeState?.attributes?.latitude;
    const homeLon = homeState?.attributes?.longitude;

    // Home marker
    if (homeLat && homeLon) {
      if (!this._homeMarker) {
        this._homeMarker = L.marker([homeLat, homeLon], { icon: this._homeIcon(), zIndexOffset: 100 })
          .addTo(this._map)
          .bindTooltip('Home', { permanent: false });
      } else {
        this._homeMarker.setLatLng([homeLat, homeLon]);
      }
    }

    // Orders
    const state = this._hass.states[this._entity];
    const orders = state?.attributes?.orders || [];
    this._renderInfo(orders);
    const active = orders.filter(o => o.active_order && o.driver_lat && o.driver_lon);

    // Remove stale markers/routes
    const activeIds = new Set(active.map((o, i) => o.order_id || `idx_${i}`));
    for (const id of Object.keys(this._driverMarkers)) {
      if (!activeIds.has(id)) { this._driverMarkers[id].remove(); delete this._driverMarkers[id]; }
    }
    for (const id of Object.keys(this._routeLines)) {
      if (!activeIds.has(id)) { this._routeLines[id].remove(); delete this._routeLines[id]; }
    }

    const bounds = [];
    if (homeLat && homeLon) bounds.push([homeLat, homeLon]);

    for (let i = 0; i < active.length; i++) {
      const o = active[i];
      const id = o.order_id || `idx_${i}`;
      const color = _ORDER_COLORS[i % _ORDER_COLORS.length];
      const lat = o.driver_lat;
      const lon = o.driver_lon;
      bounds.push([lat, lon]);

      const etaLine = o.eta_minutes ? `<br><span style="color:#666">ETA: ${_esc(String(o.eta_minutes))} min</span>` : '';
      const statusLine = o.order_status ? `<br><small style="color:#888">${_esc(o.order_status)}</small>` : '';
      const popup = `<div style="font-family:sans-serif;font-size:13px"><b>${_esc(o.restaurant || 'Driver')}</b>${etaLine}${statusLine}</div>`;

      if (this._driverMarkers[id]) {
        this._driverMarkers[id].setLatLng([lat, lon]).setPopupContent(popup);
      } else {
        this._driverMarkers[id] = L.marker([lat, lon], { icon: this._driverIcon(color) })
          .addTo(this._map)
          .bindPopup(popup)
          .bindTooltip(o.restaurant || 'Driver');
      }

      if (homeLat && homeLon) {
        const coords = await this._fetchRoute(lon, lat, homeLon, homeLat);
        if (coords) {
          const lls = coords.map(c => [c[1], c[0]]);
          if (this._routeLines[id]) {
            this._routeLines[id].setLatLngs(lls);
          } else {
            this._routeLines[id] = L.polyline(lls, { color, weight: 4, opacity: 0.75, dashArray: '8,4' })
              .addTo(this._map);
          }
        }
      }
    }

    // Refit only on first load or when a genuinely new order ID appears.
    // Do NOT refit when driver location temporarily disappears mid-order (would
    // cause zoom-back every poll cycle when lat/lon is momentarily null).
    const newOrderIds = active
      .map((o, i) => o.order_id || `idx_${i}`)
      .filter(id => !this._fittedOrderIds.has(id));
    const needsRefit = !this._viewInitialized || newOrderIds.length > 0;
    if (needsRefit) {
      this._viewInitialized = true;
      newOrderIds.forEach(id => this._fittedOrderIds.add(id));
      if (bounds.length > 1) {
        this._map.fitBounds(bounds, { padding: [48, 48], maxZoom: 16 });
      } else if (bounds.length === 1) {
        this._map.setView(bounds[0], 15);
      }
    }
  }

  getCardSize() {
    if (this._aspectRatio) {
      // Estimate height from aspect ratio assuming ~500px card width
      const [w, h] = this._aspectRatio.split('/').map(Number);
      return Math.ceil((500 * (h / w)) / 50);
    }
    return Math.ceil((this._height || 400) / 50);
  }

  static getConfigElement() {
    return document.createElement('grabfood-map-card-editor');
  }

  static getStubConfig() {
    return { entity: 'sensor.grabfood_orders', show_info: true, height: 400 };
  }
}

// ── Visual editor ──────────────────────────────────────────────────────────────
class GrabFoodMapCardEditor extends HTMLElement {
  constructor() {
    super();
    this._config = {};
  }

  setConfig(config) {
    this._config = { ...config };
    this._render();
  }

  _render() {
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    const s = this.shadowRoot;
    const cfg = this._config;
    s.innerHTML = `
      <style>
        .form { display: flex; flex-direction: column; gap: 20px; padding: 8px 0; }
        .field { display: flex; flex-direction: column; gap: 4px; }
        label { font-size: 13px; font-weight: 500; color: var(--secondary-text-color, #727272); text-transform: uppercase; letter-spacing: 0.4px; }
        input[type="text"], input[type="number"] {
          width: 100%; padding: 8px 12px; box-sizing: border-box;
          border: 1px solid var(--divider-color, rgba(0,0,0,0.12));
          border-radius: 4px; font-size: 14px;
          background: var(--input-fill-color, var(--secondary-background-color, #f5f5f5));
          color: var(--primary-text-color);
        }
        input:focus { outline: none; border-color: var(--primary-color, #03a9f4); }
        .toggle-row { display: flex; justify-content: space-between; align-items: center; }
        .toggle-label { display: flex; flex-direction: column; gap: 2px; }
        .toggle-label span { font-size: 14px; color: var(--primary-text-color); }
        .toggle-label small { font-size: 12px; color: var(--secondary-text-color, #727272); }
        .hint { font-size: 12px; color: var(--secondary-text-color, #727272); margin-top: 2px; }
        /* Simple toggle switch (ha-switch may not be available in all contexts) */
        .switch { position: relative; width: 36px; height: 20px; flex-shrink: 0; }
        .switch input { opacity: 0; width: 0; height: 0; }
        .slider {
          position: absolute; inset: 0; cursor: pointer;
          background: var(--switch-unchecked-color, #ccc); border-radius: 10px;
          transition: background 0.2s;
        }
        .slider:before {
          content: ""; position: absolute; height: 14px; width: 14px; left: 3px; top: 3px;
          background: white; border-radius: 50%; transition: transform 0.2s;
          box-shadow: 0 1px 3px rgba(0,0,0,0.4);
        }
        input:checked + .slider { background: var(--primary-color, #03a9f4); }
        input:checked + .slider:before { transform: translateX(16px); }
      </style>
      <div class="form">
        <div class="field">
          <label>Entity</label>
          <input type="text" id="entity" value="${_esc(cfg.entity || 'sensor.grabfood_orders')}" placeholder="sensor.grabfood_orders">
        </div>
        <div class="field">
          <div class="toggle-row">
            <div class="toggle-label">
              <span>Show order info</span>
              <small>Restaurant name and ETA above the map</small>
            </div>
            <label class="switch">
              <input type="checkbox" id="show-info" ${cfg.show_info !== false ? 'checked' : ''}>
              <span class="slider"></span>
            </label>
          </div>
        </div>
        <div class="field">
          <label>Aspect ratio</label>
          <input type="text" id="aspect-ratio" value="${_esc(cfg.aspect_ratio || '')}" placeholder="e.g. 16/9">
          <div class="hint">Overrides the fixed height when set (e.g. 16/9, 4/3, 2/1)</div>
        </div>
        <div class="field">
          <label>Height (px)</label>
          <input type="number" id="height" value="${_esc(String(cfg.height || 400))}" min="100" max="2000" step="50">
          <div class="hint">Fixed height in pixels — used when no aspect ratio is set</div>
        </div>
        <div class="field">
          <label>OSRM routing URL</label>
          <input type="text" id="osrm-url" value="${_esc(cfg.osrm_url || '')}" placeholder="${_esc(_OSRM_BASE)}">
          <div class="hint">Override the routing endpoint (e.g. a self-hosted OSRM instance). Leave blank for the default public server.</div>
        </div>
      </div>
    `;
    s.getElementById('entity').addEventListener('change', e => {
      this._fire({ ...this._config, entity: e.target.value.trim() || 'sensor.grabfood_orders' });
    });
    s.getElementById('show-info').addEventListener('change', e => {
      this._fire({ ...this._config, show_info: e.target.checked });
    });
    s.getElementById('aspect-ratio').addEventListener('change', e => {
      const v = e.target.value.trim();
      const c = { ...this._config };
      if (v) c.aspect_ratio = v; else delete c.aspect_ratio;
      this._fire(c);
    });
    s.getElementById('height').addEventListener('change', e => {
      this._fire({ ...this._config, height: parseInt(e.target.value) || 400 });
    });
    s.getElementById('osrm-url').addEventListener('change', e => {
      const v = e.target.value.trim();
      const c = { ...this._config };
      if (v) c.osrm_url = v; else delete c.osrm_url;
      this._fire(c);
    });
  }

  _fire(config) {
    this._config = config;
    this.dispatchEvent(new CustomEvent('config-changed', { detail: { config }, bubbles: true, composed: true }));
  }
}
customElements.define('grabfood-map-card-editor', GrabFoodMapCardEditor);

customElements.define('grabfood-map-card', GrabFoodMapCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: 'grabfood-map-card',
  name: 'GrabFood Map Card',
  description: 'Live map of active GrabFood orders — home pin, driver pins, and route polylines.',
  preview: false,
  documentationURL: 'https://github.com/de0rc/ha-grabfood',
});

console.info('%c grabfood-map-card v' + _VERSION + ' loaded', 'color: #00B14F; font-weight: bold');
