#!/usr/bin/env node
/*
 * Authenticated browser benchmark for the realtime dashboard.
 *
 * This deliberately opens real browser pages. Do not replace page.evaluate(fetch)
 * with context.request: the latter bypasses the browser tab and cannot detect
 * document reloads, WebSocket connections, or the polling fallback.
 */
import process from "node:process";

const baseUrl = process.env.BASE_URL || "http://127.0.0.1:8000";
const dashboardPath = process.env.DASHBOARD_PATH || "/";
const clusters = (process.env.CLUSTERS || "").split(",").map((value) => value.trim()).filter(Boolean);
const statePath = process.env.STORAGE_STATE || undefined;
const isolationProbe = process.env.ISOLATION_PROBE_CLUSTER || "";
const fallbackProbeSeconds = Number(process.env.FALLBACK_PROBE_SECONDS || 12);
const settleMs = Number(process.env.SETTLE_MS || 1500);

let playwright;
try { playwright = await import(process.env.PLAYWRIGHT_MODULE || "playwright"); }
catch {
  console.error("SKIP: install playwright and provide STORAGE_STATE to run the browser test");
  process.exit(2);
}

const percentile = (values, p) => {
  const ordered = [...values].sort((a, b) => a - b);
  return ordered[Math.min(ordered.length - 1, Math.ceil(ordered.length * p) - 1)] || 0;
};

const browser = await playwright.chromium.launch({
  headless: true,
  executablePath: process.env.BROWSER_EXECUTABLE || undefined,
});
const context = await browser.newContext(statePath ? { storageState: statePath } : {});
const samples = [];
let isolation = { verified: false, mode: "not-run" };

const healthUrl = (cluster) => `${baseUrl}/api/dashboard/health${cluster ? `?cluster=${encodeURIComponent(cluster)}` : ""}`;
const dashboardUrl = (cluster) => `${baseUrl}${dashboardPath}${cluster ? `?cluster=${encodeURIComponent(cluster)}` : ""}`;

async function browserHealthRead(page, cluster, label) {
  return page.evaluate(async ({ url, requestId }) => {
    const started = performance.now();
    const response = await fetch(url, {
      credentials: "same-origin",
      headers: { "X-Request-ID": requestId, "Accept": "application/json" },
    });
    const body = await response.json().catch(() => ({}));
    return { status: response.status, latency_ms: performance.now() - started, body };
  }, { url: healthUrl(cluster), requestId: `browser-${label}-${Date.now()}-${Math.random().toString(36).slice(2, 10)}` });
}

async function measureTab(page, cluster, tabCount, index) {
  const requests = [];
  let documentRequests = 0;
  let websocketOpens = 0;
  let websocketMessages = 0;
  let healthSettled = false;
  const healthStarted = new WeakMap();
  let resolveHealth;
  let rejectHealth;
  const firstHealthRead = new Promise((resolve, reject) => {
    resolveHealth = resolve;
    rejectHealth = reject;
  });
  const navigationStarted = performance.now();
  page.on("request", (request) => {
    requests.push({ url: request.url(), resourceType: request.resourceType() });
    if (request.resourceType() === "document") documentRequests += 1;
    if (new URL(request.url()).pathname === "/api/dashboard/health") {
      healthStarted.set(request, performance.now());
    }
  });
  page.on("response", async (response) => {
    const request = response.request();
    if (healthSettled || new URL(request.url()).pathname !== "/api/dashboard/health") return;
    try {
      const body = await response.json();
      healthSettled = true;
      resolveHealth({
        status: response.status(),
        latency_ms: performance.now() - (healthStarted.get(request) || navigationStarted),
        body,
      });
    } catch (error) {
      healthSettled = true;
      rejectHealth(error);
    }
  });
  page.on("websocket", (socket) => {
    websocketOpens += 1;
    socket.on("framereceived", () => { websocketMessages += 1; });
  });
  // Measure the first HTML response, then allow the page to hydrate. Waiting
  // for every third-party/static resource would conflate page-open latency
  // with an optional footer asset and can hide the snapshot fast path.
  await page.goto(dashboardUrl(cluster), { waitUntil: "commit", timeout: 30_000 });
  const navigationMs = performance.now() - navigationStarted;
  const read = await Promise.race([
    firstHealthRead,
    new Promise((_, reject) => setTimeout(() => reject(new Error("dashboard health request timed out")), 10_000)),
  ]);
  if (cluster && read.body.cluster_id && read.body.cluster_id !== cluster) {
    throw new Error(`cluster isolation failure: requested ${cluster}, received ${read.body.cluster_id}`);
  }
  await page.waitForTimeout(settleMs);
  return {
    navigation_ms: Number(navigationMs.toFixed(2)),
    api_ms: Number(read.latency_ms.toFixed(2)),
    api_status: read.status,
    cluster_id: read.body.cluster_id || null,
    generation: read.body.generation || 0,
    collected_at: read.body.collected_at || null,
    document_requests: documentRequests,
    websocket_opens: websocketOpens,
    websocket_messages: websocketMessages,
    health_requests: requests.filter((item) => item.url.includes("/api/dashboard/health")).length,
  };
}

for (const tabCount of [1, 5, 10]) {
  const pages = await Promise.all(Array.from({ length: tabCount }, () => context.newPage()));
  const started = performance.now();
  const results = await Promise.all(pages.map((page, index) => {
    const cluster = clusters.length ? clusters[index % clusters.length] : "";
    return measureTab(page, cluster, tabCount, index);
  }));
  samples.push({
    tabCount,
    p95_navigation_ms: Number(percentile(results.map((item) => item.navigation_ms), 0.95).toFixed(2)),
    p95_api_ms: Number(percentile(results.map((item) => item.api_ms), 0.95).toFixed(2)),
    max_document_requests: Math.max(...results.map((item) => item.document_requests)),
    total_websocket_opens: results.reduce((sum, item) => sum + item.websocket_opens, 0),
    wall_ms: Number((performance.now() - started).toFixed(2)),
    tabs: results,
  });
  await Promise.all(pages.map((page) => page.close()));
}

/* Force the socket path down on one real page and prove polling continues.
 * The page's normal health polling is the fallback; document_requests must
 * remain one, so a page reload is not being used as the fallback mechanism.
 */
const fallbackPage = await context.newPage();
await fallbackPage.addInitScript(() => {
  const NativeWebSocket = window.WebSocket;
  window.WebSocket = function (...args) {
    const socket = new NativeWebSocket(...args);
    window.setTimeout(() => socket.close(1000, "benchmark-fallback"), 0);
    return socket;
  };
  window.WebSocket.prototype = NativeWebSocket.prototype;
});
let fallbackDocuments = 0;
let fallbackHealthRequests = 0;
fallbackPage.on("request", (request) => {
  if (request.resourceType() === "document") fallbackDocuments += 1;
  if (request.url().includes("/api/dashboard/health")) fallbackHealthRequests += 1;
});
const fallbackCluster = clusters[0] || "";
  await fallbackPage.goto(dashboardUrl(fallbackCluster), { waitUntil: "commit", timeout: 30_000 });
await fallbackPage.waitForTimeout(Math.max(1, fallbackProbeSeconds) * 1000);
const fallback = {
  verified: fallbackDocuments === 1 && fallbackHealthRequests >= 2,
  document_requests: fallbackDocuments,
  health_requests: fallbackHealthRequests,
  wait_seconds: fallbackProbeSeconds,
};
await fallbackPage.close();

if (isolationProbe) {
  const scopePage = await context.newPage();
  await scopePage.goto(`${baseUrl}/login`, { waitUntil: "commit" });
  const wsPolicy = await scopePage.evaluate(({ invalidCluster }) => new Promise((resolve) => {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(`${protocol}//${window.location.host}/ws/cluster-state?cluster_id=${encodeURIComponent(invalidCluster)}`);
    const timer = window.setTimeout(() => { socket.close(); resolve({ code: null, timed_out: true }); }, 5000);
    socket.onclose = (event) => { window.clearTimeout(timer); resolve({ code: event.code, timed_out: false }); };
    socket.onerror = () => {};
  }), { invalidCluster: isolationProbe });
  await scopePage.close();
  const clusterChecks = [];
  if (clusters.length >= 2) {
    for (const cluster of clusters.slice(0, 2)) {
      const page = await context.newPage();
      const read = await browserHealthRead(page, cluster, `isolation-${cluster}`);
      clusterChecks.push({ requested: cluster, returned: read.body.cluster_id || null, status: read.status });
      await page.close();
    }
  }
  isolation = {
    verified: wsPolicy.code === 1008 && !wsPolicy.timed_out && clusterChecks.every((item) => item.returned === item.requested),
    mode: "browser-websocket-scope-and-cluster-response",
    requested_invalid_cluster: isolationProbe,
    websocket_close_code: wsPolicy.code,
    cluster_checks: clusterChecks,
  };
  if (!isolation.verified) throw new Error(`cluster isolation probe failed: ${JSON.stringify(isolation)}`);
} else if (clusters.length >= 2) {
  isolation = { verified: true, mode: "browser-cluster-response-check", clusters: clusters.slice(0, 2) };
}

console.log(JSON.stringify({ base_url: baseUrl, dashboard_path: dashboardPath, clusters, fallback, isolation, samples }, null, 2));
await context.close();
await browser.close();
