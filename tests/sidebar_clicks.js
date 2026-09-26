const fs=require('fs');
const html=fs.readFileSync('web/static/index.html','utf8');
const ids=[...html.matchAll(/id="([^"]+)"/g)].map(m=>m[1]);
function node(id){return {id,children:[],attrs:{},style:{},hidden:true,textContent:"",value:"",checked:false,type:"",
  classList:{_s:new Set(),add(...c){c.forEach(x=>this._s.add(x))},remove(...c){c.forEach(x=>this._s.delete(x))},
    toggle(c,on){on?this._s.add(c):this._s.delete(c)},contains(c){return this._s.has(c)}},
  set innerHTML(v){if(v==="")this.children.length=0},get innerHTML(){return ""},
  appendChild(c){this.children.push(c);return c},append(...c){this.children.push(...c)},
  _clicks:[],addEventListener(ev,fn){if(ev==="click")this._clicks.push(fn)},
  click(){this._clicks.forEach(f=>f({target:this}))},
  setAttribute(k,v){this.attrs[k]=v},removeAttribute(k){delete this.attrs[k]},getAttribute(){return null},
  focus(){},remove(){},querySelector(){return null},querySelectorAll(){return []},
  get clientWidth(){return 1200},get parentElement(){return null}};}
const store={}; const E=id=>store[id]||(store[id]=node(id));
const mq={matches:false,addEventListener(){},addListener(){}};
global.localStorage={_d:{},getItem(k){return this._d[k]??null},setItem(k,v){this._d[k]=v},removeItem(k){delete this._d[k]}};
global.window={fetch:()=>Promise.resolve({ok:true,json:()=>Promise.resolve({})}),addEventListener(){},matchMedia:()=>mq,location:{href:'',origin:'http://x'}};
global.document={documentElement:node('html'),body:node('body'),getElementById:id=>E(id),
  querySelector:s=>{const m=/^#([A-Za-z0-9_-]+)$/.exec(s);return m?E(m[1]):node('g')},
  querySelectorAll:()=>[],createElement:t=>node(t),createElementNS:(n,t)=>node(t),addEventListener(){},createTextNode:()=>({})};
global.fetch=(u)=>Promise.resolve({ok:true,json:()=>Promise.resolve(
  u.includes('/api/health')?{at:'x',build:'b',counts:{ok:1},checks:[{name:'Build',group:'Deploy',state:'ok',detail:'d',fix:''}]}:
  u.includes('/api/capabilities')?{total:1,counts:{used:1},capabilities:[{key:'k',name:'n',group:'Core',state:'used',test:'t'}]}:
  u.includes('/api/tour')?{chapters:['A'],stops:[{key:'k',chapter:'A',title:'T',what:'w',why:'y','try':'t',panel:''}]}:{})});
global.navigator={userAgent:'n',clipboard:{writeText(){}}};
global.EventSource=function(){this.addEventListener=()=>{};this.close=()=>{}};
global.alert=()=>{};global.confirm=()=>true;global.setTimeout=(f)=>{try{f&&f()}catch(e){}return 0};
global.setInterval=()=>0;global.clearTimeout=()=>{};global.matchMedia=()=>mq;

const src=fs.readFileSync('web/static/app.js','utf8');
const api=new Function(src+"\n;return {wireEvents};")();
api.wireEvents();
const btns=[...html.matchAll(/<button class="foot-btn" id="([A-Za-z]+)"/g)].map(m=>m[1]);
let bad=[];
for(const b of btns){
  const el=E(b);
  if(!el._clicks.length){ bad.push(b+" (no click handler)"); continue; }
  try{ el.click(); }catch(e){ bad.push(b+" -> "+e.constructor.name+": "+e.message); }
}
console.log("sidebar buttons:", btns.length);
console.log("buttons that do nothing or throw:", bad.length?bad:"none");
