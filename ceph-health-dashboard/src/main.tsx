import React from "react";
import { createRoot } from "react-dom/client";
import { CephDashboard, type DashboardHealth } from "./components/CephDashboard";
import { PoolsPage } from "./components/PoolsPage";
import type { InstallationProfile } from "./components/InstallationStream";
import type { AiFlow } from "./components/AiFlowStream";
import type { CephTopology } from "./components/CephClusterStream";
import { StreamPage } from "./components/StreamPage";
import "./styles.css";

const root = document.getElementById("ceph-dashboard-root");
const dashboardBootstrap = document.getElementById("dashboard-bootstrap-data");
let initialHealth: DashboardHealth | undefined;
if (dashboardBootstrap?.textContent) {
  try {
    initialHealth = JSON.parse(dashboardBootstrap.textContent) as DashboardHealth;
  } catch {
    // The normal API fetch remains the source of truth if bootstrap data is
    // malformed or missing during a rolling deployment.
  }
}
if (root) createRoot(root).render(<React.StrictMode><CephDashboard initialHealth={initialHealth} /></React.StrictMode>);

const poolsRoot = document.getElementById("pools-dashboard-root");
const poolsData = document.getElementById("pools-bootstrap-data");
if (poolsRoot && poolsData?.textContent) {
  createRoot(poolsRoot).render(
    <React.StrictMode><PoolsPage bootstrap={JSON.parse(poolsData.textContent)} /></React.StrictMode>,
  );
}

const streamRoot = document.getElementById("installation-stream-root");
const streamData = document.getElementById("installation-stream-bootstrap");
if (streamRoot && streamData?.textContent) {
  try {
    const profile = JSON.parse(streamData.textContent) as InstallationProfile;
    let topology: CephTopology | null = null;
    try {
      topology = JSON.parse(document.getElementById("ceph-topology-bootstrap")?.textContent || "null") as CephTopology | null;
    } catch (error) {
      // The system stream still renders; the Ceph tab stays disabled.
      console.error("Invalid ceph-topology bootstrap data", error);
    }
    let aiFlow: AiFlow | null = null;
    try {
      aiFlow = JSON.parse(document.getElementById("ai-flow-bootstrap")?.textContent || "null") as AiFlow | null;
    } catch (error) {
      // The other tabs still render; the AI tab stays disabled.
      console.error("Invalid ai-flow bootstrap data", error);
    }
    createRoot(streamRoot).render(<React.StrictMode><StreamPage profile={profile} topology={topology} aiFlow={aiFlow} /></React.StrictMode>);
  } catch (error) {
    console.error("Invalid installation-stream bootstrap data", error);
    streamRoot.textContent = "Không đọc được profile cấu hình. Hãy tải lại trang.";
  }
}
