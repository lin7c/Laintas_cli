# SPDX-License-Identifier: MIT
"""Offline observability pages: one run view per commit, one dashboard per repository.

Nothing is fetched at view time. Scores lead each page; supporting evidence
is grouped into expandable sections.
"""
import base64
import hashlib
import json

CSS = r"""
:root{
--bg:#0b0e13;--panel:#111620;--panel2:#151b26;--head:#0e131b;--line:#1f2733;--line2:#2b3543;
--text:#e6edf3;--muted:#8b98a9;--dim:#5f6c7d;--accent:#ff9c31;--teal:#39c2b4;--violet:#9b8cff;
--good:#52c07a;--bad:#e8695d;--grid:#1a222d;
--mono:'SFMono-Regular',Consolas,'Liberation Mono',Menlo,monospace;
--sans:'Inter','Helvetica Neue',Arial,sans-serif}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:13.5px/1.6 var(--mono);-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
button,select{font:inherit;color:inherit}button{cursor:pointer}
button:focus-visible,a:focus-visible,select:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
h1,h2,h3,h4,p,table{margin:0}
.num{font-variant-numeric:tabular-nums}
.shell{max-width:1460px;margin:0 auto;padding:0 20px 56px}
.skip{position:absolute;top:-80px;left:16px;background:#fff;color:#000;padding:10px;z-index:9}.skip:focus{top:8px}
.topbar{background:var(--head);border-bottom:1px solid var(--line2);position:sticky;top:0;z-index:5}
.topbar .shell{padding-block:0;display:flex;align-items:center;gap:16px;height:58px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:10px;font:700 13px var(--sans);letter-spacing:.04em}
.mark{display:flex;gap:3px;transform:skew(-16deg)}.mark i{width:5px;height:15px;background:var(--accent)}
.mark i:last-child{height:10px;align-self:flex-end;background:var(--text)}
.crumb{color:var(--muted);font-size:12px;display:flex;align-items:center;gap:8px;min-width:0;overflow:hidden}
.crumb b{color:var(--text);font-weight:400}
.spacer{flex:1}
.chip{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line2);border-radius:2px;padding:4px 9px;font-size:11px;color:var(--muted);letter-spacing:.06em;text-transform:uppercase;white-space:nowrap}
.chip.ok{color:var(--good);border-color:#2a4636}.chip.warn{color:var(--accent);border-color:#4a3a22}
.chip.dot:before{content:'';width:6px;height:6px;border-radius:50%;background:currentColor}
.btn{border:1px solid var(--line2);background:var(--panel);color:var(--muted);padding:7px 13px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;border-radius:2px;min-height:30px;display:inline-flex;align-items:center;gap:6px}
.btn:hover{color:var(--text);border-color:var(--dim);text-decoration:none}
.pagehead{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;flex-wrap:wrap;margin:22px 0 14px}
.pagehead h1{font:600 23px/1.25 var(--sans);letter-spacing:-.01em;overflow-wrap:anywhere;max-width:820px}
.pagehead .sub{color:var(--muted);font-size:12px;margin-top:7px}
.pagehead .meta{display:flex;gap:8px;flex-wrap:wrap}
.hint{color:var(--dim);font-size:11.5px;letter-spacing:.02em}
.grid{display:grid;gap:1px;background:var(--line);border:1px solid var(--line);margin-bottom:16px}
.g6{grid-template-columns:repeat(6,1fr)}.g4{grid-template-columns:repeat(4,1fr)}
.g3{grid-template-columns:repeat(3,1fr)}.g2{grid-template-columns:repeat(2,1fr)}
.g21{grid-template-columns:2fr 1fr}.g12{grid-template-columns:1fr 2fr}
.stat{background:var(--panel);padding:15px 17px 16px;min-width:0;display:flex;flex-direction:column;gap:2px}
.stat .label{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);display:flex;justify-content:space-between;gap:10px;align-items:baseline}
.stat .label>span:first-child,.stat .label{white-space:nowrap}
.stat .value{font:600 31px/1.2 var(--sans);letter-spacing:-.02em;overflow-wrap:anywhere}
.stat .value em{font:400 12.5px var(--mono);color:var(--dim);font-style:normal;margin-left:5px;letter-spacing:.04em}
.stat .sub{color:var(--muted);font-size:12px;overflow-wrap:anywhere}
.stat .spark{margin-top:9px;height:30px}
.delta{font-size:11px;letter-spacing:.04em;color:var(--muted);white-space:nowrap}
.delta.up:before{content:'▲ ';color:var(--muted)}.delta.down:before{content:'▼ ';color:var(--muted)}
.panel{background:var(--panel);min-width:0;display:flex;flex-direction:column}
.panel>header{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:11px 17px;border-bottom:1px solid var(--line);background:var(--panel2)}
.panel>header h3{font:600 12.5px var(--sans);letter-spacing:.09em;text-transform:uppercase;color:var(--text)}
.panel>header .unit{font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);text-align:right}
.panel .body{padding:15px 17px 17px;min-width:0;flex:1}
.panel .body.flush{padding:0}
.kv{display:flex;justify-content:space-between;align-items:baseline;gap:12px;padding:5px 0;border-bottom:1px dotted var(--line2);color:var(--muted);font-size:12.5px}
.kv:last-child{border-bottom:0}.kv span,.kv b{min-width:0;overflow-wrap:anywhere}
.kv b{color:var(--text);font-weight:400;text-align:right}
.kv.none b{color:var(--dim)}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;color:var(--dim);font-weight:400;font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;padding:10px 13px;border-bottom:1px solid var(--line);background:var(--panel2)}
.scroll thead th{position:sticky;top:0;z-index:1}
td{padding:10px 13px;border-bottom:1px solid var(--line);color:var(--muted);overflow-wrap:anywhere}
tr:hover td{background:var(--panel2)}
td.key{color:var(--text)}td.r,th.r{text-align:right}
.bar{display:flex;align-items:center;gap:8px;justify-content:flex-end}
.bar i{display:block;height:6px;background:var(--accent);min-width:1px}
.track{flex:1;max-width:120px;height:6px;background:var(--grid)}
.scroll{max-height:360px;overflow:auto}
.tree{font:12.5px/1.8 var(--mono);color:var(--muted);white-space:pre;overflow:auto;max-height:210px;margin:0}
.tree b{color:var(--accent);font-weight:400}
.legend{display:flex;gap:16px;flex-wrap:wrap;color:var(--dim);font-size:11px;margin-top:10px}
.legend i{display:inline-block;width:9px;height:9px;margin-right:6px;vertical-align:-1px}
svg{display:block}
.axis{fill:var(--dim);font:11px var(--mono)}
.note{color:var(--dim);font-size:11.5px;margin-top:10px;line-height:1.7}
.prov{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1px;background:var(--line)}
.prov div{background:var(--panel);padding:13px 15px 15px}
.prov h4{font:600 11.5px var(--sans);letter-spacing:.12em;text-transform:uppercase;margin-bottom:8px}
.prov ul{margin:0;padding-left:16px;color:var(--muted);font-size:12px;line-height:1.85}
.prov .obs{color:var(--teal)}.prov .der{color:var(--accent)}.prov .inf{color:var(--violet)}
.checks{display:flex;flex-wrap:wrap;gap:7px}
.big-total{display:flex;align-items:flex-end;gap:18px;flex-wrap:wrap}
.big-total b{font:600 46px/1 var(--sans);letter-spacing:-.03em}
.pattern b{font:600 24px var(--sans);letter-spacing:-.01em;display:block;margin-bottom:4px}
.lvl{display:grid;grid-template-columns:minmax(0,1fr) 82px;gap:6px 12px;padding:8px 0;border-bottom:1px dotted var(--line2);font-size:12.5px;color:var(--muted)}
.lvl:last-child{border-bottom:0}
.lvl .tag{text-align:right;font-size:11px;letter-spacing:.1em}
.lvl .tag.high{color:var(--accent)}.lvl .tag.low{color:var(--teal)}.lvl .tag.medium{color:var(--muted)}
.lvl .why{grid-column:1/3;color:var(--dim);font-size:11.5px}
.foot{display:flex;justify-content:space-between;gap:20px;flex-wrap:wrap;color:var(--dim);font-size:11px;border-top:1px solid var(--line);padding-top:14px;margin-top:26px}
@media(max-width:1180px){.g6{grid-template-columns:repeat(3,1fr)}.g4{grid-template-columns:repeat(2,1fr)}
.g21,.g12,.g3{grid-template-columns:1fr}}
@media(max-width:720px){.shell{padding:0 12px 40px}.g6,.g4,.g3,.g2{grid-template-columns:1fr}
.prov{grid-template-columns:1fr}.scroll thead th{position:static}.topbar .shell{height:auto;padding-block:8px}
.pagehead h1{font-size:17px}table.compact td.opt,table.compact th.opt{display:none}}
@media print{body{background:#fff;color:#111}.topbar{position:static}.btn{display:none}
.panel,.stat,.prov div{background:#fff}.grid,.prov{background:#d8dee6;border-color:#d8dee6}
td,th{border-color:#e2e7ee}.kv,.note,.hint{color:#4b5563}}
/* Score-led report: a quiet canvas, one dominant number, progressive detail. */
:root{--bg:#0e1215;--panel:#151b1f;--panel2:#1a2227;--head:#0e1215;--line:#293238;--line2:#364249;--text:#f2f3ed;--muted:#b0bcbf;--dim:#91a1a6;--accent:#dbed9b;--teal:#83c8be;--sans:'Avenir Next','Trebuchet MS','Noto Sans CJK SC',sans-serif}
body{font-family:var(--sans);font-size:14px}.shell{max-width:1180px;padding-inline:32px}
.topbar{position:static}.topbar .shell{min-height:72px;height:auto;padding-block:16px}.chip{letter-spacing:0;text-transform:none;border-radius:30px}.btn{letter-spacing:0;text-transform:none;border-radius:6px;min-height:38px}
.pagehead{margin:36px 0 24px}.pagehead h1{font-size:26px}.pagehead .sub{font-size:13px}.pagehead .meta{display:none}
.score-hero{display:grid;grid-template-columns:1.25fr 1fr;border:1px solid var(--line2);border-radius:16px;background:var(--panel);overflow:hidden;margin-bottom:28px}
.score-main{padding:32px 40px;background:radial-gradient(ellipse at 0 100%,#dbed9b0d,transparent 75%)}
.eyebrow{font-size:12px;letter-spacing:.14em;color:var(--accent);margin-bottom:14px}.score-label{font-size:19px;font-weight:500}.score-value{display:block;font:500 clamp(72px,8vw,112px)/1.15 var(--sans);letter-spacing:-.065em;color:var(--accent);font-variant-numeric:tabular-nums;margin:10px 0}.score-value small{font-size:22px;letter-spacing:0;color:var(--dim);margin-left:12px}.score-caption{color:var(--muted);font-size:13px;max-width:430px;line-height:1.8}
.score-aside{padding:36px;border-left:1px solid var(--line);display:flex;flex-direction:column;justify-content:center;gap:20px}.score-aside h3{font-size:15px;font-weight:500}.score-aside .kv{padding:12px 0;font-size:14px}.score-aside .kv b{font:500 22px var(--sans)}.score-aside .note{margin:0}
.section-heading{display:flex;align-items:baseline;justify-content:space-between;gap:16px;margin:30px 0 16px}.section-heading h2{font-size:19px;font-weight:500}.section-heading p{color:var(--dim);font-size:12px}
.grid{gap:16px;background:transparent;border:0;margin-bottom:20px}.g6{grid-template-columns:repeat(3,minmax(0,1fr))}.panel,.stat{border:1px solid var(--line);border-radius:10px;overflow:hidden}.panel>header{padding:16px 20px;background:transparent}.panel>header h3{letter-spacing:0;text-transform:none;font-size:14px}.panel .body{padding:20px}.panel .body.flush{padding:0}.stat{padding:20px}.stat .label{letter-spacing:0;text-transform:none}.stat .value{font-size:26px}.note,.hint{font-size:12px}
.detail-group{border-top:1px solid var(--line);padding:0 0 4px}.detail-group>summary{cursor:pointer;list-style:none;display:flex;align-items:center;gap:18px;padding:22px 0;font-size:16px;min-height:68px}.detail-group>summary::-webkit-details-marker{display:none}.detail-group>summary:after{content:'+';margin-left:auto;color:var(--accent);font:24px var(--mono)}.detail-group[open]>summary:after{content:'−'}.detail-group>summary span{font-size:12px;color:var(--dim)}summary:focus-visible{outline:2px solid var(--accent);outline-offset:4px}
.iterations{border:1px solid var(--line);border-radius:10px;overflow:hidden}.iterations td{padding:18px 20px}.iterations th{padding:14px 20px;letter-spacing:0;text-transform:none}.iterations .iteration-score{color:var(--accent);font-size:23px;white-space:nowrap}.iterations .iteration-title{display:block;color:var(--text);font-size:14px;margin-bottom:5px}.iterations .scroll{max-height:490px}.iterations .hint{font-family:var(--mono);font-size:11px}.iterations td:first-child{width:64%}.score-track{height:5px;background:var(--line2);margin-top:14px;border-radius:4px;overflow:hidden}.score-track i{display:block;height:100%;background:var(--accent)}
@media(max-width:720px){.shell{padding-inline:18px}.topbar .shell{gap:10px}.topbar #crumb{display:none}.topbar #chips{order:5;flex-basis:100%;flex-wrap:wrap;overflow:visible}.pagehead{margin-top:26px}.pagehead h1{font-size:22px}.score-hero{grid-template-columns:1fr}.score-main{padding:26px}.score-value{font-size:80px}.score-aside{border-left:0;border-top:1px solid var(--line);padding:24px;gap:12px}.score-aside .kv{padding:7px 0}.g6{grid-template-columns:1fr}.detail-group>summary{flex-wrap:wrap;gap:6px}.detail-group>summary span{flex-basis:75%;font-size:11px}.section-heading p{display:none}.iterations td,.iterations th{padding:13px 12px}.iterations .iteration-score{font-size:21px}.iterations .opt{display:none}.score-caption{font-size:12px}}
@media print{.score-main{background:none}.score-value,.iterations .iteration-score{color:#314b16}.score-hero{break-inside:avoid}details>summary{display:none}.score-aside{border-color:#ccc}.score-caption{color:#444}}

"""

HELPERS = r"""
const data=JSON.parse(document.getElementById('report-data').textContent);
const $=s=>document.querySelector(s);
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const NA='—';
const isNum=x=>x!==null&&x!==undefined&&!Number.isNaN(Number(x));
const n=(x,d)=>!isNum(x)?NA:Number(x).toLocaleString('en-US',{maximumFractionDigits:d===undefined?1:d});
const short=x=>!isNum(x)?NA:Math.abs(x)>=1e6?(x/1e6).toFixed(2)+'M':Math.abs(x)>=1e3?(x/1e3).toFixed(1)+'k':n(x,0);
const pct=(x,d)=>!isNum(x)?NA:(Number(x)*100).toFixed(d===undefined?1:d)+'%';
const usd=x=>!isNum(x)?NA:'$'+Number(x).toFixed(2);
const rate=(a,b,d)=>!isNum(a)||!b?null:a/b;
const dur=ms=>{if(!isNum(ms))return NA;const s=Math.round(ms/1000);if(s<90)return s+'s';
  const m=Math.round(s/60);if(m<90)return m+'m';const h=Math.floor(m/60);
  if(h<48)return h+'h'+String(m%60).padStart(2,'0');return Math.floor(h/24)+'d'+(h%24)+'h'};
const stamp=x=>new Date(x).toISOString().replace('T',' ').slice(0,16)+'Z';
const dayOf=x=>new Date(x).toISOString().slice(0,10);
const hhmm=x=>new Date(x).toISOString().slice(11,19)+'Z';
const chip=(text,kind)=>`<span class="chip${kind?' '+kind:''}">${esc(text)}</span>`;
const kv=(k,v,cls)=>`<div class="kv${isNum(v)||typeof v==='string'?'':' none'}${cls?' '+cls:''}"><span>${esc(k)}</span><b>${v===null||v===undefined?NA:esc(v)}</b></div>`;
const panel=(title,unit,body,cls)=>`<section class="panel${cls?' '+cls:''}"><header><h3>${esc(title)}</h3><span class="unit">${esc(unit||'')}</span></header><div class="body${cls&&cls.includes('flush')?' flush':''}">${body}</div></section>`;
const stat=(label,value,unit,sub,extra,sparkId)=>`<div class="stat"><span class="label"><span>${esc(label)}</span>${extra||''}</span>
  <span class="value num">${esc(value)}${unit?`<em>${esc(unit)}</em>`:''}</span><span class="sub">${esc(sub||'')}</span>${sparkId?`<div class="spark" id="${esc(sparkId)}"></div>`:''}</div>`;
const disclosure=(title,subtitle,nodes)=>{
  const el=document.createElement('details');el.className='detail-group';
  el.innerHTML=`<summary>${esc(title)}<span>${esc(subtitle)}</span></summary>`;
  nodes.forEach(node=>el.append(node));return el;
};
function scoreLayout(hero,groups,featured){
  const content=$('#content'),head=content.querySelector('.pagehead');
  content.replaceChildren(head);
  content.insertAdjacentHTML('beforeend',hero);
  if(featured)content.insertAdjacentHTML('beforeend',featured);
  content.insertAdjacentHTML('beforeend','<div class="section-heading"><h2>Data & evidence</h2><p>Expand a section to explore the full record</p></div>');
  groups.forEach(([title,subtitle,nodes])=>content.append(disclosure(title,subtitle,nodes)));
  if(data.demo)$('#chips').insertAdjacentHTML('afterbegin',chip('Demo data','warn'));
}
const scoreHero=(label,value,unit,caption,aside)=>`<section class="score-hero" aria-label="${esc(label)}"><div class="score-main"><p class="eyebrow">AI-POW / SCORE REPORT</p><h2 class="score-label">${esc(label)}</h2><strong class="score-value">${n(value,1)}${unit?`<small>${esc(unit)}</small>`:''}</strong><p class="score-caption">${esc(caption)}</p></div><div class="score-aside">${aside}</div></section>`;

function vsBase(value,reference){
  if(!isNum(value)||!isNum(reference)||!Number(reference))return '';
  const r=Number(value)/Number(reference);
  if(r>=.9&&r<=1.1)return '<span class="delta">≈ baseline</span>';
  const text=r>1?r.toFixed(1)+'× base':Math.round((1-r)*100)+'% below';
  return `<span class="delta ${r>1?'up':'down'}">${text}</span>`;
}
const track=(value,scale)=>`<span class="bar"><span class="track"><i style="width:${Math.max(0,Math.min(100,Math.round((value||0)*100/(scale||1))))}%"></i></span></span>`;
function spark(values,host,color){
  const w=host.clientWidth||180,h=26,pad=2;
  const clean=values.map(v=>isNum(v)?Number(v):null),known=clean.filter(v=>v!==null);
  if(known.length<2)return host.innerHTML='';
  const hi=Math.max(...known),lo=Math.min(...known),range=(hi-lo)||1;
  const x=i=>pad+(w-2*pad)*i/Math.max(1,clean.length-1),y=v=>h-pad-(h-2*pad)*(v-lo)/range;
  let d='';clean.forEach((v,i)=>{if(v===null)return;d+=(d?' L':'M')+x(i).toFixed(1)+' '+y(v).toFixed(1)});
  const last=clean.reduce((a,v,i)=>v===null?a:i,-1);
  host.innerHTML=`<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true"><path d="${d}" fill="none" stroke="${color||'#ff9c31'}" stroke-width="1.5" opacity=".9"/>`
    +(last>=0?`<circle cx="${x(last).toFixed(1)}" cy="${y(clean[last]).toFixed(1)}" r="2.2" fill="${color||'#ff9c31'}"/>`:'')+'</svg>';
}
"""

JS_COMMIT = HELPERS + r"""
const c=data.current,s=c.score,m=c.summary,e=s.evidence,d=data.derived,v=data.verification;
const counts=m.event_counts||{},models=m.models||{},agent=m.agent||null,act=m.activity||{},win=m.window||{};
const sum=f=>{let t=0,seen=false;for(const b of Object.values(models)){if(!isNum(b[f]))return null;t+=b[f];seen=true}return seen?t:null};
const modelCalls=Object.values(models).reduce((t,b)=>t+(b.calls||0),0);
const tok=b=>b.tokens_complete===false?null:(b.tokens_estimated||0)+(b.tokens_measured||0);
const kept=e.artifact.retained,ops=e.artifact.operations;
const hours=isNum(win.span_ms)?win.span_ms/3600000:null;
const awc=m.priced_calls?Number(m.reference_usd_known_subtotal):null;
const toolCalls=counts['tool.call']||0;
const trend=data.trend||[],prior=data.baseline;
const RATES={human:(h,k)=>rate(h.human_tokens,k),read:h=>rate(h.visible_tokens,h.visible_events),
  cost:(h,k)=>rate(isNum(h.reference_usd)?Number(h.reference_usd):null,k),
  keepRate:h=>rate(h.retained,h.operations),
  speed:h=>isNum(h.span_ms)&&h.span_ms>=60000?rate(h.retained,h.span_ms/3600000):null,
  tools:(h,k)=>rate(h.tool_calls,k)};
// This commit, the same shapes as a history row, so one formula serves both.
const own={human_tokens:tok(m.human),visible_tokens:tok(m.visible_ai),visible_events:m.visible_ai.events||0,
  reference_usd:m.priced_calls?m.reference_usd_known_subtotal:null,retained:kept,operations:ops,
  span_ms:win.span_ms,tool_calls:counts['tool.call']||0};
const readRow=row=>({human:RATES.human(row,row.retained),read:RATES.read(row),cost:RATES.cost(row,row.retained),
  keepRate:RATES.keepRate(row),speed:RATES.speed(row),tools:RATES.tools(row,row.retained)});
const rates=readRow(own);
const baseTotals=prior?prior.totals:null;
const base=baseTotals?readRow({human_tokens:baseTotals.human_tokens,visible_tokens:baseTotals.visible_tokens,
  visible_events:baseTotals.visible_events,reference_usd:baseTotals.reference_usd_known_subtotal,
  retained:baseTotals.retained,operations:baseTotals.operations,span_ms:baseTotals.span_ms,
  tool_calls:baseTotals.tool_calls}):{};
const baseNote=(value,fmt)=>isNum(value)?' · baseline '+fmt(Number(value)):'';
const trendOf=key=>trend.map(row=>readRow(row)[key]);
document.title=c.commit.slice(0,7)+' · '+data.project+' · AI-PoW run';
if(data.demo)document.querySelector('#chips').insertAdjacentHTML('afterbegin',chip('demo data','warn'));

function waterfall(){
  const first=win.first_ms,last=win.last_ms;
  if(!isNum(first)||!isNum(last)||last<=first)return '<p class="hint">No timestamps were recorded for this interval.</p>';
  const host=$('#waterfall'),w=host.clientWidth||700,lanes=[],marks=[];
  for(const item of d.timeline){
    if(item.kind==='session'&&/started/.test(item.label))lanes.push({from:item.at,to:null});
    else if(item.kind==='session'){const open=[...lanes].reverse().find(l=>l.to===null);if(open)open.to=item.at;else lanes.push({from:item.at-1,to:item.at})}
    else marks.push(item);
  }
  lanes.forEach(l=>{if(l.to===null)l.to=last});
  if(!lanes.length)lanes.push({from:first,to:last});
  const rowH=17,top=20,h=top+lanes.length*rowH+30,pad=2;
  const x=t=>pad+(w-2*pad)*(t-first)/(last-first);
  let svg=`<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="Session and event timeline">`;
  for(let i=0;i<=4;i++){const t=first+(last-first)*i/4;
    svg+=`<line x1="${x(t).toFixed(1)}" x2="${x(t).toFixed(1)}" y1="${top-8}" y2="${h-24}" stroke="#1a222d"/>`
      +`<text class="axis" x="${x(t).toFixed(1)}" y="${h-10}" text-anchor="${i===0?'start':i===4?'end':'middle'}">${esc(hhmm(t))}</text>`}
  lanes.forEach((l,i)=>{const y=top+i*rowH;
    svg+=`<rect x="${x(l.from).toFixed(1)}" y="${y}" width="${Math.max(2,x(l.to)-x(l.from)).toFixed(1)}" height="10" fill="#39c2b4" opacity=".55"/>`
      +`<title>session ${i+1}</title>`});
  marks.forEach(item=>{const color=item.kind==='rework'?'#ff9c31':'#9b8cff';
    svg+=`<line x1="${x(item.at).toFixed(1)}" x2="${x(item.at).toFixed(1)}" y1="${top-6}" y2="${top+lanes.length*rowH+4}" stroke="${color}" stroke-width="1.5" opacity=".8"><title>${esc(item.label+' · '+(item.detail||''))}</title></line>`});
  host.innerHTML=svg+'</svg>';
}

function modelRows(){
  const rows=Object.entries(models).map(([key,b])=>({name:key.split('/')[0],basis:key.split('/')[1],
    calls:b.calls||0,tokens:(b.input_tokens||0)+(b.output_tokens||0),usd:b.reference_usd}));
  if(!rows.length)return '<p class="hint">No model usage was recorded.</p>';
  const top=Math.max(...rows.map(r=>r.tokens))||1;
  return `<table class="compact"><thead><tr><th>Model</th><th class="r">Calls</th><th class="r">Tokens</th><th class="r opt">Share</th></tr></thead><tbody>`
    +rows.sort((a,b)=>b.tokens-a.tokens).map(r=>`<tr><td class="key">${esc(r.name)}<div class="hint">${esc(r.basis)}</div></td>
      <td class="r num">${n(r.calls,0)}</td><td class="r num">${short(r.tokens)}</td><td class="r opt">${track(r.tokens,top)}</td></tr>`).join('')+'</tbody></table>';
}

function toolRows(){
  const rows=(agent&&agent.tool_calls_by_name||[]).slice(0,10);
  if(!rows.length)return '<p class="hint">No tool calls were recorded.</p>';
  const top=rows[0][1]||1;
  return `<table class="compact"><thead><tr><th>Tool</th><th class="r">Calls</th><th class="r opt">Share</th></tr></thead><tbody>`
    +rows.map(([name,calls])=>`<tr><td class="key">${esc(name)}</td><td class="r num">${n(calls,0)}</td><td class="r opt">${track(calls,top)}</td></tr>`).join('')
    +(agent&&agent.other_tool_calls?`<tr><td class="key">other</td><td class="r num">${n(agent.other_tool_calls,0)}</td><td class="r opt"></td></tr>`:'')
    +'</tbody></table>';
}

function tree(){
  if(!agent||!agent.graph.length)return '<p class="hint">No sub-agents were recorded.</p>';
  const spawned=new Set(agent.graph.map(([id])=>id)),kids=new Map(),roots=[];
  const push=(k,id)=>{if(!kids.has(k))kids.set(k,[]);kids.get(k).push(id)};
  for(const [id,parent] of agent.graph){
    if(!parent)roots.push(id);
    else if(spawned.has(parent))push(parent,id);
    else{if(!kids.has(parent)){kids.set(parent,[]);roots.push(parent)}push(parent,id)}}
  const lines=[],seen=new Set();
  const walk=(id,prefix,last,top)=>{
    if(lines.length>=24||seen.has(id))return;seen.add(id);
    lines.push((top?'':prefix+'<b>'+(last?'└─ ':'├─ ')+'</b>')+esc(id.slice(0,28))+(spawned.has(id)?'':'  <span class="hint">(primary)</span>'));
    const children=kids.get(id)||[];
    children.forEach((child,i)=>walk(child,top?'':prefix+(last?'   ':'│  '),i===children.length-1,false))};
  roots.forEach(id=>walk(id,'',true,true));
  if(seen.size<agent.graph.length||agent.truncated)lines.push('<span class="hint">… bounded by capture limits</span>');
  return `<pre class="tree">${lines.join('\n')}</pre>`;
}

function fileRows(){
  if(!d.files.length)return '<p class="hint">No file observations were recorded.</p>';
  const top=Math.max(...d.files.map(f=>f.operations))||1;
  return `<div class="scroll"><table class="compact"><thead><tr><th>Path</th><th class="r">Edits</th>
    <th class="r opt">Writes</th><th class="r opt">Overwritten</th><th class="r">Survival</th></tr></thead><tbody>`
    +d.files.map(f=>{const sv=rate(f.retained,f.operations);
      return `<tr><td class="key">${esc(f.path||('unresolved '+f.id))}<div class="hint">${n(f.operations,0)} edits${f.reintroduced?' · '+n(f.reintroduced,0)+' re-added':''}</div></td>
        <td class="r num">${n(f.operations,0)}</td><td class="r opt num">${n(f.writes,0)}</td>
        <td class="r opt num">${n(f.overwritten,0)}</td>
        <td class="r">${f.checked?`<span class="bar"><span class="track"><i style="width:${Math.round((sv||0)*100)}%"></i></span><span class="num">${pct(sv,0)}</span></span>`:'<span class="hint">not in tree</span>'}</td></tr>`}).join('')
    +'</tbody></table></div>'+(d.files_total>d.files.length?`<p class="hint">${n(d.files_total-d.files.length,0)} more files omitted.</p>`:'');
}

function eventRows(){
  const rows=Object.entries(counts).sort((a,b)=>b[1]-a[1]);
  const top=rows.length?rows[0][1]:1;
  return `<table class="compact"><tbody>`+rows.map(([name,count])=>`<tr><td class="key">${esc(name)}</td>
    <td class="r num">${n(count,0)}</td><td class="r opt">${track(count,top)}</td></tr>`).join('')+'</tbody></table>';
}

$('#crumb').innerHTML=`<b>${esc(data.project)}</b> / commit / <b>${esc(c.commit.slice(0,10))}</b>`;
$('#chips').innerHTML=[chip(v.integrity_verified?'verified':'unverified',v.integrity_verified?'ok dot':'warn dot'),
  chip('Iteration')].join('');

$('#content').innerHTML=`
<div class="pagehead"><div><h1>${esc(c.title)}</h1>
  <p class="sub">${esc(stamp(c.date))} · parent ${esc(c.parent?c.parent.slice(0,10):'root')} · trace ${esc((c.trace_root||c.proof_hash).slice(0,12))}…</p></div>
  <div class="meta">${chip(n(data.diff?data.diff.files:e.scope.files,0)+' files')}${chip('+'+n(data.diff?data.diff.insertions:null,0)+' / −'+n(data.diff?data.diff.deletions:null,0))}${chip(n(act.sessions,0)+' sessions')}</div>
</div>

<div class="grid g6">
  ${stat('Human steering',n(rates.human,1),'tok/edit',short(tok(m.human))+' prompt tokens · '+n(m.human.messages||0,0)+' turns'+baseNote(base.human,v=>n(v,1)),vsBase(rates.human,base.human),'sp-human')}
  ${stat('Reading burden',n(rates.read,0),'tok/msg',short(tok(m.visible_ai))+' shown of '+short(sum('output_tokens'))+' generated'+baseNote(base.read,v=>n(v,0)),vsBase(rates.read,base.read),'sp-read')}
  ${stat('Unit cost',isNum(rates.cost)?rates.cost.toFixed(4):NA,'AWC/edit',(isNum(awc)?awc.toFixed(2)+' AWC':NA)+' · '+n(modelCalls,0)+' calls'+baseNote(base.cost,v=>v.toFixed(4)),vsBase(rates.cost,base.cost),'sp-cost')}
  ${stat('Retention',pct(rates.keepRate,1),'survived',n(kept,0)+' of '+n(ops,0)+' edits · '+pct(1-rates.keepRate,1)+' rework'+baseNote(base.keepRate,v=>pct(v,0)),vsBase(rates.keepRate,base.keepRate),'sp-keep')}
  ${stat('Throughput',isNum(rates.speed)?n(rates.speed,1):NA,'edits/h',
    isNum(hours)&&hours<1/60?'window under a minute · rate not meaningful':dur(win.span_ms)+' observed window'+baseNote(base.speed,v=>n(v,1)),vsBase(rates.speed,base.speed),'sp-speed')}
  ${stat('Autonomy',n(rates.tools,2),'calls/edit',n(toolCalls,0)+' tool calls · '+n(counts['agent.spawn']||0,0)+' sub-agents'+baseNote(base.tools,v=>n(v,2)),vsBase(rates.tools,base.tools),'sp-tools')}
</div>
<div class="grid g21">
  ${panel('Session timeline','sessions, tasks, rework','<div id="waterfall"></div><div class="legend"><span><i style="background:#39c2b4;opacity:.55"></i>session</span><span><i style="background:#9b8cff"></i>task change</span><span><i style="background:#ff9c31"></i>rework</span></div>')}
  ${panel('Interval','observed',[kv('Window',dur(win.span_ms)),kv('First event',isNum(win.first_ms)?hhmm(win.first_ms):null),
    kv('Last event',isNum(win.last_ms)?hhmm(win.last_ms):null),kv('Sessions',n(act.sessions,0)),
    kv('Wrapped runs',n(act.runs,0)+' started / '+n(act.run_stops,0)+' ended'),kv('Non-zero exits',n(act.nonzero_exits,0)),
    kv('Coverage gaps',n(act.coverage_gaps,0))].join(''))}
</div>
<div class="grid g3">
  ${panel('Human','tokens',[kv('Prompt tokens',short(tok(m.human))),kv('Turns',n(m.human.messages||0,0)),
    kv('Tokens / turn',n(rate(tok(m.human),m.human.messages||0),0)),
    kv('Linked to edits',n(e.human.linked_prompts,0)+' / '+n(e.human.prompts,0)),
    kv('Counting basis',m.human.tokens_complete===false?'partial':m.human.tokens_estimated?'estimated':'tokenizer')].join(''))}
  ${panel('Machine','tokens · AWC',[kv('Reference compute',isNum(awc)?awc.toFixed(2)+' AWC':null),
    kv('Actually paid',m.actual_priced_calls?usd(m.actual_usd_known_subtotal):null),
    kv('Input tokens',short(sum('input_tokens'))),
    kv('Cache read share',isNum(sum('cached_input_tokens'))&&sum('input_tokens')?pct(sum('cached_input_tokens')/sum('input_tokens'),0):null),
    kv('Output tokens',short(sum('output_tokens'))),
    kv('Reasoning share',isNum(sum('reasoning_tokens'))&&sum('output_tokens')?pct(sum('reasoning_tokens')/sum('output_tokens'),0):null),
    kv('Priced calls',n(m.priced_calls,0)+' / '+n((m.priced_calls||0)+(m.unpriced_calls||0),0))].join('')
    +'<p class="note">1 AWC = $1 at the list price recorded with each call, so discounts and credits cannot move it.</p>')}
  ${panel('Agent','observed use',[kv('Tool calls',n(toolCalls,0)),
    kv('Failed results',n(act.failed_tool_calls,0)+(toolCalls?' · '+pct(rate(act.failed_tool_calls,toolCalls),1):'')),
    kv('Distinct tools',n((m.tools_used||[]).length,0)),kv('Skills used',n((m.skills_used||[]).length,0)),
    kv('MCP servers / calls',n((m.mcp_used||[]).length,0)+' / '+n(d.mcp_calls,0)),
    kv('Sub-agents',n(counts['agent.spawn']||0,0)),kv('Max depth',agent?n(agent.max_depth,0):null)].join(''))}
</div>
<div class="grid g2">
  ${panel('Work by model','share of tokens',modelRows(),'flush')}
  ${panel('Tool calls','by name',toolRows(),'flush')}
</div>
<div class="grid g21">
  ${panel('Files','edits and survival',fileRows(),'flush')}
  ${panel('Agent topology','spawn graph',tree())}
</div>
<div class="grid g4">
  ${panel('Artifact','edits',[kv('Semantic edits',n(ops,0)),kv('Surviving',n(kept,0)),kv('Rework',pct(1-rate(kept,ops),1)),
    kv('Files touched',n(d.files_total,0)),kv('Committed units',n(e.scope.units,0)),
    kv('Capture',e.limited?'bounded':'complete')].join(''))}
  ${panel('Tasks','declared',[kv('Declared',n(e.task.declared,0)),kv('Attempts',n(e.task.attempts,0)),
    kv('Completed w/ evidence',n(e.task.completed,0)),kv('Re-opened',e.task.declared?n(e.task.attempts-e.task.declared,0):null),
    kv('Basis',e.task.basis)].join(''))}
  ${panel('Result','from git',[kv('Files added',d.result?n(d.result.added,0):null),kv('Modified',d.result?n(d.result.modified,0):null),
    kv('Removed',d.result?n(d.result.removed,0):null),kv('Test files touched',d.result?n(d.result.tests,0):null),
    kv('Build / tests / lint','not recorded')].join(''))}
  ${panel('Event volume','by type',eventRows(),'flush')}
</div>
<div class="grid g12">
  ${panel('Proof','verification',
    '<div class="checks">'+[['trace chain',v.integrity_verified],['git parent',v.git_tree_verified],['git tree',v.git_tree_verified],
      ['usage records',v.integrity_verified],['artifact trace',v.integrity_verified]]
      .map(([label,ok])=>chip(label,ok?'ok':'')).join('')+'</div>'
    +[kv('Proof hash',c.proof_hash.slice(0,20)+'…'),kv('Events',n(v.events,0)),kv('Boundary',c.boundary),
      kv('Trust',v.trust),kv('Retention score [derived]',s.value+' / 100 · '+s.grade),
      kv('Score evidence',pct(s.confidence,0)+' · '+s.status)].join(''))}
  ${panel('Provenance','how each number was produced',
    `<div class="prov">
      <div><h4 class="obs">Observed</h4><ul><li>Prompt tokens and turns</li><li>AI visible output</li><li>Model calls and token buckets</li><li>Tool, skill and sub-agent events</li><li>File write observations</li><li>Event timestamps</li></ul></div>
      <div><h4 class="der">Derived</h4><ul><li>Reference compute (AWC)</li><li>Semantic edits and survival</li><li>Per-file rework</li><li>Commit contents from git</li><li>Retention score</li></ul></div>
      <div><h4 class="inf">Inferred</h4><ul><li>Prompt-to-edit attribution</li><li>Task churn equivalence</li><li>Intent graph (not implemented)</li></ul></div>
    </div>
    <p class="note">Hashes prove the record is internally consistent and bound to this commit. They cannot prove the recorder saw everything, and nothing here attests that the code builds, passes tests or is correct.</p>`)}
</div>`;
function sparks(){
  for(const [id,key] of [['sp-human','human'],['sp-read','read'],['sp-cost','cost'],
                         ['sp-keep','keepRate'],['sp-speed','speed'],['sp-tools','tools']]){
    const host=document.getElementById(id);
    if(host)spark(trendOf(key),host,'#ff9c31');
  }
}

const commitGrids=[...$('#content').querySelectorAll(':scope > .grid')];
scoreLayout(scoreHero('Iteration score',s.value,'/ 100','The score for this commit, based on recorded process retention.',
  '<h3>Score evidence</h3>'+kv('Retained edits',n(kept,0)+' / '+n(ops,0))+
  kv('Evidence confidence',pct(s.confidence,0))+kv('Score status',s.status)+
  `<div class="score-track"><i style="width:${Math.max(0,Math.min(100,Number(s.value)||0))}%"></i></div><p class="note">${esc(s.algorithm)} · ${esc(s.grade)}. This score does not verify code quality or passing tests.</p>`),[
  ['Efficiency & input','Human input, cost, retention and tool use',[commitGrids[0],commitGrids[2],commitGrids[3]]],
  ['Changes & execution','Files, session timelines and task records',[commitGrids[4],commitGrids[1],commitGrids[5]]],
  ['Verification & provenance','Checks, evidence and measurement methods',[commitGrids[6]]]
]);
function paint(){waterfall();sparks()}
$('#content').addEventListener('toggle',()=>requestAnimationFrame(paint),true);
paint();
let t;addEventListener('resize',()=>{clearTimeout(t);t=setTimeout(paint,150)});
"""

JS_INDEX = HELPERS + r"""
const lt=data.lifetime,total=data.iteration,rows=data.history,stats=data.statistics||{};
const series=[...rows].reverse();
document.title=data.project+' · AI-PoW history';
if(data.demo)document.querySelector('#chips').insertAdjacentHTML('afterbegin',chip('demo data','warn'));
const days=data.span.first&&data.span.last?Math.max(1,Math.round((new Date(data.span.last.date)-new Date(data.span.first.date))/86400000)):null;
const perEdit=f=>series.map(p=>p.retained?f(p)/p.retained:null);
const ratesHuman=perEdit(p=>p.human_tokens||0),ratesCost=perEdit(p=>Number(p.awc||0)),ratesTools=perEdit(p=>p.tool_calls||0);
const survival=series.map(p=>p.operations?p.retained/p.operations:null);
const durations=series.map(p=>isNum(p.span_ms)?p.span_ms/3600000:null);
const cacheShare=series.map(p=>p.input_tokens?p.cached_input_tokens/p.input_tokens:null);
const failShare=series.map(p=>p.tool_calls?p.failed_tool_calls/p.tool_calls:null);
const lastOf=values=>{for(let i=values.length-1;i>=0;i--)if(isNum(values[i]))return values[i];return null};
const prevOf=values=>{let found=0;for(let i=values.length-1;i>=0;i--)if(isNum(values[i])&&++found===2)return values[i];return null};
function delta(values,invert){
  const now=lastOf(values),before=prevOf(values);
  if(!isNum(now)||!isNum(before)||!before)return '';
  const change=(now-before)/Math.abs(before);
  if(Math.abs(change)<.005)return '<span class="delta">flat</span>';
  return `<span class="delta ${change>0?'up':'down'}">${(Math.abs(change)*100).toFixed(0)}% vs prev</span>`;
}
const keptTotal=lt.artifact_retained,opsTotal=lt.artifact_operations;
const perOperation=opsTotal?lt.human_tokens/opsTotal:null;
const awcPerEdit=opsTotal&&isNum(lt.reference_usd_known_subtotal)?Number(lt.reference_usd_known_subtotal)/opsTotal:null;
const rework=isNum(lt.artifact_survival)?1-Number(lt.artifact_survival):null;
const level=(value,high,low)=>!isNum(value)?['medium','—']:value>=high?['high','HIGH']:value<=low?['low','LOW']:['medium','MEDIUM'];
const hu=level(perOperation,40,15),ma=level(awcPerEdit,.05,.02),rw=level(rework,.4,.2);
const pattern=hu[0]==='high'&&ma[0]==='low'?'Human-guided / low-cost AI':hu[0]==='low'&&ma[0]==='high'?'Autonomous / high-compute'
  :hu[1]==='—'||ma[1]==='—'?'Not enough recorded history':'Balanced';

$('#crumb').innerHTML=`<b>${esc(data.project)}</b> / repository history`;
$('#chips').innerHTML=[chip(data.chain_verified?'chain verified':'chain unverified',data.chain_verified?'ok dot':'warn dot'),
  chip('Current version')].join('');

$('#content').innerHTML=`
<div class="pagehead"><div><h1>${esc(data.project)} <span style="color:var(--dim);font-weight:400">/ Current version</span></h1>
  <p class="sub">${rows.length?'Latest commit '+esc(rows[0].commit.slice(0,7))+' · '+esc(dayOf(rows[0].date)):'No recorded iterations yet'}</p></div>
  <div class="meta">${chip(n(lt.sessions,0)+' sessions')}${chip(n(lt.model_calls,0)+' model calls')}${chip(short(lt.artifact_operations)+' edits')}</div>
</div>
<div class="grid g4">
  ${stat('Human steering',n(lastOf(ratesHuman),1),'tok/edit','pooled '+n(perOperation,1)+' · '+short(lt.human_tokens)+' tokens',delta(ratesHuman))}
  ${stat('Unit cost',isNum(lastOf(ratesCost))?lastOf(ratesCost).toFixed(4):NA,'AWC/edit','pooled '+(isNum(awcPerEdit)?awcPerEdit.toFixed(4):NA)+' · '+(lt.priced_calls?Number(lt.reference_usd_known_subtotal).toFixed(2)+' AWC':NA),delta(ratesCost))}
  ${stat('Retention',pct(lastOf(survival),1),'survived','pooled '+pct(lt.artifact_survival,1)+' · rework '+pct(rework,1),delta(survival))}
  ${stat('Autonomy',n(lastOf(ratesTools),2),'calls/edit','pooled '+n(rate(lt.tool_calls,opsTotal),2)+' · '+short(lt.tool_calls)+' calls',delta(ratesTools))}
</div>
<div class="grid g4">
  ${stat('Duration',dur(lastOf(durations)*3600000),'per commit','total '+dur(lt.span_ms),delta(durations))}
  ${stat('Cache reads',pct(lastOf(cacheShare),0),'of input','pooled '+(isNum(lt.cached_input_tokens)&&lt.input_tokens?pct(lt.cached_input_tokens/lt.input_tokens,0):NA),delta(cacheShare))}
  ${stat('Failed tools',pct(lastOf(failShare),1),'of calls','total '+n(lt.failed_tool_calls,0)+' failures',delta(failShare))}
  ${stat('Paid',lt.actual_priced_calls?usd(lt.actual_usd_known_subtotal):NA,'actual','reference '+(lt.priced_calls?Number(lt.reference_usd_known_subtotal).toFixed(2)+' AWC':NA))}
</div>
<div class="grid g2">
  ${panel('Human tokens per surviving edit','oldest → newest','<div id="chart-effort"></div>')}
  ${panel('AWC per surviving edit','oldest → newest','<div id="chart-cost"></div>')}
</div>
<div class="grid g2">
  ${panel('Retention and rework','share of observed edits','<div id="chart-retention"></div><div class="legend"><span><i style="background:#39c2b4"></i>survived</span><span><i style="background:#ff9c31"></i>reworked</span></div>')}
  ${panel('Surviving edits per commit','volume','<div id="chart-volume"></div>')}
</div>
<div class="grid">
  ${panel('Development style','descriptive, not a score',
    `<div class="pattern"><b>${esc(pattern)}</b><span class="hint">Derived from the pooled ratios below.</span></div>
     <div style="margin-top:10px">
     <div class="lvl"><span>Human input</span><span class="tag ${hu[0]}">${hu[1]}</span><span class="why">${isNum(perOperation)?n(perOperation,1)+' prompt tokens per semantic edit · high ≥ 40, low ≤ 15':'not recorded'}</span></div>
     <div class="lvl"><span>Machine compute</span><span class="tag ${ma[0]}">${ma[1]}</span><span class="why">${isNum(awcPerEdit)?awcPerEdit.toFixed(4)+' AWC per semantic edit · high ≥ 0.05, low ≤ 0.02':'not recorded'}</span></div>
     <div class="lvl"><span>Rework</span><span class="tag ${rw[0]}">${rw[1]}</span><span class="why">${isNum(rework)?pct(rework,0)+' of observed edits did not survive · high ≥ 40%, low ≤ 20%':'not recorded'}</span></div>
     </div>`)}
</div>
<div class="grid">
  ${panel('Commits','newest first · open one for its proof',
    `<div class="scroll"><table class="compact"><thead><tr><th>Date</th><th>Commit</th><th>Subject</th>
      <th class="r opt">Duration</th><th class="r opt">AWC</th><th class="r">Edits</th><th class="r">Survival</th><th class="r opt">Score</th></tr></thead><tbody>`
    +rows.map(p=>{const sv=p.operations?p.retained/p.operations:null;
      return `<tr><td>${esc(dayOf(p.date))}</td>
        <td><a href="${esc((data.links&&data.links.commit)||'reports/')}${esc(p.commit)}.html">${esc(p.commit.slice(0,7))}</a></td>
        <td class="key">${esc(p.title)}</td><td class="r opt">${dur(p.span_ms)}</td>
        <td class="r opt num">${isNum(p.awc)?Number(p.awc).toFixed(2):NA}</td><td class="r num">${n(p.retained,0)}</td>
        <td class="r"><span class="bar"><span class="track"><i style="width:${Math.round((sv||0)*100)}%"></i></span><span class="num">${pct(sv,0)}</span></span></td>
        <td class="r opt num">${p.score?esc(p.score.value):NA}</td></tr>`}).join('')
    +'</tbody></table></div>','flush')}
</div>
<div class="grid g3">
  ${panel('Pooled machine','totals',[kv('Reference compute',lt.priced_calls?Number(lt.reference_usd_known_subtotal).toFixed(2)+' AWC':null),
    kv('Actually paid',lt.actual_priced_calls?usd(lt.actual_usd_known_subtotal):null),
    kv('Model tokens',short(isNum(lt.input_tokens)&&isNum(lt.output_tokens)?lt.input_tokens+lt.output_tokens:null)),
    kv('Reasoning tokens',short(lt.reasoning_tokens)),kv('Model calls',n(lt.model_calls,0))].join(''))}
  ${panel('Pooled agent','totals',[kv('Tool calls',n(lt.tool_calls,0)),kv('Failed results',n(lt.failed_tool_calls,0)),
    kv('Sub-agents',n(lt.sub_agents,0)),kv('Skills / MCP',n(lt.skills_used,0)+' / '+n(lt.mcp_used,0)),
    kv('Wrapped runs',n(lt.runs,0)),kv('Coverage gaps',n(lt.coverage_gaps,0))].join(''))}
  ${panel('Pooled artifact','totals',[kv('Semantic edits',n(lt.artifact_operations,0)),kv('Surviving',n(lt.artifact_retained,0)),
    kv('Survival',pct(lt.artifact_survival,1)),kv('Rework',pct(rework,1)),
    kv('Task fulfillment',lt.task_attempts?pct(lt.task_fulfillment,1):null),
    kv('Commits scored',n(lt.scored_commits,0)+' / '+n(lt.commits,0))].join(''))}
</div>
${total?`<div class="grid">
  ${panel('Cumulative score','derived · '+total.algorithm,
    `<div class="big-total"><b class="num">${n(total.value,1)}</b>
      <span class="hint">${n(total.previous,1)} + ${n(total.delta,1)} = ${n(total.value,1)} · ${n(total.rated_commits,0)} scored commits · started at 0</span></div>
     <p class="note">Each commit appends one row to the iteration chain: <b>T = T-1 + this commit</b>, hash-linked to the row before it. Every score is added once and unchanged, so the total grows with recorded work, not with quality. Average commit score ${n(stats.average,1)}. Chain length ${n(data.chain_length,0)} rows${data.chain_verified?', linkage checked':', linkage NOT checked'}.</p>`)}
</div>`:''}`;

function line(host,sets,opts){
  const el=$(host);if(!el)return;
  const w=el.clientWidth||600,h=(opts&&opts.height)||150,left=34,right=8,top=10,bottom=20;
  const all=sets.flatMap(s=>s.values).filter(isNum);
  if(!all.length)return el.innerHTML='<p class="hint">not recorded</p>';
  const hi=(opts&&opts.max)||Math.max(...all)*1.1||1,lo=0,count=sets[0].values.length;
  const x=i=>left+(w-left-right)*i/Math.max(1,count-1),y=v=>top+(h-top-bottom)*(1-(v-lo)/(hi-lo||1));
  let svg=`<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(opts&&opts.label||'')}">`;
  for(let i=0;i<=2;i++){const v=hi*i/2;
    svg+=`<line x1="${left}" x2="${w-right}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" stroke="#1a222d"/>`
      +`<text class="axis" x="0" y="${(y(v)+3).toFixed(1)}">${esc(opts&&opts.fmt?opts.fmt(v):n(v,1))}</text>`}
  for(const set of sets){
    let d='';set.values.forEach((v,i)=>{if(!isNum(v))return;d+=(d?' L':'M')+x(i).toFixed(1)+' '+y(v).toFixed(1)});
    svg+=`<path d="${d}" fill="none" stroke="${set.color}" stroke-width="1.8"/>`;
    set.values.forEach((v,i)=>{if(!isNum(v))return;svg+=`<circle cx="${x(i).toFixed(1)}" cy="${y(v).toFixed(1)}" r="${i===count-1?3.5:2}" fill="${set.color}"><title>${esc(set.name+' · '+n(v,3))}</title></circle>`})}
  svg+=`<text class="axis" x="${left}" y="${h-4}">${esc(series.length?dayOf(series[0].date):'')}</text>`
    +`<text class="axis" x="${w-right}" y="${h-4}" text-anchor="end">latest</text>`;
  el.innerHTML=svg+'</svg>';
}
function bars(host,values,labels){
  const el=$(host);if(!el)return;
  const w=el.clientWidth||600,h=150,left=34,right=8,top=10,bottom=20;
  const hi=Math.max(...values.filter(isNum),1);
  const slot=(w-left-right)/Math.max(1,values.length),width=Math.max(3,Math.min(26,slot*.62));
  let svg=`<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="Surviving edits per commit">`;
  for(let i=0;i<=2;i++){const v=hi*i/2,y=top+(h-top-bottom)*(1-v/hi);
    svg+=`<line x1="${left}" x2="${w-right}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}" stroke="#1a222d"/><text class="axis" x="0" y="${(y+3).toFixed(1)}">${short(v)}</text>`}
  values.forEach((v,i)=>{if(!isNum(v))return;const bh=(h-top-bottom)*v/hi,x=left+slot*i+(slot-width)/2;
    svg+=`<rect x="${x.toFixed(1)}" y="${(h-bottom-bh).toFixed(1)}" width="${width.toFixed(1)}" height="${Math.max(1,bh).toFixed(1)}" fill="${i===values.length-1?'#ff9c31':'#39c2b4'}" opacity="${i===values.length-1?1:.65}"><title>${esc(labels[i])}</title></rect>`});
  el.innerHTML=svg+'</svg>';
}
function draw(){
  line('#chart-effort',[{name:'human tokens / edit',values:ratesHuman,color:'#ff9c31'}],
    {label:'Human tokens per surviving edit',fmt:v=>short(v)});
  line('#chart-cost',[{name:'AWC / edit',values:ratesCost,color:'#39c2b4'}],
    {label:'AWC per surviving edit',fmt:v=>v>=1?v.toFixed(1):v.toFixed(3)});
  line('#chart-retention',[{name:'survived',values:survival.map(v=>isNum(v)?v*100:null),color:'#39c2b4'},
    {name:'reworked',values:survival.map(v=>isNum(v)?(1-v)*100:null),color:'#ff9c31'}],{max:100,fmt:v=>Math.round(v)+'%',label:'Retention and rework'});
  bars('#chart-volume',series.map(p=>p.retained),series.map(p=>p.commit.slice(0,7)+' · '+n(p.retained,0)+' surviving edits'));
}

const indexGrids=[...$('#content').querySelectorAll(':scope > .grid')];
const historyView=`<div class="section-heading"><h2>Iteration history</h2><p>Newest first · Select a change to view its score</p></div><section class="iterations"><div class="scroll"><table><thead><tr><th>Version / change</th><th class="r">Iteration score</th><th class="r opt">Running total</th></tr></thead><tbody>${rows.map(p=>`<tr><td><a class="iteration-title" href="${esc((data.links&&data.links.commit)||'reports/')}${esc(p.commit)}.html">${esc(p.title)}</a><span class="hint">${esc(p.commit.slice(0,7))} · ${esc(dayOf(p.date))}</span></td><td class="r iteration-score num">${n(p.score?p.score.value:null,1)}</td><td class="r opt num">${n(p.total,1)}</td></tr>`).join('')||'<tr><td colspan="3">No iterations recorded</td></tr>'}</tbody></table></div></section>`;
scoreLayout(scoreHero('Total score',total?total.value:null,'','The cumulative score of all rated iterations through the current version.',
  '<h3>How the total changed</h3>'+kv('Previous total',n(total?total.previous:null,1))+
  kv('This iteration adds',isNum(total&&total.delta)?'+'+n(total.delta,1):NA)+kv('Rated iterations',n(total?total.rated_commits:lt.scored_commits,0))+
  '<p class="note">Total = previous total + this iteration score. The total grows with recorded work; it does not measure code quality.</p>'),[
  ['Efficiency & trends','Human input, cost, retention and execution',indexGrids.slice(0,4)],
  ['Usage & development style','Models, tools, artifacts and development patterns',[indexGrids[4],indexGrids[6]]],
  ['Scoring & accumulation','Algorithms, accumulation rules and chain verification',indexGrids.slice(7)]
],historyView);
$('#content').addEventListener('toggle',()=>requestAnimationFrame(draw),true);
draw();
let timer;addEventListener('resize',()=>{clearTimeout(timer);timer=setTimeout(draw,150)});
"""

FAVICON = ("data:image/svg+xml,%3Csvg xmlns=&#39;http://www.w3.org/2000/svg&#39; viewBox=&#39;0 0 32 32&#39;%3E"
           "%3Crect width=&#39;32&#39; height=&#39;32&#39; fill=&#39;%230b0e13&#39;/%3E%3Cg transform=&#39;skewX(-16)&#39;%3E"
           "%3Crect x=&#39;12&#39; y=&#39;5&#39; width=&#39;7.2&#39; height=&#39;21.6&#39; fill=&#39;%23ff9c31&#39;/%3E"
           "%3Crect x=&#39;22.4&#39; y=&#39;11.4&#39; width=&#39;7.2&#39; height=&#39;15.2&#39; fill=&#39;%23ffffff&#39;/%3E%3C/g%3E%3C/svg%3E")


def _text(value):
    return (str(value or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;")[:120])


def _page(data, script, title, action):
    payload = json.dumps(data, ensure_ascii=True, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    script = script + r"""
let printCollapsed=[];
addEventListener('beforeprint',()=>{printCollapsed=[...document.querySelectorAll('details:not([open])')];printCollapsed.forEach(el=>el.open=true);dispatchEvent(new Event('resize'));});
addEventListener('afterprint',()=>{printCollapsed.forEach(el=>el.open=false);printCollapsed=[];});
document.getElementById('print').addEventListener('click',()=>window.print());
"""
    script_hash = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            + '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'sha256-' + script_hash
            + '\'; style-src \'unsafe-inline\'; img-src data:; base-uri \'none\'; form-action \'none\'">'
            + '<link rel="icon" href="' + FAVICON + '">'
            + "<title>" + title + "</title><style>" + CSS + "</style></head><body>"
            + '<a class="skip" href="#content">Skip to content</a>'
            + '<div class="topbar"><div class="shell"><span class="brand"><span class="mark" aria-hidden="true"><i></i><i></i></span>AI-PoW</span>'
            + '<span class="crumb" id="crumb"></span><span class="spacer"></span>'
            + '<span class="crumb" id="chips"></span>' + action
            + '<button class="btn" id="print">Print</button></div></div>'
            + '<main class="shell" id="content"></main>'
            + '<noscript>This page requires JavaScript to render its embedded data. Use the aipow CLI for the JSON form.</noscript>'
            + '<div class="shell"><div class="foot"><span>AI-PoW · ' + _text(data.get("project")) + '</span>'
            + '<span>Process record · not a measure of code quality</span></div></div>'
            + '<script id="report-data" type="application/json">' + payload + '</script><script>' + script
            + "</script></body></html>")


def render(data):
    """The per-commit run view."""
    link = (data.get("links") or {}).get("index")
    action = '<a class="btn" href="' + link + '">← Current version</a>' if link else ""
    return _page(data, JS_COMMIT, "AI-PoW · Commit run", action)


def render_index(data):
    """The repository dashboard."""
    return _page(data, JS_INDEX, "AI-PoW · Repository history", "")
