/* ==========================================================================
   台股追蹤｜Service Worker（離線快取）
   --------------------------------------------------------------------------
   策略：
     - 導覽請求（HTML）：network-first，成功時更新快取；離線時回退快取，
       再退回首頁快取，最後才回退離線提示頁。
     - 同源靜態資源（圖示、manifest）：stale-while-revalidate，先回快取
       再背景更新，讓 App 圖示與 manifest 立即載入。
     - 跨網域請求（TWSE 即時報價 API）：一律不攔截、不快取，直接走網路，
       確保即時報價永遠是新鮮的；離線時由頁面自身的內建快照接手。
   版本：改版時只需調整 CACHE_VERSION，舊快取會於 activate 時清除。
   ========================================================================== */

const CACHE_VERSION = 'tw-stock-tracker-v1';
const APP_SHELL = [
  './',
  './index.html',
  './manifest.json',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './icons/icon-maskable-512.png',
  './icons/apple-touch-icon.png',
  './icons/favicon-32.png',
  './icons/favicon.ico',
  './assets/yu_cheng_jun_avatar.png'
];

/* ---------- install：預快取 app shell ---------- */
self.addEventListener('install', (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE_VERSION);
      // 逐項加入：單一資源失敗（例如頭像暫時 404）不應讓整個 SW 安裝失敗。
      await Promise.all(
        APP_SHELL.map((url) =>
          cache.add(new Request(url, { cache: 'reload' })).catch(() => null)
        )
      );
      await self.skipWaiting();
    })()
  );
});

/* ---------- activate：清除舊版快取並立即接管 ---------- */
self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k))
      );
      if (self.registration.navigationPreload) {
        try { await self.registration.navigationPreload.disable(); } catch (e) {}
      }
      await self.clients.claim();
    })()
  );
});

/* ---------- fetch ---------- */
self.addEventListener('fetch', (event) => {
  const req = event.request;

  // 只處理 GET；POST 等一律放行。
  if (req.method !== 'GET') return;

  const url = new URL(req.url);

  // 跨網域（TWSE API、外部資源）：不攔截，保持即時性。
  if (url.origin !== self.location.origin) return;

  // 導覽請求：network-first
  if (req.mode === 'navigate') {
    event.respondWith(
      (async () => {
        try {
          const fresh = await fetch(req);
          const cache = await caches.open(CACHE_VERSION);
          cache.put('./index.html', fresh.clone());
          return fresh;
        } catch (e) {
          const cache = await caches.open(CACHE_VERSION);
          return (
            (await cache.match('./index.html')) ||
            (await cache.match('./')) ||
            new Response(
              '<!doctype html><meta charset="utf-8"><title>離線</title>' +
                '<body style="font-family:system-ui;padding:2rem;line-height:1.7">' +
                '<h1>目前離線</h1><p>台股追蹤需要網路才能取得最新報價。' +
                '請恢復連線後重新整理。</p></body>',
              { status: 200, headers: { 'Content-Type': 'text/html; charset=utf-8' } }
            )
          );
        }
      })()
    );
    return;
  }

  // 同源靜態資源：stale-while-revalidate
  event.respondWith(
    (async () => {
      const cache = await caches.open(CACHE_VERSION);
      const cached = await cache.match(req);
      const network = fetch(req)
        .then((res) => {
          if (res && res.status === 200 && res.type === 'basic') {
            cache.put(req, res.clone());
          }
          return res;
        })
        .catch(() => null);
      return cached || (await network) || Response.error();
    })()
  );
});

/* ---------- 讓頁面可要求立即啟用新版 SW ---------- */
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') self.skipWaiting();
});
