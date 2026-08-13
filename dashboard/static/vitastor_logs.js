(function(){
  var cluster=document.getElementById("vl-cluster"),host=document.getElementById("vl-host"),source=document.getElementById("vl-source"),preset=document.getElementById("vl-preset"),limit=document.getElementById("vl-lines"),keyword=document.getElementById("vl-keyword"),output=document.getElementById("vl-output"),status=document.getElementById("vl-status"),auto=document.getElementById("vl-auto"),timer=null,loading=false;
  var hostData={};
  try{hostData=JSON.parse(document.getElementById("vl-cluster-hosts").textContent||"{}");}catch(e){}
  function populateHosts(){
    var hosts=hostData[cluster&&cluster.value]||[];
    host.replaceChildren();
    hosts.forEach(function(value){var option=document.createElement("option");option.value=value;option.textContent=value;host.appendChild(option);});
  }
  function load(){
    if(!cluster||!cluster.value||!host.value||loading)return;
    loading=true;status.textContent="Đang tải…";
    var q=new URLSearchParams({cluster_id:cluster.value,host:host.value,source:source.value,preset:preset.value,lines:limit.value,keyword:keyword.value.trim()});
    fetch("/vitastor/api/logs?"+q.toString(),{credentials:"same-origin"}).then(function(r){return r.json().then(function(d){if(!r.ok)throw new Error(d.detail||"HTTP "+r.status);return d;});}).then(function(d){output.textContent=d.lines.length?d.lines.join("\n"):"Không có dòng log phù hợp.";status.textContent=d.count+" dòng · "+d.host+" · "+new Date(d.fetched_at).toLocaleString("vi-VN");}).catch(function(e){output.textContent="";status.textContent="❌ "+e.message;}).finally(function(){loading=false;});
  }
  document.getElementById("vl-apply").onclick=load;
  keyword.addEventListener("keydown",function(e){if(e.key==="Enter")load();});
  document.getElementById("vl-clear").onclick=function(){keyword.value="";preset.value="all";load();};
  document.getElementById("vl-copy").onclick=function(){navigator.clipboard.writeText(output.textContent||"");};
  auto.onchange=function(){if(timer)clearInterval(timer);timer=auto.checked?setInterval(load,10000):null;};
  cluster&&cluster.addEventListener("change",function(){populateHosts();load();});
  host.addEventListener("change",load);source.addEventListener("change",load);preset.addEventListener("change",load);limit.addEventListener("change",load);
  populateHosts();if(cluster&&cluster.value)load();
}());
