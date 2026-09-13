// Bilnova AI — Mobile Dashboard Service Worker
// NEW 13-Sep-2026: minimal hai — is app ka poora data LIVE server se
// aata hai [tumhare shop ka PC], isliye yahan bhaari offline-caching
// nahi ki. Sirf itna kaam hai: browser ko "installable" maanne ke
// liye ek service worker ka hona ZAROORI hai [PWA spec requirement].
const CACHE_NAME = "bilnova-shell-v1";
const SHELL_FILES = ["./index.html", "./manifest.json",
                      "./icons/icon-192.png", "./icons/icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_FILES))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  // Sirf apni hi shell files [HTML/manifest/icons] cache se serve
  // karo — API calls [shop ke PC ko] hamesha seedhe network se jaati
  // hain, kabhi cache nahi hoti [data hamesha LIVE hona chahiye].
  const url = new URL(event.request.url);
  if (SHELL_FILES.some((f) => url.pathname.endsWith(f.replace("./", "")))) {
    event.respondWith(
      caches.match(event.request).then((cached) => cached || fetch(event.request))
    );
  }
});
