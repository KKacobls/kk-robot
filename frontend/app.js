/* ImageFlow Studio: dependency-free, offline visual editor for a Python local backend. */
'use strict';
const $ = (id) => document.getElementById(id);
const NODE_W = 236;
const COLORS = { start:'#61a5ff', condition:'#4bd6a5', click:'#ffbf6d', wait:'#bb95ff', hotkey:'#8db4ff', end:'#fb7f91' };
const ICONS = { start:'▶', condition:'◇', click:'⊙', wait:'◷', hotkey:'⌘', end:'■' };
const NAMES = { start:'開始', condition:'圖片判斷', click:'圖片點擊', wait:'等待', hotkey:'按鍵', end:'結束' };
const ALLOWED = new Set(Object.keys(NAMES));
const OUTLETS = { start:['next'], condition:['found','missing'], click:['found','missing'], wait:['next'], hotkey:['next'], end:[] };
let token = '';
let flow = { id:'starter', name:'工作流', nodes:[], edges:[], maxSteps:500 };
let selectedId = null, selectedIds = new Set(), activeConnection = null, state = null, dirty = false, autoSaveTimer = null;
let zoom = 0.8, pan = {x:100,y:80}, dragState = null, nodeCopy = null, pasteCount = 0, spaceHeld = false;
let toastTimer = null, logsOffset = 0, lastLogFingerprint = '';
let liveNodeId = null, liveOpen = false, liveFrameUrl = null, liveTimer = null;
let cropPoint1 = null, cropRect = null, cropImageData = null, cropTarget = 'node';
let monitors = [{id:'all',name:'全部螢幕'}], testImageData = '', testOpen = false;

function esc(value) { return String(value ?? '').replace(/[&<>"']/g, x=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x])); }
function cleanImage(value) { return typeof value==='string' && value.length<=4_100_000 && /^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/]+=*$/i.test(value) ? value : ''; }
function uid(prefix = 'node') { return `${prefix}_${Math.random().toString(36).slice(2,10)}`; }
function clamp(x,a,b) { return Math.max(a,Math.min(b,x)); }
function nodeById(id) { return flow.nodes.find(n=>n.id===id); }
function isImageNode(node) { return node && (node.type === 'condition' || node.type === 'click'); }
function toast(msg, bad=false) {
  const el=$('toast'); el.textContent=msg; el.style.borderColor=bad?'#9c5269':'#50627e';el.classList.add('show');
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.classList.remove('show'),3500);
}
async function api(url, method='GET', body) {
  const options={method, headers:{'X-ImageFlow-Token':token}};
  if(body !== undefined) { options.body=JSON.stringify(body);options.headers['Content-Type']='application/json'; }
  const res=await fetch(url, options);
  if(!res.ok) { const detail=await res.json().catch(()=>({detail:`HTTP ${res.status}`})); throw new Error(detail.detail || `HTTP ${res.status}`); }
  if(res.status===204) return null;
  return res.json();
}
function normalizeFlow(source) {
  if(!source || !Array.isArray(source.nodes) || !Array.isArray(source.edges)) throw Error('不是有效的 ImageFlow JSON');
  let ids=new Set();
  const nodes=source.nodes.slice(0,150).map(n=>{
    const type=ALLOWED.has(n.type)?n.type:'wait';
    let id = /^[a-zA-Z0-9_-]{1,70}$/.test(n.id) && !ids.has(n.id) ? n.id : uid();
    ids.add(id);
    return {
      ...n, id, type, title:String(n.title || NAMES[type]).slice(0,90),
      x:clamp(Number(n.x)||0,-10000,10000), y:clamp(Number(n.y)||0,-10000,10000),
      image:cleanImage(n.image),
      threshold:clamp(Number(n.threshold) || 0.85,0.5,1),
      clickX:clamp(Number(n.clickX ?? 0.5),0,1), clickY:clamp(Number(n.clickY ?? 0.5),0,1),
      monitor:normalizeMonitor(n.monitor),
    };
  });
  const used=new Set();
  const edges=source.edges.slice(0,350).filter(e=>{
    let key=`${e.source}:${e.sourceHandle}`;
    if(!ids.has(e.source)||!ids.has(e.target)||!OUTLETS[nodeByType(nodes,e.source)]?.includes(e.sourceHandle)||used.has(key))return false;
    used.add(key);return true;
  }).map(e=>({source:e.source,sourceHandle:e.sourceHandle,target:e.target}));
  return { id:/^[a-zA-Z0-9_-]{1,48}$/.test(source.id)?source.id:uid('flow'), name:String(source.name||'未命名工作流').slice(0,100), maxSteps:clamp(Number(source.maxSteps)||500,30,2000),nodes,edges };
}
function nodeByType(nodes,id) { return nodes.find(n=>n.id===id)?.type; }
function normalizeMonitor(value) {
  if(value == null || value==='all' || value===0 || value==='0')return 'all';
  return /^([1-9]|[12][0-9]|3[0-2])$/.test(String(value))?String(value):'all';
}
function monitorOptions(value='all') {
  return monitors.map(m=>`<option value="${esc(m.id)}" ${String(m.id)===String(value)?'selected':''}>${esc(m.name)}（${m.width||'?'}×${m.height||'?'}，${m.left??0}, ${m.top??0}）</option>`).join('');
}
async function refreshMonitors() {
  try {
    const info=await api('/api/monitors');
    monitors=[{...info[0],id:'all'},...info.slice(1)];
    if($('testMonitor'))$('testMonitor').innerHTML=monitorOptions($('testMonitor').value || 'all');
    renderInspector();
  }catch(e){toast('螢幕清單尚無法讀取：'+e.message,true);}
}
function markDirty() {
  dirty=true;$('dirtyBadge').hidden=false;$('boardTitle').textContent=flow.name;
  clearTimeout(autoSaveTimer);
  autoSaveTimer=setTimeout(()=>saveFlow(true),2500);
}
async function saveFlow(silent=false) {
  if(!flow.id)return;
  clearTimeout(autoSaveTimer);
  const snapshot=JSON.stringify(flow);
  try {
    await api('/api/workflows','POST',flow);
    if(JSON.stringify(flow)===snapshot) {dirty=false;$('dirtyBadge').hidden=true;}
    if(!silent)toast('工作流已儲存（包含全部貼上的圖片）');
    await refreshFlows();
  }catch(err){toast(`儲存失敗：${err.message}`,true);}
}
async function refreshFlows() {
  const items=await api('/api/workflows');
  const select=$('flowSelect');select.innerHTML='';
  for(const item of items) {let option=document.createElement('option');option.value=item.id;option.textContent=item.name;select.appendChild(option);}
  select.value=flow.id;
}
async function loadFlow(id) {
  if(dirty) await saveFlow(true);
  const data=await api(`/api/workflows/${encodeURIComponent(id)}`);
  flow=normalizeFlow(data);selectedId=null;selectedIds.clear();activeConnection=null;dirty=false;
  $('dirtyBadge').hidden=true;$('boardTitle').textContent=flow.name;$('flowSelect').value=flow.id;
  render();requestAnimationFrame(fitView);
}
function createNode(type,x,y) {
  const node={id:uid(),type,title:NAMES[type],x:Math.round(x),y:Math.round(y)};
  if(isImageNode(node)) Object.assign(node,{image:'',threshold:.85,timeout:type==='condition'?0:2,clickX:.5,clickY:.5,clickOnFound:false,button:'left',doubleClick:false,monitor:'all'});
  if(type==='wait')node.seconds=1;
  if(type==='hotkey')node.keys='enter';
  if(type==='start' && flow.nodes.some(n=>n.type==='start')){toast('每個工作流只能有一個「開始」節點',true);return;}
  flow.nodes.push(node);selectNode(node.id);render();markDirty();
}
function selectionSnapshot() {
  const selected = flow.nodes.filter(n=>selectedIds.has(n.id) && n.type!=='start');
  const ids = new Set(selected.map(n=>n.id));
  return {type:'imageflow-nodes',version:1,nodes:structuredClone(selected),
    edges:structuredClone(flow.edges.filter(e=>ids.has(e.source)&&ids.has(e.target)))};
}
function copyNodes(event=null) {
  const data=selectionSnapshot();
  if(!data.nodes.length){toast('請先框選節點（開始節點不能複製）',true);return false;}
  nodeCopy=data;pasteCount=0;
  const serialized=JSON.stringify(data);
  if(event?.clipboardData){event.clipboardData.setData('text/plain',serialized);event.preventDefault();}
  else navigator.clipboard?.writeText(serialized).catch(()=>{});
  toast(`已複製 ${data.nodes.length} 個節點及 ${data.edges.length} 條內部連線`);return true;
}
function pasteNodes(data=nodeCopy) {
  if(!data || data.type!=='imageflow-nodes' || !Array.isArray(data.nodes))return false;
  // Never paste a second start node, and bound the editor to backend limits.
  const originals=data.nodes.filter(n=>ALLOWED.has(n.type)&&n.type!=='start');
  if(!originals.length){toast('沒有可貼上的節點',true);return false;}
  if(flow.nodes.length+originals.length>150){toast('貼上後會超過 150 個節點',true);return false;}
  pasteCount+=1;const shift=45*pasteCount;
  const idMap = new Map();
  const copies=originals.map(n=>{
    const id=uid();idMap.set(n.id,id);
    const x=Number(n.x), y=Number(n.y);
    return {...structuredClone(n),id, title:String(n.title||NAMES[n.type]).slice(0,90),
      x:Math.round(clamp(Number.isFinite(x)?x+shift:shift,-10000,10000)),
      y:Math.round(clamp(Number.isFinite(y)?y+shift:shift,-10000,10000)),
      image:cleanImage(n.image), monitor:normalizeMonitor(n.monitor)};
  });
  const edges=(Array.isArray(data.edges)?data.edges:[]).filter(e=>idMap.has(e.source)&&idMap.has(e.target)&&OUTLETS[nodeByType(originals,e.source)]?.includes(e.sourceHandle)).map(e=>({source:idMap.get(e.source),sourceHandle:e.sourceHandle,target:idMap.get(e.target)}));
  flow.nodes.push(...copies);flow.edges.push(...edges.slice(0,Math.max(0,350-flow.edges.length)));
  selectedIds=new Set(copies.map(n=>n.id));selectedId=copies.at(-1).id;
  render();markDirty();toast(`已貼上 ${copies.length} 個節點，保留組內箭頭`);return true;
}
function duplicateNode() {
  if(!selectedIds.size){toast('先選取要複製的節點',true);return;}
  const data=selectionSnapshot();nodeCopy=data;pasteCount=0;pasteNodes(data);
}
function deleteNode() {
  if(!selectedIds.size)return;
  const doomed = new Set([...selectedIds].filter(id=>nodeById(id)?.type!=='start'));
  if(!doomed.size){toast('開始節點不能刪除',true);return;}
  flow.nodes=flow.nodes.filter(n=>!doomed.has(n.id));
  flow.edges=flow.edges.filter(e=>!doomed.has(e.source)&&!doomed.has(e.target));
  selectedIds.clear();selectedId=null;render();markDirty();
}
function selectNode(id,additive=false) {
  if(additive){
    if(selectedIds.has(id))selectedIds.delete(id);else selectedIds.add(id);
    selectedId=selectedIds.has(id)?id:[...selectedIds].at(-1)||null;
  }else{selectedId=id;selectedIds=new Set(id?[id]:[]);}
  activeConnection=null;$('linkHint').textContent=selectedIds.size>1?`已選取 ${selectedIds.size} 個節點，Ctrl+C 複製整組`:'空白處拖曳框選 · 空白鍵+拖曳平移';
  setNodeClasses();renderInspector();
}
function addEdge(source,sourceHandle,target) {
  if(!nodeById(source)||!nodeById(target))return;
  flow.edges=flow.edges.filter(e=>!(e.source===source&&e.sourceHandle===sourceHandle));
  flow.edges.push({source,sourceHandle,target});
  activeConnection=null;renderEdges();setNodeClasses();markDirty();
  $('linkHint').textContent='連線已建立';
}
function render() {renderNodes();renderEdges();renderInspector();}
function renderNodes() {
  $('nodeLayer').innerHTML=flow.nodes.map(n=>{
    const color=COLORS[n.type];
    const ports=OUTLETS[n.type].map(key=>`<div class="port-row"><span>${key==='found'?'✓ 找到':key==='missing'?'✕ 沒找到':'繼續'}</span><i class="port-out ${key}" data-out="${key}" title="點此連到下一節點"></i></div>`).join('');
    const thumb=isImageNode(n)?`<div class="n-thumb">${n.image?`<img alt="圖片範本" src="${n.image}">`:'尚未貼上圖片 · Ctrl+V'}</div>`:'';
    let detail='';
    if(n.type==='condition')detail=n.clickOnFound?'找到後直接點圖片':'只檢查圖片是否存在';
    if(n.type==='click')detail='找到圖片後點擊指定位置';
    if(n.type==='wait')detail=`等待 ${Number(n.seconds??1)} 秒`;
    if(n.type==='hotkey')detail=`送出 ${n.keys||'enter'}`;
    if(n.type==='start')detail='從這裡開始執行';
    if(n.type==='end')detail='完成並結束';
    return `<div class="workflow-node" data-id="${n.id}" style="left:${n.x}px;top:${n.y}px;--tone:${color}">
      <div class="port-in" title="輸入連接點"></div>
      <div class="node-head"><i class="n-symbol">${ICONS[n.type]}</i><span class="n-heading" title="${esc(n.title)}">${esc(n.title)}</span><span class="node-id">⋮⋮</span></div>
      <div class="node-content"><div class="node-description">${esc(detail)}</div>${thumb}</div>
      <div class="node-ports">${ports}</div>
    </div>`;
  }).join('');
  setNodeClasses();
}
function portY(node,port) {return isImageNode(node)?(port==='missing'?199:170):109;}
function drawCurve(a,b) {
  const dx=b.x-a.x;
  const k=dx>=0?Math.max(66,Math.min(200,dx*.55)):100;
  return `M ${a.x} ${a.y} C ${a.x+k} ${a.y}, ${b.x-k} ${b.y}, ${b.x} ${b.y}`;
}
function renderEdges() {
  const last=state?.lastEdge;
  let svg=`<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 Z" fill="#8191a9"></path></marker></defs>`;
  for(const edge of flow.edges){
    const a=nodeById(edge.source),b=nodeById(edge.target);if(!a||!b)continue;
    const p1={x:a.x+NODE_W+5,y:a.y+portY(a,edge.sourceHandle)};
    const p2={x:b.x-4,y:b.y+34};
    const active=last?.source===edge.source && last?.sourceHandle===edge.sourceHandle && last?.target===edge.target;
    svg+=`<path class="edge-path ${edge.sourceHandle} ${active?'active':''}" d="${drawCurve(p1,p2)}" marker-end="url(#arrow)"></path>`;
    if(edge.sourceHandle!=='next')svg+=`<text class="edge-label" x="${p1.x+22}" y="${p1.y-9}">${edge.sourceHandle==='found'?'找到':'沒找到'}</text>`;
  }
  $('edgeLayer').innerHTML=svg;
}
function setNodeClasses() {
  for(const el of $('nodeLayer').querySelectorAll('.workflow-node')){
    const id=el.dataset.id;
    el.classList.toggle('selected',selectedIds.has(id));
    el.classList.toggle('executing',state?.status==='running'&&state?.current===id);
    el.classList.toggle('visited',Boolean(state?.history?.includes(id)));
    el.querySelector('.port-in')?.classList.toggle('connectable',Boolean(activeConnection));
  }
}
function updateTransform() {
  $('world').style.transform=`translate(${pan.x}px,${pan.y}px) scale(${zoom})`;
  $('boardGrid').style.backgroundSize=`${22*zoom}px ${22*zoom}px`;
  $('boardGrid').style.backgroundPosition=`${pan.x}px ${pan.y}px`;
  $('zoomLevel').textContent=`${Math.round(zoom*100)}%`;
}
function worldAt(clientX,clientY) {let r=$('board').getBoundingClientRect();return {x:(clientX-r.left-pan.x)/zoom,y:(clientY-r.top-pan.y)/zoom};}
function zoomTo(next,clientX,clientY) {
  const before=worldAt(clientX,clientY),r=$('board').getBoundingClientRect();
  zoom=clamp(next,.3,2);
  pan={x:clientX-r.left-before.x*zoom,y:clientY-r.top-before.y*zoom};updateTransform();
}
function fitView() {
  if(!flow.nodes.length)return;
  const xs=flow.nodes.map(n=>n.x),ys=flow.nodes.map(n=>n.y);
  const minX=Math.min(...xs),minY=Math.min(...ys),maxX=Math.max(...xs)+NODE_W,maxY=Math.max(...ys)+235;
  const r=$('board').getBoundingClientRect(),padding=65;
  zoom=clamp(Math.min((r.width-2*padding)/Math.max(100,maxX-minX),(r.height-2*padding)/Math.max(100,maxY-minY)),.33,1.0);
  pan.x=(r.width-(maxX-minX)*zoom)/2-minX*zoom;
  pan.y=(r.height-(maxY-minY)*zoom)/2-minY*zoom;updateTransform();
}
function showMarquee(a,b) {
  const board=$('board').getBoundingClientRect(),x=Math.min(a.x,b.x)-board.left,y=Math.min(a.y,b.y)-board.top;
  const rect=$('marquee');rect.hidden=false;
  Object.assign(rect.style,{left:`${x}px`,top:`${y}px`,width:`${Math.abs(a.x-b.x)}px`,height:`${Math.abs(a.y-b.y)}px`});
  const x1=Math.min(a.x,b.x),x2=Math.max(a.x,b.x),y1=Math.min(a.y,b.y),y2=Math.max(a.y,b.y);
  const selected=flow.nodes.filter(n=>{
    const el=$('nodeLayer').querySelector(`[data-id="${n.id}"]`);
    if(!el)return false;const r=el.getBoundingClientRect();
    return r.left<=x2 && r.right>=x1 && r.top<=y2 && r.bottom>=y1;
  }).map(n=>n.id);
  const base=dragState?.baseSelection||new Set();
  selectedIds=new Set([...base,...selected]);selectedId=selected.at(-1)||[...base].at(-1)||null;
  setNodeClasses();
}
function startBoardInteraction(e) {
  if(e.target.closest('.board-controls,.board-overlay'))return;
  if(e.button!==0&&e.button!==1)return;
  const nodeElement=e.target.closest('.workflow-node');
  if(nodeElement&&e.button===0&&!spaceHeld){
    const id=nodeElement.dataset.id;
    if(e.target.closest('.port-out')){
      const outlet=e.target.closest('.port-out').dataset.out;
      selectNode(id);activeConnection={source:id,port:outlet};setNodeClasses();
      $('linkHint').textContent=`點擊目標節點左側的圓點：${outlet==='found'?'找到':outlet==='missing'?'沒找到':'繼續'}`;
      return;
    }
    if(e.target.closest('.port-in')){
      if(activeConnection)addEdge(activeConnection.source,activeConnection.port,id);
      else selectNode(id);
      return;
    }
    if(e.shiftKey||e.ctrlKey||e.metaKey){selectNode(id,true);e.preventDefault();return;}
    if(!selectedIds.has(id))selectNode(id);
    else {selectedId=id;renderInspector();}
    if(e.target.closest('.node-head')){
      const positions=flow.nodes.filter(n=>selectedIds.has(n.id)).map(n=>({id:n.id,x:n.x,y:n.y}));
      dragState={type:'nodes',positions,startX:e.clientX,startY:e.clientY,moved:false};
      e.preventDefault();
    }
    return;
  }
  if(activeConnection){activeConnection=null;setNodeClasses();}
  if(e.button===1||spaceHeld){
    dragState={type:'pan',startX:e.clientX,startY:e.clientY,x:pan.x,y:pan.y};
  }else if(e.button===0){
    if(!e.shiftKey&&!e.ctrlKey&&!e.metaKey){selectedIds.clear();selectedId=null;}
    dragState={type:'marquee',startX:e.clientX,startY:e.clientY,baseSelection:new Set(selectedIds)};
    showMarquee({x:e.clientX,y:e.clientY},{x:e.clientX,y:e.clientY});renderInspector();
  }
  e.preventDefault();
}
function pointerMove(e) {
  if(!dragState)return;
  const dx=e.clientX-dragState.startX,dy=e.clientY-dragState.startY;
  if(dragState.type==='marquee')showMarquee({x:dragState.startX,y:dragState.startY},{x:e.clientX,y:e.clientY});
  if(dragState.type==='pan'){pan.x=dragState.x+dx;pan.y=dragState.y+dy;updateTransform();}
  if(dragState.type==='nodes'){
    dragState.moved ||= Math.abs(dx)+Math.abs(dy)>2;
    for(const original of dragState.positions){
      const n=nodeById(original.id);if(!n)continue;
      n.x=Math.round(original.x+dx/zoom);n.y=Math.round(original.y+dy/zoom);
      const el=$('nodeLayer').querySelector(`[data-id="${n.id}"]`);
      if(el){el.style.left=`${n.x}px`;el.style.top=`${n.y}px`;}
    }
    renderEdges();
  }
}
function pointerUp() {
  if(dragState?.type==='nodes'&&dragState.moved)markDirty();
  if(dragState?.type==='marquee'){
    $('marquee').hidden=true;renderInspector();
    $('linkHint').textContent=selectedIds.size?`已框選 ${selectedIds.size} 個節點 · Ctrl+C 複製`:'框選節點 · 空白鍵+拖曳平移';
  }
  dragState=null;
}

function field(title,body,help='') {return `<div class="field"><label>${title}</label>${body}${help?`<span class="help">${help}</span>`:''}</div>`;}
function renderInspector() {
  const n=nodeById(selectedId),root=$('inspectorBody');
  $('inspectorHint').textContent=selectedIds.size>1?`已選取 ${selectedIds.size} 個節點`:n?`${NAMES[n.type]} · ${n.id.slice(-6)}`:'選取節點開始編輯';
  if(selectedIds.size>1){root.innerHTML=`<div class="multi-hint"><strong>已框選 ${selectedIds.size} 個節點</strong><p>可直接拖拉整組節點，或 Ctrl+C → Ctrl+V 複製（保留組內箭頭及圖片）。</p><div class="inspector-actions"><button id="duplicateNode">⧉ 複製這組</button><button id="removeNode" class="danger">✕ 刪除這組</button></div></div>`;return;}
  if(!n){root.innerHTML='<div class="empty-state"><div class="empty-logo">◎</div><strong>選取一個節點</strong><p>選好節點後，直接按 Ctrl+V 貼圖片，就能建立圖片辨識條件。</p></div>';return;}
  let content=field('節點名稱',`<input data-field="title" value="${esc(n.title)}" maxlength="90">`);
  if(isImageNode(n)){
    content+='<div class="inspector-section">圖片辨識範本 <span>PNG / JPG / WebP</span></div>';
    if(n.image){
      content+=`<div class="field"><div class="image-point-picker" id="pointPicker" title="點擊圖片設定點擊位置"><img alt="按此設定相對點擊位置" src="${n.image}"><i class="point-marker" id="pointMarker" style="left:${n.clickX*100}%;top:${n.clickY*100}%"></i></div>
        <span class="help">黃色十字是點擊位置。直接點圖片可移動它（X: <b>${Math.round(n.clickX*100)}%</b>，Y: <b>${Math.round(n.clickY*100)}%</b>）。</span></div>`;
    }else{
      content+=`<div class="image-drop" id="imageDrop"><span class="drop-icon">▧</span><strong>直接 Ctrl+V 貼上圖片</strong><small>先用 Win+Shift+S 截圖，再回這裡貼上<br>也可按下方「讀取圖片」</small></div>`;
    }
    content+=`<div class="image-actions"><button id="uploadImage">▧ 讀取圖片</button><button id="captureImage">▣ 擷取桌面</button>${n.image?'<button id="copyImage">▤ 複製圖片</button><button id="removeImage">✕ 清除</button>':''}</div>`;
    content+='<div class="inspector-section">比對設定</div>';
    content+=field('搜尋螢幕',`<select data-field="monitor">${monitorOptions(n.monitor||'all')}</select>`,'預設搜尋全部實體螢幕；也可以固定辨識某一個螢幕。');
    content+=field('相似度門檻',`<div style="display:flex;align-items:center;gap:9px"><input type="range" data-field="threshold" min="0.5" max="1" step="0.01" value="${n.threshold}"><strong id="thresholdLabel">${Math.round(n.threshold*100)}%</strong></div>`,'建議從 85% 開始。太低容易誤判，太高容易找不到。');
    content+=field('等待圖片出現（秒）',`<input type="number" data-field="timeout" min="0" max="120" step="0.5" value="${Number(n.timeout??0)}">`,'0 秒＝檢查一次；大於 0＝不斷偵測直到找到或逾時。');
    if(n.type==='condition'){
      content+=`<div class="field"><label class="field-check"><input type="checkbox" data-field="clickOnFound" ${n.clickOnFound?'checked':''}><span>找到圖片後，自動點擊黃色十字的位置</span></label><span class="help">打勾就可以做到「找到 A → 點擊 A 的指定位置」，不需要額外建立點擊節點。</span></div>`;
    }
    if(n.type==='click' || n.clickOnFound){
      content+=`<div class="field-grid">${field('滑鼠按鍵',`<select data-field="button"><option value="left" ${n.button!=='right'?'selected':''}>左鍵</option><option value="right" ${n.button==='right'?'selected':''}>右鍵</option></select>`)}${field('點擊次數',`<select data-field="doubleClick"><option value="false" ${!n.doubleClick?'selected':''}>單擊</option><option value="true" ${n.doubleClick?'selected':''}>雙擊</option></select>`)}</div>`;
    }
    content+=`<div class="image-actions"><button id="testImage">◎ 立即測試辨識</button><button id="previewImage">◉ 開啟即時畫面</button></div><div id="testResult"></div>`;
  }
  if(n.type==='wait')content+=field('等待時間（秒）',`<input type="number" data-field="seconds" min="0" max="120" step="0.1" value="${Number(n.seconds??1)}">`);
  if(n.type==='hotkey')content+=field('要按的按鍵',`<input data-field="keys" value="${esc(n.keys||'enter')}" placeholder="ctrl+s">`,'例如 enter、alt+tab、ctrl+s、esc、f5。');
  if(n.type==='start')content+='<p style="color:#8fa1bb;font-size:12px;line-height:1.8">這個節點代表流程開始。由右側藍點連到下一個節點。</p>';
  if(n.type==='end')content+='<p style="color:#8fa1bb;font-size:12px;line-height:1.8">執行到這裡時，流程就會結束。</p>';
  const connections=flow.edges.filter(e=>e.source===n.id);
  if(connections.length){
    content+='<div class="inspector-section">目前連線</div>';
    for(const edge of connections){let target=nodeById(edge.target);
      content+=`<div class="field-check" style="margin-bottom:8px;justify-content:space-between"><span>${edge.sourceHandle==='found'?'✓ 找到':edge.sourceHandle==='missing'?'✕ 沒找到':'→ 繼續'}：${esc(target?.title||edge.target)}</span><button class="unlink" data-port="${edge.sourceHandle}" style="background:transparent;border:0;color:#ff9fae" title="移除此連線">✕</button></div>`;
    }
  }
  content+=`<div class="inspector-actions"><button id="duplicateNode">⧉ 複製節點</button><button id="removeNode" class="danger">✕ 刪除節點</button></div>`;
  root.innerHTML=content;
}
async function blobAsPng(blob) {
  if(blob.size > 10*1024*1024)throw Error('原始圖片太大，請截取小範圍');
  const bitmap=await createImageBitmap(blob);
  if(bitmap.width*bitmap.height>5_000_000){bitmap.close();throw Error('圖片解析度太大，請縮小範圍');}
  const canvas=document.createElement('canvas');canvas.width=bitmap.width;canvas.height=bitmap.height;
  canvas.getContext('2d',{alpha:false}).drawImage(bitmap,0,0);bitmap.close();
  const data=canvas.toDataURL('image/png');if(data.length>4_000_000)throw Error('PNG 超過 3MB，請縮小截圖範圍');
  return data;
}
function assignImage(data,id=selectedId) {
  const n=nodeById(id);if(!isImageNode(n))throw Error('請先選取「圖片判斷」或「圖片點擊」節點');
  n.image=cleanImage(data);n.clickX=.5;n.clickY=.5;
  render();selectNode(n.id);markDirty();toast(`已將圖片貼到「${n.title}」`);
}
async function uploadImage(file) {
  if(!file)return;
  const id=selectedId;
  try{assignImage(await blobAsPng(file),id);}catch(err){toast(err.message,true);}
}
function setNodeProperty(name,value) {
  const n=nodeById(selectedId);if(!n)return;
  const numeric=new Set(['threshold','timeout','seconds']);
  if(numeric.has(name))value=Number(value);
  if(name==='threshold')value=clamp(value,.5,1);
  if(name==='timeout'||name==='seconds')value=clamp(value,0,120);
  if(name==='doubleClick'||name==='clickOnFound')value=(value===true||value==='true');
  if(name==='monitor')value=normalizeMonitor(value);
  n[name]=value;
  if(name==='threshold')$('thresholdLabel').textContent=`${Math.round(value*100)}%`;
  if(name==='clickOnFound'){renderInspector();}
  renderNodes();markDirty();
}
async function testImage() {
  const n=nodeById(selectedId);if(!n?.image){toast('請先貼上圖片',true);return;}
  const resultBox=$('testResult');if(resultBox)resultBox.innerHTML='<div class="test-result">偵測中…</div>';
  try{
    const r=await api('/api/test','POST',{image:n.image,threshold:n.threshold,monitor:n.monitor||'all'});
    if(resultBox && resultBox.isConnected){resultBox.innerHTML=`<div class="test-result ${r.found?'ok':'fail'}">${r.found?'✓ 找到圖片':'✕ 未達到門檻'}<br>相似度 ${Math.round(r.score*1000)/10}% ／ 設定門檻 ${Math.round(n.threshold*100)}%<br>最高分位置 (${r.rect.x}, ${r.rect.y})，尺寸 ${r.rect.w}×${r.rect.h}<br>螢幕 ${r.monitor}</div>`;}
  }catch(err){if(resultBox&&resultBox.isConnected)resultBox.textContent=err.message;toast(err.message,true);}
}
async function copyImage() {
  const n=nodeById(selectedId);if(!n?.image)return;
  try{const blob=await(await fetch(n.image)).blob();await navigator.clipboard.write([new ClipboardItem({'image/png':blob})]);toast('圖片已複製，可貼到其他節點');}
  catch(err){toast('瀏覽器不允許直接複製圖片，請改用 Ctrl+V 貼原始圖片',true);}
}
async function openLive(id=selectedId) {
  liveNodeId=id;liveOpen=true;$('liveModal').hidden=false;
  $('liveSub').textContent='綠色＝找到／橘色＝低於門檻／黃色十字＝預計點擊位置';
  await updateLive();
}
function closeLive(){liveOpen=false;clearTimeout(liveTimer);$('liveModal').hidden=true;document.querySelector('.live-card')?.classList.remove('expanded');if(liveFrameUrl){URL.revokeObjectURL(liveFrameUrl);liveFrameUrl=null;}}
async function updateLive() {
  if(!liveOpen)return;
  const n=nodeById(liveNodeId);const testMode=liveNodeId==='__test__';
  const request={image:testMode?testImageData:isImageNode(n)?n.image:'',threshold:testMode?Number($('testThreshold').value):n?.threshold??.85,clickX:n?.clickX??.5,clickY:n?.clickY??.5,monitor:testMode?$('testMonitor').value:(n?.monitor||'all')};
  try{
    const res=await fetch('/api/preview',{method:'POST',headers:{'Content-Type':'application/json','X-ImageFlow-Token':token},body:JSON.stringify(request)});
    if(!res.ok){const msg=await res.json().catch(()=>({detail:'畫面擷取失敗'}));throw Error(msg.detail);}
    const blob=await res.blob();if(!liveOpen)return;
    const newURL=URL.createObjectURL(blob),oldURL=liveFrameUrl;liveFrameUrl=newURL;
    $('liveImage').src=newURL;$('liveLoading').style.display='none';
    if(oldURL)URL.revokeObjectURL(oldURL);
    const score=res.headers.get('X-Match-Score');const found=res.headers.get('X-Match-Found')==='true';
    $('liveScore').textContent=score ? `${testMode?'圖片測試區':n?.title||'畫面'}｜螢幕 ${res.headers.get('X-Match-Monitor')||'?'}｜${found?'✓ 找到':'✕ 未找到'}｜最高相似度 ${(Number(score)*100).toFixed(1)}%` : '目前沒有指定圖片，正在顯示所選螢幕畫面';
  }catch(err){$('liveLoading').textContent=err.message;$('liveLoading').style.display='block';$('liveScore').textContent='請確認目前是在有螢幕的 Windows 桌面執行';}
  if(liveOpen)liveTimer=setTimeout(updateLive,600);
}
async function openCrop(target='node') {
  const n=nodeById(selectedId);if(target!=='test'&&!isImageNode(n)){toast('請先選取圖片節點',true);return;}
  cropTarget=target;const monitor=target==='test'?$('testMonitor').value:(n.monitor||'all');
  try{
    toast('正在擷取螢幕，請在畫面中框選要辨識的範圍');
    const r=await api('/api/capture','POST',{monitor});cropImageData=r.image;
    $('cropImage').src=r.image;$('cropModal').hidden=false;
    $('cropSelection').style.display='none';$('cropConfirm').disabled=true;cropRect=null;
  }catch(err){toast(err.message,true);}
}
function stagePosition(e) {
  const r=$('cropImage').getBoundingClientRect();
  return {x:clamp(e.clientX-r.left,0,r.width),y:clamp(e.clientY-r.top,0,r.height)};
}
function drawCrop(a,b) {
  let x=Math.min(a.x,b.x),y=Math.min(a.y,b.y),w=Math.abs(a.x-b.x),h=Math.abs(a.y-b.y);
  const el=$('cropSelection');Object.assign(el.style,{display:'block',left:`${x}px`,top:`${y}px`,width:`${w}px`,height:`${h}px`});
  cropRect={x,y,w,h};$('cropHint').textContent=`選取 ${Math.round(w)}×${Math.round(h)} 顯示像素`;
  $('cropConfirm').disabled=w<8||h<8;
}
async function confirmCrop() {
  if(!cropRect||!cropImageData)return;
  const img=$('cropImage'),r=img.getBoundingClientRect();const scaleX=img.naturalWidth/r.width,scaleY=img.naturalHeight/r.height;
  const canvas=document.createElement('canvas');canvas.width=Math.round(cropRect.w*scaleX);canvas.height=Math.round(cropRect.h*scaleY);
  if(canvas.width*canvas.height>5_000_000){toast('框選範圍太大，請縮小',true);return;}
  canvas.getContext('2d').drawImage(img,cropRect.x*scaleX,cropRect.y*scaleY,canvas.width,canvas.height,0,0,canvas.width,canvas.height);
  const data=canvas.toDataURL('image/png');if(data.length>4_000_000){toast('圖片超過 3MB，請截取更小範圍',true);return;}
  $('cropModal').hidden=true;if(cropTarget==='test')setTestImage(data);else assignImage(data);
}
function openTestCenter() {
  testOpen=true;$('testModal').hidden=false;
  if(!testImageData){const selected=nodeById(selectedId);if(isImageNode(selected)&&selected.image)setTestImage(selected.image);}
  $('testMonitor').innerHTML=monitorOptions($('testMonitor').value||'all');
}
function closeTestCenter(){testOpen=false;$('testModal').hidden=true;}
function setTestImage(data) {
  testImageData=cleanImage(data);$('testThumb').hidden=!testImageData;
  $('testPlaceholder').hidden=Boolean(testImageData);
  if(testImageData)$('testThumb').src=testImageData;
  $('testCenterResult').className='test-center-result';
  $('testCenterResult').textContent='圖片已載入，按「判斷螢幕是否存在」，不會執行滑鼠點擊。';
}
async function testCenterNow() {
  if(!testImageData){toast('請先貼圖到圖片測試區',true);return;}
  const target=$('testCenterResult');target.className='test-center-result';target.textContent='比對中…';
  const monitor=$('testMonitor').value,threshold=Number($('testThreshold').value);
  try{
    const r=await api('/api/test','POST',{image:testImageData,threshold,monitor});
    target.className=`test-center-result ${r.found?'ok':'fail'}`;
    const scores=(r.candidates||[]).map(v=>`螢幕 ${esc(v.monitor)}：${(v.score*100).toFixed(1)}%`).join('　');
    target.innerHTML=`<strong>${r.found?'✓ 找到圖片':'✕ 沒有達到門檻'}</strong>　<br>最高相似度：${(r.score*100).toFixed(2)}%（門檻 ${(threshold*100).toFixed(0)}%）<br>偵測螢幕：${esc(r.monitor)}　絕對座標：(${r.rect.x}, ${r.rect.y})　尺寸：${r.rect.w}×${r.rect.h}<div class="test-scores">${scores}</div>`;
  }catch(e){target.className='test-center-result fail';target.textContent='偵測失敗：'+e.message;}
}
function downloadFile(name,text) {
  const blob=new Blob([text],{type:'application/json;charset=utf-8'});
  const a=document.createElement('a'),url=URL.createObjectURL(blob);a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
async function run() {
  if(state?.status==='running'){toast('已有流程正在執行',true);return;}
  const dryRun=$('dryRun').checked;
  if(!dryRun && !confirm('即將實際控制這台電腦的滑鼠與鍵盤。請確認流程已通過模擬測試。按 F8 或把滑鼠移到螢幕左上角可停止。\n\n確定要執行嗎？'))return;
  try{await saveFlow(true);await api('/api/run','POST',{flow,dryRun});toast(dryRun?'已開始模擬（不會真的點擊）':'已開始實際點擊，F8 可停止');await pollState();}
  catch(err){toast(`無法執行：${err.message}`,true);}
}
async function pollState() {
  try{
    state=await api('/api/state');
    const st=$('runStatus');st.classList.toggle('running',state.status==='running');st.classList.toggle('error',state.status==='error');
    const map={idle:'就緒',running:state.dryRun?'模擬執行中':'正在實際操作',finished:'已完成',stopped:'已停止',error:'執行錯誤'};
    st.querySelector('span').textContent=map[state.status]||state.status;
    $('runButton').disabled=state.status==='running';$('stopButton').disabled=state.status!=='running';
    $('dryRun').disabled=state.status==='running';
    setNodeClasses();renderEdges();
    const logs=state.logs||[],fingerprint=`${state.startedAt||''}|${logs.length}|${logs.at(-1)?.message||''}|${logsOffset}`;
    if(fingerprint!==lastLogFingerprint){
      const el=$('logs');el.innerHTML=logs.length>logsOffset?logs.slice(logsOffset).map(entry=>`<div class="log-entry"><time>${esc(entry.time)}</time><span>${esc(entry.message)}</span></div>`).join(''):'<div class="log-placeholder">沒有紀錄</div>';
      el.scrollTop=el.scrollHeight;lastLogFingerprint=fingerprint;
    }
  }catch(err){$('runStatus').querySelector('span').textContent='伺服器無回應';}
}
function bindEvents() {
  $('board').addEventListener('pointerdown',startBoardInteraction);
  window.addEventListener('pointermove',pointerMove);window.addEventListener('pointerup',pointerUp);
  $('board').addEventListener('wheel',e=>{e.preventDefault();zoomTo(zoom*(e.deltaY>0?0.91:1.10),e.clientX,e.clientY);},{passive:false});
  document.querySelectorAll('[data-add]').forEach(button=>{
    button.addEventListener('click',()=>{const r=$('board').getBoundingClientRect(),p=worldAt(r.left+r.width*.42,r.top+r.height*.40);createNode(button.dataset.add,p.x,p.y);});
    button.addEventListener('dragstart',e=>e.dataTransfer.setData('text/x-imageflow-node',button.dataset.add));
  });
  $('board').addEventListener('dragover',e=>{if(e.dataTransfer.types.includes('text/x-imageflow-node'))e.preventDefault();});
  $('board').addEventListener('drop',e=>{let type=e.dataTransfer.getData('text/x-imageflow-node');if(ALLOWED.has(type)){e.preventDefault();let p=worldAt(e.clientX,e.clientY);createNode(type,p.x,p.y);}});
  $('zoomIn').onclick=()=>{let r=$('board').getBoundingClientRect();zoomTo(zoom*1.2,r.left+r.width/2,r.top+r.height/2);};
  $('zoomOut').onclick=()=>{let r=$('board').getBoundingClientRect();zoomTo(zoom/1.2,r.left+r.width/2,r.top+r.height/2);};
  $('fitView').onclick=fitView;
  $('saveFlow').onclick=()=>saveFlow(false);
  $('newFlow').onclick=async()=>{
    const name=prompt('新工作流名稱：','我的圖片工作流');if(!name?.trim())return;
    if(dirty)await saveFlow(true);
    const start={id:uid(),type:'start',title:'開始',x:120,y:160},end={id:uid(),type:'end',title:'結束',x:530,y:160};
    flow={id:uid('flow'),name:name.trim().slice(0,100),maxSteps:500,nodes:[start,end],edges:[{source:start.id,sourceHandle:'next',target:end.id}]};
    selectedId=null;selectedIds.clear();dirty=true;render();await saveFlow(true);await refreshFlows();fitView();toast('新工作流已建立。從工具箱拖曳節點進來吧！');
  };
  $('flowSelect').onchange=async e=>{try{await loadFlow(e.target.value);}catch(err){toast(err.message,true);}};
  $('exportFlow').onclick=()=>downloadFile(`${flow.id}.json`,JSON.stringify(flow,null,2));
  $('importFlow').onclick=()=>$('jsonInput').click();
  $('jsonInput').onchange=async e=>{
    const file=e.target.files[0];if(!file)return;
    try{
      const source=JSON.parse(await file.text());const imported=normalizeFlow(source);
      imported.id=uid('imported');imported.name=imported.name+'（匯入）';
      flow=imported;selectedId=null;selectedIds.clear();render();markDirty();await saveFlow(true);await refreshFlows();fitView();toast('已匯入工作流和所有內嵌圖片');
    }catch(err){toast(`匯入失敗：${err.message}`,true);} e.target.value='';
  };
  $('imageInput').onchange=e=>{uploadImage(e.target.files[0]);e.target.value='';};
  $('inspectorBody').addEventListener('input',e=>{
    const field=e.target.dataset.field;if(!field||e.target.type==='checkbox'||e.target.tagName==='SELECT')return;
    setNodeProperty(field,e.target.value);
  });
  $('inspectorBody').addEventListener('change',e=>{
    const field=e.target.dataset.field;if(!field)return;
    if(e.target.type==='checkbox')setNodeProperty(field,e.target.checked);
    if(e.target.tagName==='SELECT')setNodeProperty(field,e.target.value);
  });
  $('inspectorBody').addEventListener('click',async e=>{
    const id=e.target.closest('button')?.id;
    if(id==='uploadImage'||e.target.closest('#imageDrop'))$('imageInput').click();
    if(id==='captureImage')openCrop('node');
    if(id==='copyImage')copyImage();
    if(id==='removeImage'){const n=nodeById(selectedId);n.image='';render();markDirty();}
    if(id==='testImage')testImage();
    if(id==='previewImage')openLive();
    if(id==='duplicateNode')duplicateNode();
    if(id==='removeNode')deleteNode();
    if(e.target.closest('.unlink')){const p=e.target.closest('.unlink').dataset.port;flow.edges=flow.edges.filter(edge=>!(edge.source===selectedId&&edge.sourceHandle===p));renderInspector();renderEdges();markDirty();}
    if(e.target.closest('#pointPicker')){
      const n=nodeById(selectedId),img=$('pointPicker img'),rect=img.getBoundingClientRect();
      // object-fit: contain can letterbox, so account for its actual rendered image rectangle.
      const ratio=Math.min(rect.width/img.naturalWidth,rect.height/img.naturalHeight);
      const actualW=img.naturalWidth*ratio,actualH=img.naturalHeight*ratio;
      const x0=rect.left+(rect.width-actualW)/2,y0=rect.top+(rect.height-actualH)/2;
      n.clickX=Number(clamp((e.clientX-x0)/actualW,0,1).toFixed(3));
      n.clickY=Number(clamp((e.clientY-y0)/actualH,0,1).toFixed(3));
      renderInspector();markDirty();
    }
  });
  $('testCenterButton').onclick=openTestCenter;
  $('testClose').onclick=closeTestCenter;
  $('testDrop').onclick=()=>$('testInput').click();
  $('testDrop').addEventListener('dragover',e=>{e.preventDefault();$('testDrop').classList.add('drag-over');});
  $('testDrop').addEventListener('dragleave',()=>$('testDrop').classList.remove('drag-over'));
  $('testDrop').addEventListener('drop',async e=>{
    e.preventDefault();$('testDrop').classList.remove('drag-over');
    const file=[...e.dataTransfer.files].find(f=>f.type.startsWith('image/'));
    if(file)try{setTestImage(await blobAsPng(file));}catch(err){toast(err.message,true);}
  });
  $('testInput').onchange=async e=>{const file=e.target.files[0];if(file)try{setTestImage(await blobAsPng(file));}catch(err){toast(err.message,true);}e.target.value='';};
  $('testUpload').onclick=()=>$('testInput').click();
  $('testFromNode').onclick=()=>{const n=nodeById(selectedId);if(!n?.image)toast('請先選擇有圖片的節點',true);else setTestImage(n.image);};
  $('testCapture').onclick=()=>openCrop('test');
  $('testNow').onclick=testCenterNow;
  $('testPreview').onclick=()=>openLive('__test__');
  $('testThreshold').oninput=e=>$('testThresholdLabel').textContent=`${Math.round(Number(e.target.value)*100)}%`;
  $('copyNodes').onclick=()=>copyNodes();
  $('pasteNodes').onclick=async()=>{
    try{const raw=await navigator.clipboard.readText();const data=JSON.parse(raw);if(!pasteNodes(data))pasteNodes();}
    catch(e){if(!pasteNodes())toast('請先 Ctrl+C 複製節點',true);}
  };
  document.addEventListener('copy',e=>{
    if(testOpen||e.target.matches('input,textarea,select,[contenteditable]'))return;
    if(selectedIds.size)copyNodes(e);
  });
  document.addEventListener('paste',async e=>{
    const items=[...e.clipboardData.items],imageItem=items.find(i=>i.type.startsWith('image/'));
    if(imageItem){
      e.preventDefault();
      try{
        const image=await blobAsPng(imageItem.getAsFile());
        if(testOpen){setTestImage(image);toast('已貼圖片到測試區');}
        else {const n=nodeById(selectedId);if(!isImageNode(n)){toast('請先選取圖片節點，或開啟圖片測試區',true);return;}assignImage(image,n.id);}
      }catch(err){toast(`圖片貼上失敗：${err.message}`,true);}return;
    }
    if(testOpen)return;
    const raw=e.clipboardData.getData('text/plain');
    let payload=null;try{payload=JSON.parse(raw);}catch(_ignored){}
    if(payload?.type==='imageflow-nodes'){e.preventDefault();pasteNodes(payload);}
    else if(nodeCopy && !raw.trim()){e.preventDefault();pasteNodes(nodeCopy);}
  });
  document.addEventListener('keydown',e=>{
    const typing=e.target.matches('input,textarea,select,[contenteditable]');
    if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='s'){e.preventDefault();saveFlow(false);}
    if(e.code==='Space'&&!typing&&!testOpen){spaceHeld=true;e.preventDefault();return;}
    if(typing || testOpen || !$('cropModal').hidden || !$('liveModal').hidden)return;
    if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='a'){e.preventDefault();selectedIds=new Set(flow.nodes.map(n=>n.id));selectedId=flow.nodes.at(-1)?.id||null;setNodeClasses();renderInspector();return;}
    if((e.key==='Delete'||e.key==='Backspace')&&selectedIds.size){e.preventDefault();deleteNode();}
    if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='d'){e.preventDefault();duplicateNode();}
    if(e.key==='Escape'&&activeConnection){activeConnection=null;setNodeClasses();$('linkHint').textContent='已取消連線';}
    if(e.key==='Escape'&&liveOpen)closeLive();
  });
  document.addEventListener('keyup',e=>{if(e.code==='Space')spaceHeld=false;});
  window.addEventListener('blur',()=>{spaceHeld=false;});
  $('runButton').onclick=run;$('stopButton').onclick=async()=>{try{await api('/api/stop','POST',{});toast('已發出停止指令');}catch(err){toast(err.message,true);}};
  $('clearLogs').onclick=()=>{logsOffset=state?.logs?.length||0;lastLogFingerprint='';pollState();};
  $('liveButton').onclick=()=>openLive();$('liveClose').onclick=closeLive;
  $('liveExpand').onclick=()=>{const card=document.querySelector('.live-card');card.classList.toggle('expanded');$('liveExpand').textContent=card.classList.contains('expanded')?'⤡ 還原':'⤢ 展開';};
  $('cropClose').onclick=()=>$('cropModal').hidden=true;
  $('cropStage').addEventListener('pointerdown',e=>{if(e.button!==0)return;cropPoint1=stagePosition(e);drawCrop(cropPoint1,cropPoint1);$('cropStage').setPointerCapture(e.pointerId);e.preventDefault();});
  $('cropStage').addEventListener('pointermove',e=>{if(cropPoint1)drawCrop(cropPoint1,stagePosition(e));});
  $('cropStage').addEventListener('pointerup',e=>{if(cropPoint1){drawCrop(cropPoint1,stagePosition(e));cropPoint1=null;}});
  $('cropConfirm').onclick=confirmCrop;
}
async function init() {
  bindEvents();
  try{
    const bootstrap=await api('/api/bootstrap');token=bootstrap.token;
    await refreshMonitors();
    const flows=await api('/api/workflows');
    if(flows.length){flow.id=flows[0].id;await loadFlow(flows[0].id);await refreshFlows();}
    else toast('尚未建立流程，請按「新增」');
    await pollState();setInterval(pollState,650);
  }catch(err){toast(`無法連接 Python 後端：${err.message}`,true);$('boardTitle').textContent='後端連線失敗';}
}
init();
