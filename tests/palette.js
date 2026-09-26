const fs=require('fs');
const html=fs.readFileSync('web/static/index.html','utf8');
const footBtns=[...html.matchAll(/<button class="foot-btn" id="([A-Za-z]+)">([\s\S]*?)<\/button>/g)]
  .map(m=>({id:m[1], text:m[2].replace(/<[^>]+>/g,' ').replace(/\s+/g,' ').trim()}));
function node(id){const n={id,children:[],attrs:{},style:{},dataset:{},hidden:false,textContent:"",value:"",
  classList:{_s:new Set(),add(...c){c.forEach(x=>this._s.add(x))},remove(...c){c.forEach(x=>this._s.delete(x))},
    toggle(c,on){on?this._s.add(c):this._s.delete(c);return !!on},contains(c){return this._s.has(c)}},
  set innerHTML(v){if(v==="")this.children.length=0},get innerHTML(){return ""},
  appendChild(c){c._parent=this;this.children.push(c);return c},append(...c){c.forEach(x=>x._parent=this);this.children.push(...c)},
  _clicks:[],addEventListener(ev,fn){if(ev==="click")this._clicks.push(fn)},
  click(){clicked.push(this.id);this._clicks.forEach(f=>f({target:this}))},
  scrollIntoView(){},setAttribute(){},removeAttribute(){},getAttribute(){return null},
  focus(){},remove(){const p=this._parent;if(p)p.children=p.children.filter(c=>c!==this)},
  querySelector(){return null},querySelectorAll(){return []},
  get clientWidth(){return 1200},get parentElement(){return this._parent||null}};
  Object.defineProperty(n,"className",{get(){return [...n.classList._s].join(" ")},
    set(v){n.classList._s=new Set(String(v||"").split(/\s+/).filter(Boolean))}});
  return n;}
const clicked=[]; const store={}; const E=id=>store[id]||(store[id]=node(id));
footBtns.forEach(b=>{const n=E(b.id); n.textContent=b.text; n.classList.add("foot-btn");});
const mq={matches:false,addEventListener(){},addListener(){}};
global.localStorage={_d:{},getItem(){return null},setItem(){},removeItem(){}};
global.window={fetch:()=>Promise.resolve({ok:true,json:()=>Promise.resolve({})}),addEventListener(){},matchMedia:()=>mq,location:{href:'',origin:'http://x'}};
const keyHandlers=[];
global.document={documentElement:node('html'),body:{classList:{add(){},remove(){},contains(){return false}}},
  getElementById:id=>E(id),querySelector:s=>{const m=/^#([A-Za-z0-9_-]+)$/.exec(s);return m?E(m[1]):node('g')},
  querySelectorAll:(s)=> s===".foot-btn" ? footBtns.map(b=>E(b.id)) : [],
  createElement:t=>node(t),createElementNS:(n,t)=>node(t),
  addEventListener(ev,fn){if(ev==="keydown")keyHandlers.push(fn)},removeEventListener(){},createTextNode:()=>({})};
global.fetch=()=>Promise.resolve({ok:true,json:()=>Promise.resolve({})});
global.navigator={userAgent:'n',clipboard:{writeText(){}}};
global.EventSource=function(){this.addEventListener=()=>{};this.close=()=>{}};
global.alert=()=>{};global.confirm=()=>true;
global.setTimeout=(f)=>{try{f&&f()}catch(e){}return 0};
global.setInterval=()=>0;global.clearTimeout=()=>{};global.matchMedia=()=>mq;

const src=fs.readFileSync('web/static/app.js','utf8');
const api=new Function(src+"\n;return {wireEvents,openPalette,closePalette,paletteMatches,renderPalette,toast,setNotifyLevel};")();
api.wireEvents();
console.log("sidebar panels found:", footBtns.length>=20);
api.openPalette();
console.log("palette opens:", E('paletteBackdrop').hidden===false);
// Jobs moved to its own app, so three panel-opening entries became one
// that opens it. The palette should still cover every panel that exists.
console.log("lists every panel plus actions:", api.paletteMatches("").length>=footBtns.length+2);
console.log("jobs is reachable from the palette:",
  api.paletteMatches("jobs").length>=1);

const names=(q)=>api.paletteMatches(q).map(x=>x.name);
console.log("finds by purpose, not just name:");
[["spending cap","Settings"],["cv","Jobs"],["what is broken","Health"],
 ["restore a file","Undo"],["phone","Phone"],["tender","Challenges"]].forEach(([q,want])=>{
  const hit=names(q).slice(0,3).some(n=>n.includes(want));
  console.log(`   ${hit?"OK ":"MISS"} "${q}" -> ${names(q).slice(0,2).join(", ")||"(none)"}`);
});
console.log("every word must match:", api.paletteMatches("spending zzzz").length===0);
console.log("name beats description:", names("jobs")[0].includes("Jobs"));

// keyboard drive
api.renderPalette("health");
keyHandlers.forEach(h=>h({key:"Enter",preventDefault(){},ctrlKey:false,metaKey:false}));
console.log("enter opens the highlighted panel:", clicked.includes("healthBtn"));
console.log("and closes the palette:", E('paletteBackdrop').hidden===true);

// toasts
// the harness fires timers immediately, so check the element rather than the stack
// notifications have a level now, and the default ("only what needs me")
// suppresses plain confirmations — so say which level this is testing
api.setNotifyLevel && api.setNotifyLevel("all");
const t1=api.toast("Saved.","ok");
console.log("toast carries its kind:", t1 && t1.classList.contains("ok") && t1.textContent==="Saved.");
// and the gate must actually gate
api.setNotifyLevel && api.setNotifyLevel("important");
console.log("routine confirmations are suppressed:", api.toast("Saved.","ok")===null);
console.log("errors still get through:", !!api.toast("Broke.","bad"));
api.setNotifyLevel && api.setNotifyLevel("off");
console.log("off silences everything:", api.toast("Broke.","bad")===null);
for(let i=0;i<6;i++) api.toast("x");
console.log("and are capped:", E('toasts').children.length<=4);
