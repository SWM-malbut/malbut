import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";

async function loadServiceWorker() {
  const source = await readFile(
    new URL("../public/sw.js", import.meta.url),
    "utf8",
  );
  const listeners = {};
  const notifications = [];
  const self = {
    location: { origin: "https://homecam.example" },
    addEventListener(type, listener) {
      listeners[type] = listener;
    },
    registration: {
      async showNotification(title, options) {
        notifications.push({ title, options });
      },
    },
    clients: {
      claim: async () => undefined,
      matchAll: async () => [],
      openWindow: async () => undefined,
    },
    skipWaiting: async () => undefined,
  };
  const caches = {
    open: async () => ({
      addAll: async () => undefined,
      put: async () => undefined,
    }),
    keys: async () => [],
    delete: async () => true,
    match: async () => undefined,
  };
  runInNewContext(source, { Array, Promise, URL, caches, self });
  return { listeners, notifications };
}

async function dispatchPush(listener, payload) {
  let pending;
  listener({
    data: { json: () => payload, text: () => JSON.stringify(payload) },
    waitUntil(value) {
      pending = value;
    },
  });
  await pending;
}

test("Web Push opens the exact same-origin event deep link", async () => {
  const { listeners, notifications } = await loadServiceWorker();
  await dispatchPush(listeners.push, {
    title: "거실 홈캠",
    body: "7. 26. 21:30 · 사람이 감지되었습니다.",
    data: {
      eventId: "00000000-0000-4000-8000-000000000001",
      url: "/?view=events&device=living-room&event=event-1",
    },
  });

  assert.equal(notifications.length, 1);
  assert.equal(
    notifications[0].options.data.url,
    "/?view=events&device=living-room&event=event-1",
  );
  assert.match(notifications[0].options.tag, /^homecam-event-/);
  assert.equal("image" in notifications[0].options, false);
});

test("Web Push refuses protocol-relative notification links", async () => {
  const { listeners, notifications } = await loadServiceWorker();
  await dispatchPush(listeners.push, {
    data: { url: "//evil.example/redirect" },
  });
  assert.equal(notifications[0].options.data.url, "/?view=events");
});
