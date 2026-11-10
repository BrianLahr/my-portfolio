// Scope-relative cache for static assets; microphone data is never intercepted.
const VERSION='spectra-pwa-0.4.0-r2';
const ASSETS=['./','./index.html','./styles.css','./app.mjs','./manifest.webmanifest','./src/dsp-core.mjs','./workers/dsp-worker.mjs','./workers/capture-worklet.js','./icons/icon-192.png','./icons/icon-512.png','./icons/apple-touch-icon.png'];
self.addEventListener('install',event=>event.waitUntil(caches.open(VERSION).then(cache=>cache.addAll(ASSETS)).then(()=>self.skipWaiting())));
self.addEventListener('activate',event=>event.waitUntil(Promise.all([caches.keys().then(keys=>Promise.all(keys.filter(k=>k.startsWith('spectra-pwa-')&&k!==VERSION).map(k=>caches.delete(k)))),self.clients.claim()])));
self.addEventListener('fetch',event=>{
  const req=event.request;
  if(req.method!=='GET'||new URL(req.url).origin!==self.location.origin)return;
  event.respondWith(caches.match(req).then(hit=>hit||fetch(req)));
});
