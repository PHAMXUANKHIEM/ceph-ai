(() => {
  "use strict";

  const byId = (id) => document.getElementById(id);
  const select = byId("vitastor-cluster-select");
  const monitor = byId("vitastor-monitor");
  const notice = byId("vitastor-live-notice");
  let loading = false;
  let historyPoints = [];

  const number = (value, digits = 0) => Number(value || 0).toLocaleString("vi-VN", {
    maximumFractionDigits: digits,
  });
  const bytes = (value) => {
    let amount = Number(value || 0);
    if (!Number.isFinite(amount) || amount <= 0) return "0 B";
    const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
    let index = 0;
    while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; }
    return `${number(amount, amount < 10 ? 2 : 1)} ${units[index]}`;
  };
  const rate = (value) => `${bytes(value)}/s`;
  const latency = (value) => {
    const microseconds = Number(value || 0);
    return microseconds ? `${number(microseconds / 1000, 2)} ms` : "—";
  };
  const setText = (id, value) => { const element = byId(id); if (element) element.textContent = value; };
  const child = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const showNotice = (message, tone = "info") => {
    if (!notice) return;
    notice.textContent = message;
    notice.className = `vitastor-live-notice is-${tone}`;
    notice.hidden = !message;
  };
  const stat = (source, key) => source && typeof source[key] === "object" ? source[key] : {};

  function renderSummary(data) {
    const summary = data.summary || {};
    const health = summary.health || "UNKNOWN";
    const healthElement = byId("vita-health");
    setText("vita-health", health);
    if (healthElement) healthElement.className = `health-${health.toLowerCase()}`;
    setText("vita-checked-at", data.checked_at ? `Cập nhật ${new Date(data.checked_at).toLocaleString("vi-VN")}` : "Không rõ thời điểm");

    const etcd = summary.etcd || {}, mon = summary.mon || {}, osds = summary.osds || {}, pools = summary.pools || {}, servers = summary.servers || {};
    setText("vita-etcd", `${number(etcd.up)} / ${number(etcd.total)} up`);
    setText("vita-etcd-db", `Database ${bytes(etcd.db_size)}`);
    const etcdDetail = data.etcd_detail || {};
    setText("vita-etcd-latency", etcdDetail.latency_ms === null || etcdDetail.latency_ms === undefined ? "—" : `${number(etcdDetail.latency_ms, 2)} ms`);
    setText("vita-etcd-quorum", etcdDetail.total ? `Quorum ${etcdDetail.quorum ? "OK" : "LOST"} · Leader ${number(etcdDetail.leader_count)}` : "Etcdctl unavailable");
    setText("vita-mon", `${number(mon.count)} monitor`);
    setText("vita-mon-master", mon.master ? `Master ${mon.master}` : "Chưa có master");
    setText("vita-osd", `${number(osds.up)} / ${number(osds.total)} up`);
    setText("vita-osd-note", `Full ${number(osds.full)} · Nearfull ${number(osds.nearfull)}`);
    setText("vita-servers", `${number(servers.online)} / ${number(servers.total)}`);
    setText("vita-pools", `${number(pools.active)} / ${number(pools.total)} active`);
    setText("vita-pool-note", `${(pools.backfillfull || []).length} backfillfull`);

    const capacity = summary.capacity || {};
    setText("vita-utilization", `${number(capacity.used_percent, 2)}%`);
    setText("vita-utilization-note", `${bytes(capacity.used)} in ${number(pools.total)} pools`);
    setText("vita-capacity-percent", `${number(capacity.used_percent, 2)}%`);
    setText("vita-used", bytes(capacity.used));
    setText("vita-free", bytes(capacity.free));
    setText("vita-down", bytes(capacity.down));
    const fill = byId("vita-capacity-fill");
    if (fill) fill.style.width = `${Math.min(100, Math.max(0, Number(capacity.used_percent || 0)))}%`;

    renderPgStates(summary.pg_states || {});
    const read = stat(summary.io, "read"), write = stat(summary.io, "write");
    setText("vita-read-iops", number(read.iops, 1));
    setText("vita-write-iops", number(write.iops, 1));
    setText("vita-read-bw", rate(read.bps));
    setText("vita-write-bw", rate(write.bps));
    const metrics = summary.metrics || {};
    setText("vita-latency", metrics.latency_ms === null || metrics.latency_ms === undefined ? "—" : `${number(metrics.latency_ms, 2)} ms`);
    setText("vita-bandwidth", rate(metrics.bandwidth_bps));
    setText("vita-iops", number(metrics.iops, 1));
    setText("vita-placement-state", summary.placement_groups || "UNKNOWN");
    const recovery = summary.recovery || {};
    const recoveryIops = Object.values(recovery).reduce((sum, item) => sum + Number(item?.iops || 0), 0);
    const recoveryBps = Object.values(recovery).reduce((sum, item) => sum + Number(item?.bps || 0), 0);
    setText("vita-recovery", `${number(recoveryIops, 1)} IOPS · ${rate(recoveryBps)}`);
    renderObjectHealth(summary);
  }

  function renderPgStates(states) {
    const root = byId("vita-pg-states");
    if (!root) return;
    root.replaceChildren();
    const entries = Object.entries(states).sort((a, b) => Number(b[1]) - Number(a[1]));
    const total = entries.reduce((sum, item) => sum + Number(item[1] || 0), 0);
    setText("vita-pg-total", `${number(total)} PG`);
    setText("vita-pg-summary", `${number(total)} PG total`);
    if (!entries.length) { root.append(child("p", "Không có dữ liệu PG", "vitastor-muted")); return; }
    for (const [state, count] of entries) {
      const row = child("div", undefined, "vitastor-state-row");
      const tone = state === "active" ? "is-healthy" : state.includes("inactive") || state.includes("incomplete") ? "is-critical" : "is-warning";
      row.append(child("i", "", tone), child("span", state), child("strong", number(count)));
      root.append(row);
    }
  }

  function renderObjectHealth(summary) {
    const root = byId("vita-object-health");
    if (root) {
      root.replaceChildren();
      const values = summary.data_states || {};
      for (const key of ["clean", "misplaced", "degraded", "incomplete"]) {
        const card = child("div", undefined, `is-${key}`);
        card.append(child("span", key), child("strong", bytes(values[key])));
        root.append(card);
      }
    }
    const flags = byId("vita-flags");
    if (!flags) return;
    flags.replaceChildren();
    const active = summary.flags || [];
    flags.append(child("span", active.length ? `Flags: ${active.join(", ")}` : "Không có cluster flag", active.length ? "is-warning" : "is-ok"));
  }

  const emptyRow = (columns, message) => {
    const row = child("tr"), cell = child("td", message, "vitastor-table-empty");
    cell.colSpan = columns; row.append(cell); return row;
  };
  const cell = (value, className) => child("td", value, className);

  function renderOsds(items) {
    const root = byId("vita-osd-table"); if (!root) return;
    root.replaceChildren(); setText("vita-osd-count", `${items.length} items`);
    if (!items.length) { root.append(emptyRow(8, "Không có dữ liệu topology OSD")); return; }
    for (const item of items) {
      const row = child("tr");
      const isOsd = item.type === "osd";
      const size = Number(item.size || 0), free = Number(item.free || 0);
      const used = size ? Math.max(0, (size - free) / size * 100) : 0;
      const read = stat(item.op_stats, "read"), write = stat(item.op_stats, "write");
      row.append(
        cell(`${isOsd ? "OSD " : ""}${item.name ?? item.id ?? "—"}`, isOsd ? "vitastor-mono" : "vitastor-node"),
        cell(String(item.parent ?? "—")),
        cell(isOsd ? (item.up ? "UP" : "DOWN") : String(item.type || "node"), item.up || !isOsd ? "is-up" : "is-down"),
        cell(size ? bytes(size) : "—"), cell(size ? `${number(used, 1)}%` : "—"),
        cell(isOsd ? `${number(read.iops, 1)} / ${number(write.iops, 1)}` : "—"),
        cell(isOsd ? `${rate(read.bps)} / ${rate(write.bps)}` : "—"),
        cell(isOsd ? `R ${latency(read.latency_us ?? read.usec ?? read.lat)} · W ${latency(write.latency_us ?? write.usec ?? write.lat)}` : "—")
      );
      root.append(row);
    }
  }

  function drawHistory() {
    const canvas=byId("vita-history-chart"), empty=byId("vita-history-empty"); if(!canvas)return;
    const metric=byId("vita-history-metric")?.value||"iops", dpr=window.devicePixelRatio||1, width=canvas.clientWidth||900, height=220;
    canvas.width=width*dpr;canvas.height=height*dpr;const ctx=canvas.getContext("2d");ctx.scale(dpr,dpr);ctx.clearRect(0,0,width,height);
    if(empty)empty.hidden=historyPoints.length>1;if(historyPoints.length<2)return;
    const values=historyPoints.map(p=>metric==="iops"?Number(p.read_iops)+Number(p.write_iops):metric==="bandwidth"?Number(p.read_bps)+Number(p.write_bps):metric==="latency"?Math.max(Number(p.read_latency_ms||0),Number(p.write_latency_ms||0)):metric==="capacity"?Number(p.used_percent):Number(p.recovery_bps));
    const max=Math.max(1,...values),pad=28;ctx.strokeStyle="rgba(108,190,173,.14)";ctx.lineWidth=1;for(let i=0;i<4;i++){const y=pad+(height-pad*2)*i/3;ctx.beginPath();ctx.moveTo(pad,y);ctx.lineTo(width-pad,y);ctx.stroke();}
    ctx.strokeStyle="#55d8bc";ctx.lineWidth=2;ctx.beginPath();values.forEach((v,i)=>{const x=pad+(width-pad*2)*i/(values.length-1),y=height-pad-(height-pad*2)*v/max;i?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.stroke();
    ctx.fillStyle="#789b93";ctx.font="11px sans-serif";ctx.fillText(metric==="capacity"?`${number(max,1)}%`:metric==="latency"?`${number(max,2)} ms`:metric==="iops"?`${number(max,1)} IOPS`:rate(max),pad,15);
  }
  async function loadHistory(clusterId){try{const data=await fetch(`/vitastor/api/metrics/history?cluster_id=${encodeURIComponent(clusterId)}&hours=24`,{credentials:"same-origin"}).then(r=>r.json());historyPoints=data.points||[];drawHistory();}catch(_){historyPoints=[];drawHistory();}}

  function renderPools(items) {
    const root = byId("vita-pool-table"); if (!root) return;
    root.replaceChildren(); setText("vita-pool-count", `${items.length} items`);
    if (!items.length) { root.append(emptyRow(6, "Không có dữ liệu pool")); return; }
    for (const item of items) {
      const status = String(item.status || "unknown");
      const row = child("tr");
      row.append(cell(String(item.name || item.id || "—"), "vitastor-mono"), cell(String(item.scheme_name || "—")),
        cell(`${number(item.real_pg_count ?? item.pg_count)} / ${number(item.pg_count)}`),
        cell(status, status === "active" ? "is-up" : "is-down"),
        cell(`${bytes(item.used_raw)} / ${bytes(item.total_raw)}`),
        cell(`${number(Number(item.space_efficiency || 0) * 100, 1)}%`));
      root.append(row);
    }
  }

  function renderImages(items) {
    const root = byId("vita-image-table"); if (!root) return;
    root.replaceChildren(); setText("vita-image-count", `${items.length} images`);
    if (!items.length) { root.append(emptyRow(8, "Không có image / volume")); return; }
    for (const item of items) {
      const row = child("tr");
      row.append(cell(String(item.name || "—"), "vitastor-mono"), cell(String(item.pool_name || item.pool_id || "—")),
        cell(bytes(item.size)), cell(bytes(item.used_size)), cell(item.readonly ? "Read only" : "Read / write"),
        cell(number(item.read_iops, 1)), cell(number(item.write_iops, 1)),
        cell(`R ${latency(item.read_lat)} · W ${latency(item.write_lat)}`));
      root.append(row);
    }
  }

  function renderDiagnosis(diagnostic) {
    const empty=byId("vita-diagnosis-empty"),root=byId("vita-diagnosis-result");
    if(!diagnostic){if(empty)empty.hidden=false;if(root)root.hidden=true;return;}
    if(empty)empty.hidden=true;if(root)root.hidden=false;
    const result=diagnostic.result||{};
    setText("vita-diagnosis-health",diagnostic.health||"UNKNOWN");setText("vita-diagnosis-confidence",result.confidence||"—");
    setText("vita-diagnosis-status",diagnostic.status||"—");setText("vita-diagnosis-time",diagnostic.finished_at?new Date(diagnostic.finished_at).toLocaleString("vi-VN"):"—");
    setText("vita-diagnosis-cause",result.root_cause||diagnostic.error||"—");setText("vita-diagnosis-impact",result.impact||"—");
    const fillList=(id,items)=>{const list=byId(id);if(!list)return;list.replaceChildren();(items||[]).forEach(item=>list.append(child("li",item)));};
    fillList("vita-diagnosis-evidence",result.evidence);fillList("vita-diagnosis-steps",result.recommended_steps);
    setText("vita-diagnosis-commands",(result.commands_preview||[]).join("\n")||"Không có command preview");
    const safety=byId("vita-diagnosis-safety");if(safety){safety.replaceChildren();(result.safety_notes||[]).forEach(item=>safety.append(child("span",item,"is-warning")));}
  }
  async function loadLatestDiagnosis(){if(!select)return;try{const response=await fetch(`/vitastor/api/diagnostics/latest?cluster_id=${encodeURIComponent(select.value)}`);const data=await response.json();if(response.ok)renderDiagnosis(data.diagnostic);}catch(_){renderDiagnosis(null);}}

  function renderHardware(nodes) {
    const root=byId("vita-hardware-table");if(!root)return;root.replaceChildren();setText("vita-hardware-count",`${nodes.length} nodes`);
    if(!nodes.length){root.append(emptyRow(8,"Chưa có hardware sample từ Watcher"));return;}
    nodes.forEach(node=>{const devices=node.devices||[];if(!devices.length){const row=child("tr");row.append(cell(node.host||"—","vitastor-mono"),cell(number(node.osd_processes)),cell(`${number(node.cpu_percent,1)}%`),cell(bytes(node.ram_bytes)),cell("—"),cell("—"),cell("—"),cell(node.error||"No device data"));root.append(row);return;}devices.forEach((device,index)=>{const row=child("tr");row.append(cell(index?device.device:`${node.host} · ${device.device}`,"vitastor-mono"),cell(number(node.osd_processes)),cell(`${number(node.cpu_percent,1)}%`),cell(bytes(node.ram_bytes)),cell(device.temperature_c==null?"—":`${number(device.temperature_c,1)}°C`),cell(device.wear_percent==null?"—":`${number(device.wear_percent,1)}%`),cell(number(device.media_errors)),cell(device.smart_passed===false?"FAILING":device.smart_passed===true?"PASSED":"UNKNOWN",device.smart_passed===false?"is-down":"is-up"));root.append(row);});});
  }
  function renderNetwork(sources){const root=byId("vita-network-table");if(!root)return;root.replaceChildren();const count=sources.reduce((n,s)=>n+(s.probes||[]).length,0);setText("vita-network-count",`${count} paths`);if(!count){root.append(emptyRow(8,"Chưa có network sample từ Watcher"));return;}sources.forEach(source=>(source.probes||[]).forEach(probe=>{const nics=source.interfaces||[],active=nics.filter(n=>n.state==="up"),errors=nics.reduce((sum,n)=>sum+Number(n.rx_errors||0)+Number(n.rx_dropped||0)+Number(n.tx_errors||0)+Number(n.tx_dropped||0),0),row=child("tr");row.append(cell(source.source||"—","vitastor-mono"),cell(probe.target||"—","vitastor-mono"),cell(probe.reachable?"YES":"NO",probe.reachable?"is-up":"is-down"),cell(probe.rtt_ms==null?"—":`${number(probe.rtt_ms,2)} ms`),cell(probe.jumbo_9000?"PASS":"FAIL",probe.jumbo_9000?"is-up":"is-down"),cell(active.map(n=>`${n.name} MTU ${n.mtu}`).join(", ")||"—"),cell(active.map(n=>`${n.speed_mbps} Mb/s`).join(", ")||"—"),cell(number(errors),errors?"is-down":"is-up"));root.append(row);}));}

  async function loadOverview() {
    if (!select || loading) return;
    loading = true;
    const refresh = byId("vitastor-refresh"); if (refresh) refresh.disabled = true;
    showNotice("Đang đọc telemetry trực tiếp từ Vitastor…");
    try {
      const response = await fetch(`/vitastor/api/overview?cluster_id=${encodeURIComponent(select.value)}`, {headers: {Accept: "application/json"}});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Không tải được telemetry");
      if (monitor) monitor.hidden = false;
      renderSummary(data); renderOsds(data.osds || []); renderPools(data.pools || []); renderImages(data.images || []); renderHardware(data.hardware || []); renderNetwork(data.network || []); loadHistory(select.value);
      const sectionErrors = Object.entries(data.section_errors || {});
      if (data.stale) showNotice(`Đang hiển thị dữ liệu cache: ${data.error || "mất kết nối cluster"}`, "warning");
      else if (sectionErrors.length) showNotice(`Một số mục chưa đọc được: ${sectionErrors.map(([name]) => name).join(", ")}`, "warning");
      else showNotice("");
    } catch (error) {
      showNotice(error.message || "Không thể tải dashboard Vitastor", "critical");
    } finally {
      loading = false; if (refresh) refresh.disabled = false;
    }
  }
  if(byId("vita-history-metric"))byId("vita-history-metric").onchange=drawHistory;

  const dialog = byId("vitastor-cluster-dialog");
  document.querySelectorAll("#vitastor-add-cluster, [data-open-vitastor-cluster]").forEach((button) => button.addEventListener("click", () => dialog?.showModal()));
  document.querySelectorAll("[data-close-vitastor-cluster]").forEach((button) => button.addEventListener("click", () => dialog?.close()));
  const mode = byId("vitastor-exec-mode"), containerField = byId("vitastor-container-field");
  mode?.addEventListener("change", () => { if (containerField) containerField.hidden = mode.value === "none"; });
  byId("vitastor-cluster-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget, submit = form.querySelector("button[type=submit]"), error = byId("vitastor-cluster-error");
    submit.disabled = true; submit.textContent = "Đang xác minh…"; if (error) error.hidden = true;
    try {
      const response = await fetch("/vitastor/clusters", {method: "POST", body: new FormData(form)});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Không thể kết nối cụm");
      window.location.reload();
    } catch (reason) {
      if (error) { error.textContent = reason.message; error.hidden = false; }
      submit.disabled = false; submit.textContent = "Xác minh & kết nối";
    }
  });
  byId("vitastor-refresh")?.addEventListener("click", loadOverview);
  byId("vita-diagnose")?.addEventListener("click",async(event)=>{const button=event.currentTarget;if(!select)return;button.disabled=true;button.textContent="AI đang phân tích…";showNotice("Đang thu thập evidence read-only và chẩn đoán…");try{const form=new FormData();form.append("cluster_id",select.value);const response=await fetch("/vitastor/api/diagnostics",{method:"POST",body:form});const data=await response.json();if(!response.ok)throw new Error(data.detail||"Chẩn đoán thất bại");renderDiagnosis(data.diagnostic);showNotice("");}catch(error){showNotice(error.message||"AI chẩn đoán thất bại","critical");await loadLatestDiagnosis();}finally{button.disabled=false;button.textContent="Phân tích bằng AI";}});
  select?.addEventListener("change",()=>{loadOverview();loadLatestDiagnosis();});
  if (select) { loadOverview(); loadLatestDiagnosis(); window.setInterval(loadOverview, 30000); }
})();
