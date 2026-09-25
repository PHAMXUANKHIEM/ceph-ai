#!/usr/bin/env node
/*
 * Authenticated, read-only soak for the realtime Dashboard snapshot path.
 *
 * Required:
 *   STORAGE_STATE=/path/to/playwright-state.json
 * Optional:
 *   BASE_URL=http://127.0.0.1:8000 CLUSTER_ID=<id>
 *   SOAK_SECONDS=600 SAMPLE_INTERVAL_MS=2000
 */
import process from "node:process";

const baseUrl = process.env.BASE_URL || "http://127.0.0.1:8000";
const clusterId = process.env.CLUSTER_ID || "";
const statePath = process.env.STORAGE_STATE;
const soakSeconds = Math.max(10, Number(process.env.SOAK_SECONDS || 600));
const intervalMs = Math.max(500, Number(process.env.SAMPLE_INTERVAL_MS || 2000));

if (!statePath) {
  console.error("STORAGE_STATE is required");
  process.exit(2);
}

let playwright;
try {
  playwright = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
} catch {
  console.error("Playwright is required; set PLAYWRIGHT_MODULE when it is not globally installed");
  process.exit(2);
}

const percentile = (values, p) => {
  if (!values.length) return null;
  const ordered = [...values].sort((a, b) => a - b);
  return ordered[Math.min(ordered.length - 1, Math.ceil(ordered.length * p) - 1)];
};

const withTimeout = async (promise, timeoutMs, label) => {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timer = setTimeout(
          () => reject(new Error(`${label} timed out after ${timeoutMs}ms`)),
          timeoutMs,
        );
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
};

const browser = await playwright.chromium.launch({ headless: true, timeout: 30_000 });
const context = await browser.newContext({ storageState: statePath });
const page = await withTimeout(context.newPage(), 15_000, "browserContext.newPage");
let documentRequests = 0;
let websocketOpens = 0;
let websocketFrames = 0;
page.on("request", (request) => {
  if (request.resourceType() === "document") documentRequests += 1;
});
page.on("websocket", (socket) => {
  websocketOpens += 1;
  socket.on("framereceived", () => { websocketFrames += 1; });
});

const query = clusterId ? `?cluster=${encodeURIComponent(clusterId)}` : "";
await page.goto(`${baseUrl}/${query}`, { waitUntil: "commit", timeout: 30_000 });

const samples = [];
const deadline = Date.now() + soakSeconds * 1000;
while (Date.now() < deadline) {
  const sample = await page.evaluate(async ({ url, expectedCluster }) => {
    const started = performance.now();
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch(url, {
        credentials: "same-origin",
        headers: { "Accept": "application/json", "X-Request-ID": `soak-${Date.now()}` },
        signal: controller.signal,
      });
      const body = await response.json().catch(() => ({}));
      return {
        ok: response.ok && (!expectedCluster || body.cluster_id === expectedCluster),
        status: response.status,
        latency_ms: performance.now() - started,
        cluster_id: body.cluster_id || null,
        generation: Number(body.generation || 0),
        collected_at: body.collected_at || null,
        published_at: body.published_at || null,
        age_seconds: Number(body.age_seconds || 0),
        stale: Boolean(body.stale),
        refreshing: Boolean(body.refreshing),
        last_error: body.last_error || null,
      };
    } catch (error) {
      return { ok: false, status: 0, latency_ms: performance.now() - started, error: String(error) };
    } finally {
      window.clearTimeout(timeout);
    }
  }, {
    url: `${baseUrl}/api/dashboard/health${query}`,
    expectedCluster: clusterId,
  });
  samples.push({ observed_at: new Date().toISOString(), ...sample });
  await page.waitForTimeout(Math.min(intervalMs, Math.max(0, deadline - Date.now())));
}

const successful = samples.filter((sample) => sample.ok);
const generationChanges = [];
for (let index = 1; index < successful.length; index += 1) {
  if (successful[index].generation !== successful[index - 1].generation) {
    generationChanges.push({
      generation: successful[index].generation,
      collected_at: successful[index].collected_at,
      published_at: successful[index].published_at,
      observed_at: successful[index].observed_at,
    });
  }
}
const collectionGaps = [];
const publicationGaps = [];
for (let index = 1; index < generationChanges.length; index += 1) {
  const previous = Date.parse(generationChanges[index - 1].collected_at);
  const current = Date.parse(generationChanges[index].collected_at);
  if (Number.isFinite(previous) && Number.isFinite(current)) {
    collectionGaps.push((current - previous) / 1000);
  }
  const previousPublished = Date.parse(generationChanges[index - 1].published_at);
  const currentPublished = Date.parse(generationChanges[index].published_at);
  if (Number.isFinite(previousPublished) && Number.isFinite(currentPublished)) {
    publicationGaps.push((currentPublished - previousPublished) / 1000);
  }
}

const latencies = successful.map((sample) => sample.latency_ms);
const ages = successful.map((sample) => sample.age_seconds);
const result = {
  base_url: baseUrl,
  cluster_id: clusterId || null,
  requested_duration_seconds: soakSeconds,
  sample_interval_ms: intervalMs,
  samples_total: samples.length,
  samples_successful: successful.length,
  samples_failed: samples.length - successful.length,
  stale_samples: successful.filter((sample) => sample.stale).length,
  samples_with_error: successful.filter((sample) => sample.last_error).length,
  p95_api_ms: percentile(latencies, 0.95),
  max_api_ms: latencies.length ? Math.max(...latencies) : null,
  p95_snapshot_age_seconds: percentile(ages, 0.95),
  max_snapshot_age_seconds: ages.length ? Math.max(...ages) : null,
  generation_changes: generationChanges.length,
  p95_collection_gap_seconds: percentile(collectionGaps, 0.95),
  max_collection_gap_seconds: collectionGaps.length ? Math.max(...collectionGaps) : null,
  p95_publication_gap_seconds: percentile(publicationGaps, 0.95),
  max_publication_gap_seconds: publicationGaps.length ? Math.max(...publicationGaps) : null,
  document_requests: documentRequests,
  websocket_opens: websocketOpens,
  websocket_frames: websocketFrames,
  passed_no_reload: documentRequests === 1,
  passed_cluster_isolation: successful.every((sample) => !clusterId || sample.cluster_id === clusterId),
};

console.log(JSON.stringify(result, null, 2));
await context.close();
await browser.close();

if (result.samples_failed || !result.passed_no_reload || !result.passed_cluster_isolation) {
  process.exit(1);
}
