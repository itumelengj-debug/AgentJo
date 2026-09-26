const fs=require('fs');
const html=fs.readFileSync('web/static/index.html','utf8');
const ids=[...html.matchAll(/id="([^"]+)"/g)].map(m=>m[1]);
function node(id){return {id,children:[],attrs:{},style:{},hidden:true,textContent:"",value:"",disabled:false,
  classList:{_s:new Set(),add(...c){c.forEach(x=>this._s.add(x))},remove(...c){c.forEach(x=>this._s.delete(x))},
    toggle(c,on){on?this._s.add(c):this._s.delete(c)},contains(c){return this._s.has(c)}},
  set innerHTML(v){if(v==="")this.children.length=0},get innerHTML(){return ""},
  appendChild(c){this.children.push(c);return c},append(...c){this.children.push(...c)},
  _clicks:[],addEventListener(ev,fn){if(ev==="click")this._clicks.push(fn)},
  click(){opened.push(this.id);this._clicks.forEach(f=>f({target:this}))},
  setAttribute(k,v){this.attrs[k]=v},removeAttribute(k){delete this.attrs[k]},getAttribute(){return null},
  focus(){},remove(){},querySelector(){return null},querySelectorAll(){return []},
  get clientWidth(){return 1200},get parentElement(){return null}};}
const opened=[]; const store={}; const E=id=>store[id]||(store[id]=node(id));
const mq={matches:false,addEventListener(){},addListener(){}};
global.localStorage={_d:{},getItem(k){return this._d[k]??null},setItem(k,v){this._d[k]=v},removeItem(k){delete this._d[k]}};
const SCENES={scenes:[
 {key:"a",act:"Act 1",title:"T1",say:"S1",note:"N1",panel:"",seconds:5,index:0},
 {key:"b",act:"Act 1",title:"T2",say:"S2",note:"N2",panel:"healthBtn",seconds:5,index:1},
 {key:"c",act:"Act 2",title:"T3",say:"S3",note:"",panel:"auditBtn",seconds:5,index:2}],acts:["Act 1","Act 2"],count:3};
global.window={fetch:()=>Promise.resolve({ok:true,json:()=>Promise.resolve({})}),addEventListener(){},matchMedia:()=>mq,location:{href:'',origin:'http://x'}};
const keyHandlers=[];
global.document={documentElement:node('html'),
  body:{classList:{_s:new Set(),add(c){this._s.add(c)},remove(c){this._s.delete(c)},contains(c){return this._s.has(c)}}},
  getElementById:id=>E(id),
  querySelector:s=>{const m=/^#([A-Za-z0-9_-]+)$/.exec(s);return m?E(m[1]):node('g')},
  querySelectorAll:()=>[],createElement:t=>node(t),createElementNS:(n,t)=>node(t),
  addEventListener(ev,fn){if(ev==="keydown")keyHandlers.push(fn)},
  removeEventListener(ev,fn){const i=keyHandlers.indexOf(fn);if(i>=0)keyHandlers.splice(i,1)},
  createTextNode:()=>({})};
global.fetch=(u)=>Promise.resolve({ok:true,json:()=>Promise.resolve(u.includes('/api/presenter')?SCENES:{})});
global.navigator={userAgent:'n',clipboard:{writeText(){}}};
global.EventSource=function(){this.addEventListener=()=>{};this.close=()=>{}};
global.alert=()=>{};global.confirm=()=>true;
global.setTimeout=(f,ms)=>{return {f,ms}};global.clearTimeout=()=>{};global.setInterval=()=>0;global.matchMedia=()=>mq;

const src=fs.readFileSync('web/static/app.js','utf8');
const api=new Function(src+"\n;return {startPresenter,presenterGo,exitPresenter,_pres};")();
(async()=>{
  await api.startPresenter();
  console.log("presenter opens:", E('presenter').hidden===false);
  console.log("shows first scene:", E('presTitle').textContent==="T1" && E('presCount').textContent==="1/3");
  console.log("speaker notes hidden by default:", E('presNote').hidden===true);
  console.log("keyboard is bound:", keyHandlers.length===1);
  opened.length=0;
  api.presenterGo(1);
  console.log("advances and opens that scene's real panel:", E('presTitle').textContent==="T2" && opened.includes('healthBtn'));
  api.presenterGo(1);
  console.log("last scene says Finish:", E('presNext').textContent==="Finish");
  api.presenterGo(-1);
  console.log("goes back:", E('presTitle').textContent==="T2");
  api.presenterGo(-1); api.presenterGo(-1);
  console.log("won't run off the front:", E('presCount').textContent==="1/3" && E('presPrev').disabled===true);
  // advancing past the end exits cleanly
  api.presenterGo(1); api.presenterGo(1); api.presenterGo(1);
  console.log("finishing exits and unbinds:", E('presenter').hidden===true && keyHandlers.length===0);
  console.log("body class cleaned up:", document.body.classList.contains('presenting')===false);
})();
