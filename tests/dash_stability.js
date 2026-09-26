const fs=require('fs');
let created=0, cleared=0;
function node(tag){created++;return {tag,children:[],attrs:{},style:{},hidden:false,textContent:"",className:"",
  classList:{_s:new Set(),add(...c){c.forEach(x=>this._s.add(x))},remove(...c){c.forEach(x=>this._s.delete(x))},
    toggle(c,on){on?this._s.add(c):this._s.delete(c)},contains(c){return this._s.has(c)}},
  set innerHTML(v){if(v===""){cleared++;this.children.length=0}},get innerHTML(){return ""},
  appendChild(c){this.children.push(c);return c},append(...c){this.children.push(...c)},
  addEventListener(){},setAttribute(){},removeAttribute(){},getAttribute(){return null},
  focus(){},remove(){},querySelector(){return null},querySelectorAll(){return []},
  get clientWidth(){return 1200},get parentElement(){return null}};}
const store={}; const E=id=>store[id]||(store[id]=node(id));
const mq={matches:false,addEventListener(){},addListener(){}};
global.localStorage={_d:{},getItem(k){return this._d[k]??null},setItem(){},removeItem(){}};
global.window={fetch:()=>Promise.resolve({ok:true,json:()=>Promise.resolve({})}),addEventListener(){},matchMedia:()=>mq,location:{href:'',origin:'http://x'}};
global.document={documentElement:node('html'),body:{classList:{add(){},remove(){},contains(){return false}}},
  getElementById:id=>E(id),querySelector:s=>{const m=/^#([A-Za-z0-9_-]+)$/.exec(s);return m?E(m[1]):node('g')},
  querySelectorAll:()=>[],createElement:t=>node(t),createElementNS:(n,t)=>node(t),addEventListener(){},createTextNode:()=>({})};
global.navigator={userAgent:'n',clipboard:{writeText(){}}};
global.EventSource=function(){this.addEventListener=()=>{};this.close=()=>{}};
global.alert=()=>{};global.confirm=()=>true;global.setTimeout=(f)=>0;global.setInterval=()=>0;global.clearTimeout=()=>{};global.matchMedia=()=>mq;

const mk=(tokens,spark)=>({at:"1",all_clear:true,counts:{},needs_you:[],
  running:{engine:"Auto",spend:{usd:0,cap:0}},
  tiles:[{key:"tokens",label:"Tokens",value:tokens,sub:"s",spark,progress:40},
         {key:"spend",label:"Spend",value:"$1.20",sub:"of $10",progress:12},
         {key:"mem",label:"Memories",value:"190",sub:"facts"}]});
let payload=mk("2.01M",[1,2,3,4]);
global.fetch=(u)=>Promise.resolve({ok:true,json:()=>Promise.resolve(u.includes('/api/dashboard')?payload:{})});

const src=fs.readFileSync('web/static/app.js','utf8');
const api=new Function(src+"\n;return {refreshDashboard};")();
(async()=>{
  await api.refreshDashboard();                 // first paint
  const afterFirst=created, clearsAfterFirst=cleared;
  console.log("first paint animates:", E('dashTiles').classList.contains('first-paint'));
  for(let i=0;i<5;i++) await api.refreshDashboard();   // identical polls
  console.log("5 identical refreshes create 0 nodes:", created===afterFirst);
  console.log("...and clear nothing:", cleared===clearsAfterFirst);
  payload=mk("2.14M",[1,2,3,4]);                 // a value changed
  await api.refreshDashboard();
  console.log("changed value creates at most a couple of nodes:", created-afterFirst<=3, "(delta "+(created-afterFirst)+")");
  console.log("new value is shown:", E('dashTiles').children[0].children[0].textContent==="2.14M");
  console.log("no re-animation on update:", E('dashTiles').classList.contains('first-paint')===true);
  payload=mk("2.14M",[1,2,3,4,9]);               // sparkline data changed
  const beforeSpark=created;
  await api.refreshDashboard();
  console.log("new history redraws only the sparkline:", created>beforeSpark && created<beforeSpark+15);
  const p2={...mk("1",[1,2]),tiles:[{key:"only",label:"One",value:"1"}]};
  payload=p2;
  await api.refreshDashboard();
  console.log("a changed tile SET does rebuild:", E('dashTiles').children.length===1);
})();
