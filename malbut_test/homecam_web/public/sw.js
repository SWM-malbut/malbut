const CACHE_NAME = "malbut-homecam-shell-v1";
const SHELL = ["/manifest.webmanifest", "/favicon.svg", "/homecam-icon.svg"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)),
      ))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (
    url.origin !== self.location.origin ||
    url.pathname.startsWith("/api/") ||
    request.destination === "document"
  ) {
    return;
  }

  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok && request.destination !== "document") {
          const copy = response.clone();
          event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.put(request, copy)));
        }
        return response;
      })
      .catch(() => caches.match(request)),
  );
});

self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch {
    payload = { body: event.data ? event.data.text() : "" };
  }

  const title = typeof payload.title === "string" ? payload.title : "MALBUT 홈캠";
  const body = typeof payload.body === "string" ? payload.body : "새로운 홈캠 이벤트가 있습니다.";
  const payloadData =
    payload.data && typeof payload.data === "object" && !Array.isArray(payload.data)
      ? payload.data
      : {};
  const requestedUrl =
    typeof payload.url === "string" ? payload.url : payloadData.url;
  let eventUrl = "/?view=events";
  if (typeof requestedUrl === "string" && requestedUrl.startsWith("/")) {
    const resolvedUrl = new URL(requestedUrl, self.location.origin);
    if (resolvedUrl.origin === self.location.origin) {
      eventUrl = `${resolvedUrl.pathname}${resolvedUrl.search}${resolvedUrl.hash}`;
    }
  }

  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      icon: "/homecam-icon.svg",
      badge: "/homecam-icon.svg",
      tag:
        typeof payload.tag === "string"
          ? payload.tag
          : typeof payloadData.eventId === "string"
            ? `homecam-event-${payloadData.eventId}`
            : "homecam-event",
      renotify: true,
      data: { url: eventUrl },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const path =
    event.notification.data && typeof event.notification.data.url === "string"
      ? event.notification.data.url
      : "/?view=events";
  const targetUrl = new URL(path, self.location.origin).href;
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if ("focus" in client) {
          client.navigate(targetUrl);
          return client.focus();
        }
      }
      return self.clients.openWindow(targetUrl);
    }),
  );
});
