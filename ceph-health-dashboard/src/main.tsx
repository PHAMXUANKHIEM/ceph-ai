import React from "react";
import { createRoot } from "react-dom/client";
import { CephDashboard } from "./components/CephDashboard";
import "./styles.css";

const root = document.getElementById("ceph-dashboard-root");
if (root) createRoot(root).render(<React.StrictMode><CephDashboard /></React.StrictMode>);
