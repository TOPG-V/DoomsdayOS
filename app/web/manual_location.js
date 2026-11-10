// Browser-local, explicitly user-set location. Never reads GPS or calls the network.
(function (root) {
  'use strict';
  const KEY = 'doomsday.manual-position.v1';
  function validate(candidate) {
    if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) throw Error('A location needs latitude and longitude');
    const lat = candidate.lat, lon = candidate.lon;
    if (typeof lat !== 'number' || typeof lon !== 'number' || !Number.isFinite(lat) || !Number.isFinite(lon) ||
        lat < -90 || lat > 90 || lon < -180 || lon > 180) throw Error('Location coordinates must be finite and within latitude/longitude limits');
    const name = typeof candidate.name === 'string' ? candidate.name.trim().slice(0, 100) : '';
    return {lat, lon, name};
  }
  function getStorage(storage) {
    if (storage !== undefined) return storage;
    try { return root.localStorage; } catch (_) { return null; }
  }
  function load(storage) {
    const store = getStorage(storage);
    if (!store) return null;
    try {
      const raw = store.getItem(KEY);
      return raw === null ? null : validate(JSON.parse(raw));
    } catch (_) { return null; }
  }
  function save(location, storage) {
    const clean = validate(location);
    const store = getStorage(storage);
    try { if (store) store.setItem(KEY, JSON.stringify(clean)); } catch (_) { /* Browser may refuse storage; session use still works. */ }
    return clean;
  }
  function clear(storage) {
    const store = getStorage(storage);
    try { if (store) store.removeItem(KEY); } catch (_) { /* No persistent storage. */ }
  }
  const api = Object.freeze({validate, load, save, clear, key: KEY});
  root.DoomsdayManualLocation = api;
  if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
