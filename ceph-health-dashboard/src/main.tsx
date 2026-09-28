import React from "react";
import { createRoot } from "react-dom/client";
import { CephDashboard } from "./components/CephDashboard";
import { PoolsPage } from "./components/PoolsPage";
import { InstallationStream } from "./components/InstallationStream";
import "./styles.css";

const root = document.getElementById("ceph-dashboard-root");
if (root) createRoot(root).render(<React.StrictMode><CephDashboard /></React.StrictMode>);

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
  createRoot(streamRoot).render(
    <React.StrictMode><InstallationStream profile={JSON.parse(streamData.textContent)} /></React.StrictMode>,
  );
}
