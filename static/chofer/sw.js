// Service Worker de la app de rutas del chofer. Se sirve desde /chofer/sw.js
// (no desde /static/) para que su scope cubra toda esa seccion. Dos trabajos:
// 1) cachear el "shell" de la app para que abra aunque no haya señal.
// 2) Background Sync (solo Android/Chrome): si el chofer confirmo una entrega
//    sin internet, cuando el celular recupera señal esto se dispara solo,
//    aunque la app este cerrada, y manda lo pendiente al servidor.

const CACHE_NAME = 'chofer-shell-v1';
const SHELL_URLS = [
  '/chofer/ruta',
  '/static/chofer/manifest.json',
  '/static/chofer/icons/icon-192.png',
  '/static/chofer/icons/icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(SHELL_URLS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  // Las llamadas a la API nunca se cachean aqui -- los datos los maneja la
  // propia pagina via IndexedDB. Solo se cachea el "shell" (HTML/manifest/iconos).
  if (event.request.method !== 'GET' || url.pathname.startsWith('/api/')) return;

  event.respondWith(
    fetch(event.request)
      .then((resp) => {
        const copia = resp.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copia));
        return resp;
      })
      .catch(() => caches.match(event.request))
  );
});

// ── IndexedDB (mismo esquema que usa chofer_ruta.html) ─────────────────────
const DB_NAME = 'chofer_rutas';
const DB_VERSION = 1;

function abrirDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains('ruta')) db.createObjectStore('ruta', { keyPath: 'id' });
      if (!db.objectStoreNames.contains('cola_sync')) db.createObjectStore('cola_sync', { keyPath: 'clave', autoIncrement: true });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function colaPendiente() {
  const db = await abrirDB();
  return new Promise((resolve, reject) => {
    const req = db.transaction('cola_sync', 'readonly').objectStore('cola_sync').getAll();
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function quitarDeCola(clave) {
  const db = await abrirDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('cola_sync', 'readwrite');
    tx.objectStore('cola_sync').delete(clave);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

async function quitarDeRutaLocal(embarqueId) {
  const db = await abrirDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('ruta', 'readwrite');
    tx.objectStore('ruta').delete(embarqueId);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

async function avisarClientes(mensaje) {
  const clientes = await self.clients.matchAll();
  for (const c of clientes) c.postMessage(mensaje);
}

async function vaciarCola() {
  const pendientes = await colaPendiente();
  for (const item of pendientes) {
    try {
      const r = await fetch(`/api/chofer/entregas/${item.embarqueId}/confirmar`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(item.payload),
      });
      const data = await r.json().catch(() => ({ ok: false }));
      if (data.ok) {
        await quitarDeCola(item.clave);
        await quitarDeRutaLocal(item.embarqueId);
        await avisarClientes({ type: 'sync-ok', embarqueId: item.embarqueId, yaEntregada: !!data.ya_entregada });
      }
      // si no es ok (ej. error de validacion), se deja en la cola -- no se
      // puede corregir sola, pero tampoco se pierde el intento.
    } catch (e) {
      // sigue sin señal real: se reintenta en el siguiente evento sync.
      return;
    }
  }
}

self.addEventListener('sync', (event) => {
  if (event.tag === 'sync-entregas') {
    event.waitUntil(vaciarCola());
  }
});

// Respaldo universal: si la pagina esta abierta y nos pide sincronizar
// (navegadores sin Background Sync, ej. iOS), hacemos el mismo trabajo aqui.
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'sincronizar-ahora') {
    event.waitUntil(vaciarCola());
  }
});
