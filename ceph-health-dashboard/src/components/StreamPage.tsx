import { useCallback, useEffect, useState } from "react";
import { AiFlowStream, type AiFlow } from "./AiFlowStream";
import { CephClusterStream, type CephTopology } from "./CephClusterStream";
import { InstallationStream, type InstallationProfile } from "./InstallationStream";

// /stream: the Ceph AI system stream, the selected Ceph cluster's service
// stream, the Failure Lab staging cluster's (when it is another cluster) and
// the AI loop as tabs; the open tab is kept in the URL hash.

type Tab = "system" | "ceph" | "staging" | "ai";
const HASH_TABS: Record<string, Tab> = { "#ceph": "ceph", "#staging": "staging", "#ai": "ai" };

function tabFromHash(): Tab {
  return HASH_TABS[window.location.hash] || "system";
}

export function StreamPage({ profile, topology, staging = null, aiFlow }: {
  profile: InstallationProfile; topology: CephTopology | null; staging?: CephTopology | null; aiFlow: AiFlow | null;
}) {
  const available = useCallback((wanted: Tab): Tab => (
    (wanted === "ceph" && !topology) || (wanted === "staging" && !staging) || (wanted === "ai" && !aiFlow)
      ? "system" : wanted
  ), [topology, staging, aiFlow]);
  const [tab, setTab] = useState<Tab>(() => available(tabFromHash()));
  useEffect(() => {
    const onHash = () => setTab(available(tabFromHash()));
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, [available]);
  const choose = (next: Tab) => {
    setTab(next);
    window.history.replaceState(null, "", next === "system" ? window.location.pathname + window.location.search : `#${next}`);
  };
  return (
    <>
      <nav className="stream-tabs" role="tablist" aria-label="Chọn luồng">
        <button type="button" role="tab" aria-selected={tab === "system"} className={tab === "system" ? "is-active" : ""} onClick={() => choose("system")}>Hệ thống Ceph AI</button>
        <button type="button" role="tab" aria-selected={tab === "ceph"} className={tab === "ceph" ? "is-active" : ""} onClick={() => choose("ceph")} disabled={!topology} title={topology ? undefined : "Chưa chọn cụm Ceph"}>Cụm Ceph{topology ? ` · ${topology.cluster.name}` : ""}</button>
        {staging && <button type="button" role="tab" aria-selected={tab === "staging"} className={tab === "staging" ? "is-active" : ""} onClick={() => choose("staging")}>Cụm Staging · {staging.cluster.name}</button>}
        <button type="button" role="tab" aria-selected={tab === "ai"} className={tab === "ai" ? "is-active" : ""} onClick={() => choose("ai")} disabled={!aiFlow} title={aiFlow ? undefined : "Chưa đọc được số liệu luồng AI"}>Luồng AI</button>
      </nav>
      {tab === "ceph" && topology ? <CephClusterStream key="ceph" initial={topology} />
        : tab === "staging" && staging ? <CephClusterStream key="staging" initial={staging} source="/api/stream/staging-topology" />
        : tab === "ai" && aiFlow ? <AiFlowStream initial={aiFlow} />
        : <InstallationStream profile={profile} />}
    </>
  );
}
