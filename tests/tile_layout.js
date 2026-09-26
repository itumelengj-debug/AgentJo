const fs=require('fs');
const src=fs.readFileSync('web/static/app.js','utf8');
const body=src.slice(src.indexOf("const TILE_MIN"), src.indexOf("function sparkline"));
let kids=[], grid={children:kids, clientWidth:0, style:{}, parentElement:null};
global.$=(s)=> s==="#dashTiles" ? grid : null;
const fn=new Function(body+"\n;return layoutTiles;")();
function run(n,width){
  kids.length=0; for(let i=0;i<n;i++) kids.push({style:{}});
  grid.clientWidth=width; grid.style={};
  fn();
  const m=/repeat\((\d+),/.exec(grid.style.gridTemplateColumns||"");
  const cols=m?Number(m[1]):null;
  const spans=kids.map(k=>{const s=/span (\d+)/.exec(k.style.gridColumn||"");return s?Number(s[1]):1;});
  // walk rows the way CSS grid does
  const rows=[]; let cur=0, count=0;
  spans.forEach(s=>{ if(cur+s>cols){rows.push(cur);cur=0;count=0;} cur+=s; count++; });
  if(cur) rows.push(cur);
  return {cols, rows};
}
console.log("tiles width  cols  each row's filled columns   every row full?");
let allFull=true;
for (const n of [5,8,11,12,13,17]) for (const w of [520,900,1360,1920]) {
  const {cols,rows}=run(n,w);
  const full=rows.every(r=>r===cols);
  allFull = allFull && full;
  if (n===11||n===13) console.log(String(n).padEnd(6),String(w).padEnd(6),String(cols).padEnd(5),rows.join("/").padEnd(26),full?"yes":"NO");
}
console.log("\nevery row completely filled, all cases:", allFull);
console.log("zero tiles safe:", (()=>{try{run(0,1200);return true}catch(e){return false}})());
console.log("hidden pane safe:", (()=>{const r=run(11,0);return r.cols===null})());
