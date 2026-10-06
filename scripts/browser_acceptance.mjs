#!/usr/bin/env node
/*
 * Browser and operator acceptance for the Ceph dashboard (plan 5.4).
 *
 * Opens real Chromium pages against BASE_URL, logs in through the login form
 * and writes a structured report plus screenshots under OUT_DIR/<sha>/.
 * Read-only: it only navigates and reads; it never submits an action form.
 *
 *   BASE_URL=http://127.0.0.1:8000 DASHBOARD_USER=admin DASHBOARD_PASSWORD=... \
 *   SECOND_CLUSTER_ID=<id> GIT_SHA=$(git rev-parse --short HEAD) \
 *   node scripts/browser_acceptance.mjs
 *
 * Exit status: 0 all checks passed, 1 a check failed, 2 playwright missing.
 */
import fs from "node:fs";
import path from "node:path";
import process from "node:process";

const baseUrl = (process.env.BASE_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
const user = process.env.DASHBOARD_USER || "admin";
const password = process.env.DASHBOARD_PASSWORD || "";
const secondCluster = process.env.SECOND_CLUSTER_ID || "";
const sha = process.env.GIT_SHA || "unknown";
const outDir = path.join(process.env.OUT_DIR || "artifacts/browser-acceptance", sha);
const pages = (process.env.PAGES || "/,/alerts,/nodes,/pools,/block-storage,/object-storage/buckets,/backups,/clusters,/settings")
  .split(",").map((item) => item.trim()).filter(Boolean);
const viewports = [
  { name: "1280", width: 1280, height: 800 },
  { name: "1440", width: 1440, height: 900 },
  { name: "1920", width: 1920, height: 1080 },
  { name: "narrow", width: 390, height: 844 },
];

let playwright;
try { playwright = await import(process.env.PLAYWRIGHT_MODULE || "playwright"); }
catch {
  console.error("SKIP: playwright is not installed");
  process.exit(2);
}

fs.mkdirSync(outDir, { recursive: true });
const checks = [];
const record = (check, status, detail, extra = {}) => {
  checks.push({ check, status, detail, ...extra });
  console.log(`BROWSER status=${status} check=${check} detail=${detail}`);
};
async function guarded(check, fn) {
  try { await fn(); }
  catch (error) { record(check, "FAILED", String(error.message || error).split("\n")[0].slice(0, 200)); }
}
const slug = (value) => value.replace(/[^a-z0-9]+/gi, "_").replace(/^_|_$/g, "") || "home";

const browser = await playwright.chromium.launch({
  headless: true,
  executablePath: process.env.BROWSER_EXECUTABLE || undefined,
});

async function login(context) {
  const page = await context.newPage();
  await page.goto(`${baseUrl}/login?product=ceph`, { waitUntil: "domcontentloaded" });
  await page.fill('input[name="username"]', user);
  await page.fill('input[name="password"]', password);
  await Promise.all([
    page.waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 15_000 }),
    page.click('button[type="submit"], input[type="submit"]'),
  ]);
  await page.close();
}

async function openPage(context, route, viewport) {
  const page = await context.newPage();
  await page.setViewportSize({ width: viewport.width, height: viewport.height });
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error.message || error).slice(0, 200)));
  const response = await page.goto(`${baseUrl}${route}`, { waitUntil: "domcontentloaded", timeout: 30_000 });
  await page.waitForTimeout(Number(process.env.SETTLE_MS || 1500));
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  const file = path.join(outDir, `${slug(route)}-${viewport.name}.png`);
  await page.screenshot({ path: file, fullPage: false });
  return { page, status: response ? response.status() : 0, errors, overflow, file };
}

// 1. Layout across viewports.
const authed = await browser.newContext();
try {
  await login(authed);
  record("login", "PASSED", "login form accepted the operator account");
} catch (error) {
  record("login", "FAILED", String(error.message || error).slice(0, 200));
}
for (const route of pages) {
  for (const viewport of viewports) {
    const name = `layout:${route}@${viewport.name}`;
    try {
      const result = await openPage(authed, route, viewport);
      const problems = [];
      if (result.status >= 400) problems.push(`http=${result.status}`);
      if (result.errors.length) problems.push(`js_errors=${result.errors.length}`);
      if (result.overflow > 2) problems.push(`horizontal_overflow=${result.overflow}px`);
      record(name, problems.length ? "FAILED" : "PASSED", problems.join(" ") || "ok",
        { screenshot: path.basename(result.file), js_errors: result.errors });
      await result.page.close();
    } catch (error) {
      record(name, "FAILED", String(error.message || error).slice(0, 200));
    }
  }
}

// 2. Permission denied: an anonymous browser is sent to the login page.
const anonymous = await browser.newContext();
await guarded("permission_denied", async () => {
  const page = await anonymous.newPage();
  await page.goto(`${baseUrl}/nodes`, { waitUntil: "domcontentloaded" });
  const onLogin = new URL(page.url()).pathname.startsWith("/login");
  record("permission_denied", onLogin ? "PASSED" : "FAILED", `anonymous /nodes -> ${new URL(page.url()).pathname}`);
  await page.close();
});

// 3. Unknown/stale data is not rendered as zero.
await guarded("unknown_not_zero", async () => {
  const page = await authed.newPage();
  await page.goto(`${baseUrl}/`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(3000);
  const health = await page.evaluate(async () => {
    const response = await fetch("/api/dashboard/health", { credentials: "same-origin" });
    return response.json();
  });
  const monCard = await page.locator("text=MONs").first().locator("xpath=ancestor::*[self::div or self::section][1]")
    .innerText().catch(() => "");
  if (health.mons && health.mons.total === null) {
    const showsZero = /\b0\s*\/\s*0\b/.test(monCard);
    record("unknown_not_zero", showsZero ? "FAILED" : "PASSED",
      showsZero ? "MON card shows 0/0 for unknown data" : "unknown MON count is not shown as 0/0",
      { stale: health.stale, health: health.health });
  } else {
    record("unknown_not_zero", "SKIPPED", "cluster data is available; run against an unreachable cluster to exercise this");
  }
  await page.close();
});

// 4. Cluster switching keeps the selection on the requested cluster.
if (secondCluster) await guarded("cluster_switching", async () => {
  const page = await authed.newPage();
  await page.goto(`${baseUrl}/?cluster=${encodeURIComponent(secondCluster)}`, { waitUntil: "domcontentloaded" });
  const selected = await page.evaluate(async () => (await (await fetch("/api/dashboard/health", { credentials: "same-origin" })).json()).cluster_id);
  await page.goto(`${baseUrl}/nodes`, { waitUntil: "domcontentloaded" });
  const persisted = await page.evaluate(async () => (await (await fetch("/api/dashboard/health", { credentials: "same-origin" })).json()).cluster_id);
  const ok = selected === secondCluster && persisted === secondCluster;
  record("cluster_switching", ok ? "PASSED" : "FAILED", `selected=${selected} after_navigation=${persisted}`);
  await page.close();
});
else {
  record("cluster_switching", "SKIPPED", "SECOND_CLUSTER_ID not set");
}

// 5. Reconnect: going offline and back must not break the page.
await guarded("reconnect", async () => {
  const page = await authed.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error.message || error)));
  await page.goto(`${baseUrl}/`, { waitUntil: "domcontentloaded" });
  await authed.setOffline(true);
  await page.waitForTimeout(3000);
  await authed.setOffline(false);
  await page.waitForTimeout(4000);
  const status = await page.evaluate(async () => (await fetch("/api/dashboard/health", { credentials: "same-origin" })).status);
  const ok = status === 200 && errors.length === 0;
  record("reconnect", ok ? "PASSED" : "FAILED", `health_after_reconnect=${status} js_errors=${errors.length}`);
  await page.close();
});

// 6. Keyboard: Tab moves focus through interactive elements with a visible indicator.
await guarded("keyboard_focus", async () => {
  const page = await authed.newPage();
  await page.goto(`${baseUrl}/`, { waitUntil: "domcontentloaded" });
  const seen = [];
  let visible = 0;
  for (let index = 0; index < 8; index += 1) {
    await page.keyboard.press("Tab");
    const focus = await page.evaluate(() => {
      const element = document.activeElement;
      if (!element || element === document.body) return null;
      const style = window.getComputedStyle(element);
      return {
        tag: element.tagName.toLowerCase(),
        label: (element.getAttribute("aria-label") || element.textContent || "").trim().slice(0, 40),
        indicator: (style.outlineStyle !== "none" && parseFloat(style.outlineWidth) > 0) || style.boxShadow !== "none",
      };
    });
    if (focus) { seen.push(focus); visible += focus.indicator ? 1 : 0; }
  }
  const distinct = new Set(seen.map((item) => `${item.tag}:${item.label}`)).size;
  const ok = distinct >= 3 && visible >= 1;
  record("keyboard_focus", ok ? "PASSED" : "FAILED", `distinct_focus_targets=${distinct} visible_indicators=${visible}`);
  await page.close();
});

// 7. Alert Center verdicts (autonomy plan WP2.4): the "unlabelled" filter
// keeps its value, the Verdict column is present and every row left in the
// filtered view is either unlabelled or offers the quick verdict controls.
// Read-only: the verdict forms are inspected, never submitted.
await guarded("alert_verdicts", async () => {
  const page = await authed.newPage();
  await page.goto(`${baseUrl}/alerts?verdict=unlabelled`, { waitUntil: "domcontentloaded" });
  const state = await page.evaluate(() => {
    const headers = [...document.querySelectorAll(".alert-center-table thead th")].map((th) => th.textContent.trim());
    const rows = [...document.querySelectorAll(".alert-center-table tbody tr:not(.empty-row)")];
    const labelled = rows.filter((row) => row.querySelector(".alert-verdict-cell .alert-state-badge")).length;
    return {
      hasColumn: headers.includes("Verdict"),
      filter: document.querySelector("select[name=verdict]")?.value,
      rows: rows.length,
      quick: document.querySelectorAll(".alert-verdict-actions").length,
      labelled,
    };
  });
  const ok = state.hasColumn && state.filter === "unlabelled" && state.labelled === 0;
  record("alert_verdicts", ok ? "PASSED" : "FAILED",
    `column=${state.hasColumn} filter=${state.filter} rows=${state.rows} quick_forms=${state.quick} labelled_rows=${state.labelled}`);
  await page.close();
});

// 8. Self-learning progress card (autonomy plan WP8): the card fetches
// /api/ai-learning/autonomy-status and becomes visible with its four tiles
// filled in. Read-only.
await guarded("autonomy_status", async () => {
  const page = await authed.newPage();
  await page.goto(`${baseUrl}/ai-learning`, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => {
    const grid = document.getElementById("autonomy-status");
    return grid && !grid.hidden;
  }, null, { timeout: 30000 }).catch(() => {});
  const state = await page.evaluate(() => {
    const grid = document.getElementById("autonomy-status");
    const tiles = grid ? [...grid.querySelectorAll("[data-status]")] : [];
    return {
      present: Boolean(grid),
      visible: Boolean(grid && !grid.hidden),
      filled: tiles.filter((node) => node.textContent.trim() && node.textContent.trim() !== "—").length,
      tiles: tiles.length,
      learning: grid?.querySelector('[data-status="ol-scored"]')?.textContent.trim(),
    };
  });
  const ok = state.present && state.visible && state.filled >= 4;
  record("autonomy_status", ok ? "PASSED" : "FAILED",
    `present=${state.present} visible=${state.visible} filled=${state.filled}/${state.tiles} learning=${state.learning}`);
  await page.close();
});

// 9. Test and CI progress page (/test-progress): both sections render after
// the first poll. Read-only.
await guarded("test_progress", async () => {
  const page = await authed.newPage();
  await page.goto(`${baseUrl}/test-progress`, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => {
    const runs = document.querySelectorAll("#ci-runs tr");
    return runs.length && !runs[0].textContent.includes("Đang tải");
  }, null, { timeout: 30000 }).catch(() => {});
  const state = await page.evaluate(() => ({
    local: !document.getElementById("local")?.hidden || !document.getElementById("local-empty")?.hidden,
    ciRows: document.querySelectorAll("#ci-runs tr").length,
    ciError: document.getElementById("ci-error")?.hidden === false,
  }));
  const ok = state.local && state.ciRows > 0;
  record("test_progress", ok ? "PASSED" : "FAILED",
    `local_section=${state.local} ci_rows=${state.ciRows} ci_error=${state.ciError}`);
  await page.close();
});

// Ceph cluster stream (Plan/in-progress/ceph-cluster-stream-plan-2026-10-06.md):
// /stream#ceph opens the cluster tab with its service graph. Read-only.
await guarded("ceph_cluster_stream", async () => {
  const page = await authed.newPage();
  await page.goto(`${baseUrl}/stream#ceph`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".ceph-cluster-stream [data-node-id]", { timeout: 30000 }).catch(() => {});
  const state = await page.evaluate(() => ({
    active: document.querySelector(".stream-tabs button.is-active")?.textContent || "",
    nodes: document.querySelectorAll(".ceph-cluster-stream [data-node-id]").length,
    groups: document.querySelectorAll(".ceph-cluster-stream .installation-stream__group-label").length,
    overflow: document.documentElement.scrollWidth > window.innerWidth + 1,
  }));
  const ok = state.active.startsWith("Cụm Ceph") && state.nodes > 0 && state.groups > 0 && !state.overflow;
  record("ceph_cluster_stream", ok ? "PASSED" : "FAILED",
    `tab=${state.active} nodes=${state.nodes} groups=${state.groups} overflow=${state.overflow}`);
  await page.close();
});

await browser.close();
const failed = checks.filter((item) => item.status === "FAILED");
const report = {
  schema: "ceph-ai.browser-acceptance.v1",
  generated_at: new Date().toISOString(),
  sha,
  base_url: baseUrl,
  status: failed.length ? "FAILED" : "PASSED",
  failed: failed.length,
  checks,
};
fs.writeFileSync(path.join(outDir, "browser-acceptance.json"), `${JSON.stringify(report, null, 2)}\n`);
console.log(`BROWSER ACCEPTANCE ${report.status}: ${checks.length - failed.length}/${checks.length} checks passed -> ${outDir}`);
process.exit(failed.length ? 1 : 0);
