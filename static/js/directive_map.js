(() => {
  "use strict";
  const $ = (q, root=document) => root.querySelector(q);
  const state = { agents: [], agent: "", nodes: [], links: [], route: [], step: 0, paused: false, running: false, raf: 0, token: 0 };
  const NS = "http://www.w3.org/2000/svg";
  const esc = (s) => String(s || "").replace(/\s+/g, " ").trim();
  const el = (name, attrs={}) => { const n=document.createElementNS(NS,name); Object.entries(attrs).forEach(([k,v])=>n.setAttribute(k,String(v))); return n; };

  async function api(url, opts={}) {
    const r = await fetch(url, {headers:{"Content-Type":"application/json", ...(opts.headers||{})}, ...opts});
    if (!r.ok) throw new Error((await r.text()) || `HTTP ${r.status}`);
    return r.json();
  }

  function labelOf(agent){ return agent?.name || agent?.slug || "Directive"; }
  function meaningfulLines(text){
    return String(text||"").split(/\r?\n/).map(x=>x.trim()).filter(x=>x && !/^[-=*_]{3,}$/.test(x));
  }
  function normalizeTitle(line){
    let t=line.replace(/^[-*•\d.)\s]+/,"").replace(/^(SYSTEM|PERSONA|ROLE|OPERATING RULES?|RULES?|DIRECTIVE)\s*:?\s*/i,"");
    if (t.includes(":")) t=t.split(":",1)[0];
    const words=esc(t).split(" ").slice(0,5).join(" ");
    return words || "Directive step";
  }
  function buildModel(agent){
    const lines=meaningfulLines(agent.directive);
    const root={id:"root", title:(agent.name||agent.slug||"DIRECTIVE").toUpperCase(), sub:"ACTIVE DIRECTIVE", depth:0};
    const raw=[];
    const seen=new Set();
    for(const line of lines){
      const cleaned=line.replace(/^[-*•\s]+/,"");
      if (cleaned.length<4) continue;
      const title=normalizeTitle(cleaned);
      const key=title.toLowerCase();
      if(!title || seen.has(key) || /^you are$/i.test(title)) continue;
      seen.add(key);
      raw.push({title: title.toUpperCase(), sub: esc(cleaned).slice(0,72)});
      if(raw.length>=18) break;
    }
    if(!raw.length) return {nodes:[root],links:[],route:[]};
    const branches=Math.min(6, Math.max(3, Math.ceil(Math.sqrt(raw.length))));
    const nodes=[root], links=[];
    raw.forEach((r,i)=>{
      const branch=i%branches, ring=Math.floor(i/branches)+1;
      const id=`n${i}`;
      nodes.push({id,...r,depth:ring,branch});
      const parent = ring===1 ? "root" : `n${i-branches}`;
      links.push({id:`l${i}`,from:parent,to:id});
    });
    return {nodes,links,route:links.map(l=>l.id)};
  }
  function layout(model){
    const root=model.nodes[0]; root.x=700; root.y=410;
    const maxDepth=Math.max(1,...model.nodes.map(n=>n.depth));
    const byDepth=new Map();
    model.nodes.slice(1).forEach(n=>{ const a=byDepth.get(n.depth)||[]; a.push(n); byDepth.set(n.depth,a); });
    byDepth.forEach((arr,depth)=>{
      const radius=155 + (depth-1)*145;
      const phase=(depth%2 ? -Math.PI/2 : -Math.PI/2 + .28);
      arr.forEach((n,i)=>{ const angle=phase + (Math.PI*2*i/arr.length); n.x=700+Math.cos(angle)*radius; n.y=410+Math.sin(angle)*radius*.74; });
    });
    return model;
  }
  function curve(a,b){
    const mx=(a.x+b.x)/2, my=(a.y+b.y)/2;
    const dx=b.x-a.x, dy=b.y-a.y, len=Math.max(1,Math.hypot(dx,dy));
    const bend=Math.min(45,len*.12), nx=-dy/len, ny=dx/len;
    return `M ${a.x} ${a.y} Q ${mx+nx*bend} ${my+ny*bend} ${b.x} ${b.y}`;
  }
  function render(model){
    const linksHost=$("#directive-map-links"), nodesHost=$("#directive-map-nodes");
    linksHost.innerHTML=""; nodesHost.innerHTML="";
    const byId=Object.fromEntries(model.nodes.map(n=>[n.id,n]));
    model.links.forEach(l=>{
      const p=el("path",{id:`dm-${l.id}`,class:"dm-link",d:curve(byId[l.from],byId[l.to]),"data-from":l.from,"data-to":l.to});
      linksHost.appendChild(p);
    });
    model.nodes.forEach((n,i)=>{
      const g=el("g",{id:`dm-${n.id}`,class:`dm-node${i===0?" core":""}`,transform:`translate(${n.x} ${n.y})`});
      const ring=el("rect",{class:"dm-node-ring",x:-72,y:-28,width:144,height:56,rx:18,ry:18});
      const dot=el("circle",{class:"dm-node-dot",cx:0,cy:-28,r:i===0?4.5:3});
      const title=el("text",{class:"dm-node-title",x:0,y:-1}); title.textContent=n.title.slice(0,22);
      const sub=el("text",{class:"dm-node-sub",x:0,y:15}); sub.textContent=(n.sub||"").slice(0,28);
      g.append(ring,dot,title,sub); nodesHost.appendChild(g);
    });
    $("#dm-root")?.classList.add("built");
    $("#directive-map-count").textContent=`1 / ${model.nodes.length}`;
    const builder=$("#directive-map-builder"); builder.setAttribute("transform",`translate(${rootX(model)} ${rootY(model)})`); builder.classList.add("active");
  }
  const rootX=m=>m.nodes[0]?.x||700, rootY=m=>m.nodes[0]?.y||410;
  function setHud(status){ $("#directive-map-status").textContent=status; }
  function cancel(){ state.token++; cancelAnimationFrame(state.raf); state.raf=0; state.running=false; }
  function waitUntilResumed(token){ return new Promise(resolve=>{ const tick=()=>{ if(token!==state.token) return resolve(false); if(!state.paused) return resolve(true); state.raf=requestAnimationFrame(tick); }; tick(); }); }
  function animatePath(path, token, duration=760){
    return new Promise(resolve=>{
      const builder=$("#directive-map-builder"); const length=path.getTotalLength(); let started=null;
      function frame(ts){
        if(token!==state.token) return resolve(false);
        if(state.paused){ started=null; state.raf=requestAnimationFrame(frame); return; }
        if(started===null) started=ts;
        const p=Math.min(1,(ts-started)/duration); const pt=path.getPointAtLength(length*p);
        builder.setAttribute("transform",`translate(${pt.x} ${pt.y})`);
        if(p<1) state.raf=requestAnimationFrame(frame); else resolve(true);
      }
      state.raf=requestAnimationFrame(frame);
    });
  }
  async function runBuild(){
    cancel(); const token=state.token; state.running=true; state.step=0; state.paused=false; $("#directive-map-pause").textContent="PAUSE"; setHud("BUILDING");
    document.querySelectorAll(".dm-node").forEach((n,i)=>n.classList.toggle("built",i===0));
    document.querySelectorAll(".dm-link").forEach(n=>n.classList.remove("built"));
    let built=1;
    for(const link of state.links){
      if(token!==state.token) return;
      if(!(await waitUntilResumed(token))) return;
      const path=$(`#dm-${link.id}`); if(!path) continue;
      const ok=await animatePath(path,token); if(!ok) return;
      path.classList.add("built"); $(`#dm-${link.to}`)?.classList.add("built"); built++;
      $("#directive-map-count").textContent=`${built} / ${state.nodes.length}`;
      await new Promise(r=>setTimeout(r,110));
    }
    if(token===state.token){ state.running=false; setHud("COMPLETE"); }
  }
  async function loadAgent(slug, {selectGlobally=false}={}){
    if(!slug) return;
    cancel(); setHud("LOADING");
    if(selectGlobally){
      try { await api("/api/matrix/select",{method:"POST",body:JSON.stringify({agent:slug,enabled:true,lock:true})}); } catch(_) {}
    }
    const data=await api(`/api/matrix/agents/${encodeURIComponent(slug)}`); const agent=data.agent||{}; state.agent=agent.slug||slug;
    $("#directive-map-agent-name").textContent=labelOf(agent).toUpperCase();
    const model=layout(buildModel(agent)); state.nodes=model.nodes; state.links=model.links; state.route=model.route;
    const empty=$("#directive-map-empty"); empty.hidden=Boolean(agent.directive && model.nodes.length>1);
    render(model);
    if(agent.directive && model.nodes.length>1) runBuild(); else setHud("NO DIRECTIVE");
  }
  async function populate(){
    const data=await api("/api/matrix/agents?limit=1000"); state.agents=data.agents||[];
    const sel=$("#directive-map-agent"); const current=(window.CYPRA_STATE?.settings?.matrix_agent || document.querySelector("#matrix-agent-quick")?.value || "chloe").toLowerCase();
    sel.innerHTML="";
    state.agents.filter(a=>a.has_directive).forEach(a=>{ const o=document.createElement("option"); o.value=a.slug; o.textContent=(a.name||a.slug).toUpperCase(); sel.appendChild(o); });
    const pick=state.agents.some(a=>a.slug===current&&a.has_directive)?current:(sel.options[0]?.value||""); sel.value=pick;
    if(pick) await loadAgent(pick);
  }
  function bind(){
    $("#directive-map-agent")?.addEventListener("change",e=>loadAgent(e.target.value,{selectGlobally:true}).catch(err=>{setHud("ERROR");console.error(err);}));
    $("#directive-map-rebuild")?.addEventListener("click",()=>{ if(state.agent) loadAgent(state.agent).catch(console.error); });
    $("#directive-map-pause")?.addEventListener("click",()=>{ state.paused=!state.paused; $("#directive-map-pause").textContent=state.paused?"RESUME":"PAUSE"; setHud(state.paused?"PAUSED":state.running?"BUILDING":"COMPLETE"); });
    window.addEventListener("cypra:primary-view",e=>{ if(e.detail?.view==="directiveMap" && !state.agents.length) populate().catch(err=>{setHud("ERROR");console.error(err);}); });
    document.addEventListener("cypra:chat-state",()=>{ const active=document.querySelector("#matrix-agent-quick")?.value; if(active && active!==state.agent && $("#directive-map-agent")) $("#directive-map-agent").value=active; });
  }
  if(document.readyState==="loading") document.addEventListener("DOMContentLoaded",bind); else bind();
})();
