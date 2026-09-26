const fs=require('fs');
const src=fs.readFileSync('web/static/app.js','utf8');
const esc=src.slice(src.indexOf("function escapeHtml("), src.indexOf("function isTableRow("));
const start=src.indexOf("function isTableRow(");
const end=src.indexOf("/* ----------------------------- settings");
const fn=new Function(esc+src.slice(start,end)+"\n;return {renderMarkdown,isTableRow,isTableDivider,splitRow};")();
const R=fn.renderMarkdown;

const basic = `Here are the engines:

| Engine | Kind | Cost |
| --- | --- | ---: |
| Claude | cloud | $2.61 |
| CustomQWEN | local | free |

That's all.`;
let h=R(basic);
console.log("renders a real table:", h.includes("<table>") && h.includes("<thead>"));
console.log("header cells:", (h.match(/<th[ >]/g)||[]).length===3);
console.log("body rows:", (h.match(/<tr>/g)||[]).length===3);
console.log("no raw pipes left:", !h.includes("| Claude |"));
console.log("right-alignment honoured:", h.includes('text-align:right'));
console.log("surrounding prose kept:", h.includes("Here are the engines") && h.includes("That&#39;s all"));

const noOuter = `Engine | Kind\n--- | ---\nClaude | cloud`;
console.log("optional outer pipes:", R(noOuter).includes("<table>"));

const ragged = `| A | B | C |\n|---|---|---|\n| 1 | 2 |`;
h=R(ragged);
console.log("a short row is padded, not dropped:", (h.match(/<td/g)||[]).length===3);

const notATable = `Use the pipe | character in prose.`;
console.log("prose with a pipe is not a table:", !R(notATable).includes("<table>"));

const inCode = "```\n| not | a | table |\n|---|---|---|\n```";
console.log("a table inside code stays code:", R(inCode).includes("<pre>") && !R(inCode).includes("<table>"));

const escaped = `| Col |\n| --- |\n| a \\| b |`;
console.log("escaped pipes survive:", R(escaped).includes("a | b"));

const inject = `| X |\n| --- |\n| <img src=x onerror=alert(1)> |`;
h=R(inject);
console.log("html in a cell is escaped:", !h.includes("<img") && h.includes("&lt;img"));

const numeric = `| Item | Spend |\n|---|---|\n| trends | 2.61 |`;
console.log("numbers get the numeric class:", R(numeric).includes('class="num"'));

const two = `| A |\n|---|\n| 1 |\n\ntext\n\n| B |\n|---|\n| 2 |`;
console.log("two tables in one message:", (R(two).match(/<table>/g)||[]).length===2);
