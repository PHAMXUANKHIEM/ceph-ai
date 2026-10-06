import { useEffect, useState } from "react";
import { CephClusterStream, type CephTopology } from "./CephClusterStream";
import { InstallationStream, type InstallationProfile } from "./InstallationStream";

// /stream: the Ceph AI system stream and the selected Ceph cluster's
// service stream as two tabs; the open tab is kept in the URL hash.

type Tab = "system" | "ceph";

function tabFromHash(): Tab {
  return window.location.hash === "#ceph" ? "ceph" : "system";
}

export function StreamPage({ profile, topology }: { profile: InstallationProfile; topology: CephTopology | null }) {
  const [tab, setTab] = useState<Tab>(() => (tabFromHash() === "ceph" && topology ? "ceph" : "system"));
  useEffect(() => {
    const onHash = () => setTab(tabFromHash() === "ceph" && topology ? "ceph" : "system");
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, [topology]);
  const choose = (next: Tab) => {
    setTab(next);
    window.history.replaceState(null, "", next === "ceph" ? "#ceph" : window.location.pathname + window.location.search);
  };
  return (
    <>
      <nav className="stream-tabs" role="tablist" aria-label="Chọn luồng">
        <button type="button" role="tab" aria-selected={tab === "system"} className={tab === "system" ? "is-active" : ""} onClick={() => choose("system")}>Hệ thống Ceph AI</button>
        <button type="button" role="tab" aria-selected={tab === "ceph"} className={tab === "ceph" ? "is-active" : ""} onClick={() => choose("ceph")} disabled={!topology} title={topology ? undefined : "Chưa chọn cụm Ceph"}>Cụm Ceph{topology ? ` · ${topology.cluster.name}` : ""}</button>
      </nav>
      {tab === "ceph" && topology ? <CephClusterStream initial={topology} /> : <InstallationStream profile={profile} />}
    </>
  );
}
