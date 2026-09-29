import React from "react";
import { createRoot } from "react-dom/client";
import { CephDashboard, type DashboardHealth } from "./components/CephDashboard";
import { PoolsPage } from "./components/PoolsPage";
import { InstallationStream, type InstallationProfile } from "./components/InstallationStream";
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
    createRoot(streamRoot).render(<React.StrictMode><InstallationStream profile={profile} /></React.StrictMode>);
  } catch (error) {
    console.error("Invalid installation-stream bootstrap data", error);
    streamRoot.textContent = "Không đọc được profile cấu hình. Hãy tải lại trang.";
  }
}
