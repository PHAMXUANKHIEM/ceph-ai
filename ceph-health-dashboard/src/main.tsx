import React from "react";
import { createRoot } from "react-dom/client";
import { CephDashboard, type DashboardHealth } from "./components/CephDashboard";
import { PoolsPage } from "./components/PoolsPage";
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
