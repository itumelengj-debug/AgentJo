const fs=require('fs');
const html=fs.readFileSync('web/static/index.html','utf8');
const ids=[...html.matchAll(/id="([^"]+)"/g)].map(m=>m[1]);
function node(id){return {id,children:[],attrs:{},style:{},hidden:true,textContent:"",
  classList:{_s:new Set(),add(...c){c.forEach(x=>this._s.add(x))},remove(...c){c.forEach(x=>this._s.delete(x))},
    toggle(c,on){on?this._s.add(c):this._s.delete(c)},contains(c){return this._s.has(c)}},
  set innerHTML(v){if(v==="")this.children.length=0},get innerHTML(){return ""},
  appendChild(c){this.children.push(c);return c},append(...c){this.children.push(...c)},
  _clicks:[], addEventListener(ev,fn){if(ev==="click")this._clicks.push(fn)},
  click(){this._clicks.forEach(f=>f({target:this}))},
  setAttribute(k,v){this.attrs[k]=v},removeAttribute(k){delete this.attrs[k]},getAttribute(){return null},
  focus(){},remove(){},querySelector(){return null},querySelectorAll(){return []},
  get clientWidth(){return 1200},get parentElement(){return null}};}
const store={}; const E=id=>store[id]||(store[id]=node(id));
const mq={matches:false,addEventListener(){},addListener(){}};
const LS={_d:JSON.parse(process.argv[2]||"{}"),getItem(k){return this._d[k]??null},setItem(k,v){this._d[k]=v},removeItem(k){delete this._d[k]}};
global.localStorage=LS;
global.window={fetch:()=>Promise.resolve({ok:true,json:()=>Promise.resolve({})}),addEventListener(){},matchMedia:()=>mq,location:{href:'',origin:'http://x'}};
global.document={documentElement:node('html'),body:node('body'),
  getElementById:id=>E(id),
  querySelector:s=>{const m=/^#([A-Za-z0-9_-]+)$/.exec(s);return m?E(m[1]):node('g')},
  querySelectorAll:()=>[],createElement:t=>node(t),createElementNS:(n,t)=>node(t),
  addEventListener(){},createTextNode:()=>({})};
const PAYLOAD={at:"1",all_clear:false,counts:{},needs_you:[{severity:"act",title:"T",detail:"d",panel:"backupBtn",why:""}],
  running:{engine:"Auto",spend:{usd:0,cap:0}},tiles:[{label:"L",value:"1",sub:"s"}]};
global.fetch=(u)=>Promise.resolve({ok:true,json:()=>Promise.resolve(u.includes("/api/dashboard")?PAYLOAD:{})});
global.navigator={userAgent:'n',clipboard:{writeText(){}}};
global.EventSource=function(){this.addEventListener=()=>{};this.close=()=>{}};
global.alert=()=>{};global.confirm=()=>true;global.setTimeout=(f)=>{try{f&&f()}catch(e){}return 0};
global.setInterval=()=>0;global.clearTimeout=()=>{};global.matchMedia=()=>mq;

const src=fs.readFileSync('web/static/app.js','utf8');
(async()=>{
  const api=new Function(src+"\n;return {wireEvents:typeof wireEvents!=='undefined'?wireEvents:null,initKpiToggle:typeof initKpiToggle!=='undefined'?initKpiToggle:null,refreshStats:typeof refreshStats!=='undefined'?refreshStats:null,refreshDashboard:typeof refreshDashboard!=='undefined'?refreshDashboard:null};")();
  console.log("stored prefs:", JSON.stringify(LS._d));
  api.wireEvents();
  if(api.initKpiToggle) api.initKpiToggle(); else console.log("*** initKpiToggle not exported");
  console.log("after boot: dash.hidden =", E('dash').hidden);
  await api.refreshStats();
  console.log("after refreshStats: dash.hidden =", E('dash').hidden, "| tiles rendered:", E('dashTiles').children.length);
  console.log("clicking Show dashboard...");
  E('kpiToggle').click();
  await new Promise(r=>global.setTimeout?r():r());
  await api.refreshDashboard();
  console.log("after click: dash.hidden =", E('dash').hidden, "| tiles:", E('dashTiles').children.length);
})();
