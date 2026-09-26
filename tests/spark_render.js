const fs=require('fs');
const src=fs.readFileSync('web/static/app.js','utf8');
global.document={createElementNS:(ns,tag)=>({tag,attrs:{},kids:[],
  setAttribute(k,v){this.attrs[k]=v;},append(...c){this.kids.push(...c);},appendChild(c){this.kids.push(c);return c;}})};
const fn=new Function(src.slice(src.indexOf('let _sparkSeq'), src.indexOf('async function refreshDashboard'))+'\n;return sparkline;')();
const svg=fn([0,5,3,9,12,4,20]);
const g=(c)=>svg.kids.find(k=>k.attrs.class===c);
const line=g('dash-spark-line'), area=g('dash-spark-area'), dot=g('dash-spark-dot');
const nums=(line.attrs.d.match(/-?\d+\.?\d*/g)||[]).map(Number);
console.log('line drawn from every point:', /^M[\d.]+,[\d.]+( L[\d.]+,[\d.]+){6}$/.test(line.attrs.d));
console.log('area filled with a gradient:', /^url\(#spk\d+\)$/.test(area.attrs.fill));
console.log('latest reading is marked:', !!dot && Number.isFinite(Number(dot.attrs.cy)));
console.log('all coords finite:', nums.every(Number.isFinite));
console.log('y stays inside the box:', nums.filter((_,i)=>i%2===1).every(y=>y>=0&&y<=24));
const flat=fn([0,0,0]);
console.log('flat series safe:', !flat.kids.find(k=>k.attrs.class==='dash-spark-line').attrs.d.includes('NaN'));
const one=fn([7]);
console.log('single point safe:', !one.kids.find(k=>k.attrs.class==='dash-spark-line').attrs.d.includes('NaN'));
const neg=fn([-5,2,-3,8]);
console.log('negative values safe:', !neg.kids.find(k=>k.attrs.class==='dash-spark-line').attrs.d.includes('NaN'));
const a=fn([1,2]), b=fn([3,4]);
console.log('gradient ids unique per tile:',
  a.kids.find(k=>k.attrs.class==='dash-spark-area').attrs.fill !== b.kids.find(k=>k.attrs.class==='dash-spark-area').attrs.fill);
