#!/usr/bin/env node
/* Optional browser smoke/load test for the realtime path. */
import process from "node:process";

const baseUrl = process.env.BASE_URL || "http://127.0.0.1:8000";
const clusters = (process.env.CLUSTERS || "").split(",").map((value) => value.trim()).filter(Boolean);
const statePath = process.env.STORAGE_STATE || undefined;
const isolationProbe = process.env.ISOLATION_PROBE_CLUSTER || "";

let playwright;
try { playwright = await import("playwright"); }
catch { console.error("SKIP: install playwright and provide STORAGE_STATE to run the browser test"); process.exit(2); }

const browser = await playwright.chromium.launch({
  headless: true,
  executablePath: process.env.BROWSER_EXECUTABLE || undefined,
});
const context = await browser.newContext(statePath ? { storageState: statePath } : {});
const samples = [];
let isolation = { verified: false, mode: "not-run" };
const percentile = (values, p) => {
  const ordered = [...values].sort((a, b) => a - b);
  return ordered[Math.min(ordered.length - 1, Math.ceil(ordered.length * p) - 1)] || 0;
};

for (const tabCount of [1, 5, 10]) {
  const pages = await Promise.all(Array.from({ length: tabCount }, () => context.newPage()));
  const started = performance.now();
  const results = await Promise.all(pages.map(async (page, index) => {
    const cluster = clusters.length ? clusters[index % clusters.length] : "";
    const url = `${baseUrl}/api/dashboard/health${cluster ? `?cluster=${encodeURIComponent(cluster)}` : ""}`;
    const requestStarted = performance.now();
    const response = await context.request.get(url, {
      headers: { "X-Request-ID": `browser-load-${tabCount}-${index}-${Date.now()}` },
    });
    const body = await response.json();
    const latency = performance.now() - requestStarted;
    if (cluster && body.cluster_id && body.cluster_id !== cluster) {
      throw new Error(`cluster isolation failure: requested ${cluster}, received ${body.cluster_id}`);
    }
    return latency;
  }));
  samples.push({ tabCount, p95_ms: percentile(results, 0.95), max_ms: Math.max(...results), wall_ms: performance.now() - started });
  await Promise.all(pages.map((page) => page.close()));
}

if (isolationProbe) {
  const response = await context.request.get(
    `${baseUrl}/api/dashboard/health?cluster=${encodeURIComponent(isolationProbe)}`,
    { headers: { "X-Request-ID": `browser-isolation-${Date.now()}` } },
  );
  const body = await response.json().catch(() => ({}));
  const scopePage = await context.newPage();
  await scopePage.goto(`${baseUrl}/login`, { waitUntil: "domcontentloaded" });
  const wsPolicy = await scopePage.evaluate(({ invalidCluster }) => new Promise((resolve) => {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(`${protocol}//${window.location.host}/ws/cluster-state?cluster_id=${encodeURIComponent(invalidCluster)}`);
    const timer = window.setTimeout(() => { socket.close(); resolve({ code: null, timed_out: true }); }, 5000);
    socket.onclose = (event) => { window.clearTimeout(timer); resolve({ code: event.code, timed_out: false }); };
    socket.onerror = () => {};
  }), { invalidCluster: isolationProbe });
  await scopePage.close();
  isolation = {
    verified: wsPolicy.code === 1008 && !wsPolicy.timed_out,
    mode: "websocket-scope-policy",
    status: response.status,
    requested: isolationProbe,
    http_returned: body.cluster_id || null,
    websocket_close_code: wsPolicy.code,
  };
  if (!isolation.verified) throw new Error(`cluster isolation probe was not rejected: ${JSON.stringify(isolation)}`);
} else if (clusters.length >= 2) {
  isolation = { verified: true, mode: "multi-cluster-response-check" };
}

console.log(JSON.stringify({ base_url: baseUrl, clusters, isolation, samples }, null, 2));
await context.close();
await browser.close();
