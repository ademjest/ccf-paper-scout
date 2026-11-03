"""Self-contained local annotation page; no CDN, telemetry, or model scores."""
from __future__ import annotations

import json


def render(manifest: dict) -> str:
    public = {"dataset_id": manifest["dataset_id"], "tasks": manifest["tasks"]}
    payload = json.dumps(public, ensure_ascii=False).replace("<", "\\u003c")
    return PAGE.replace("__DATASET__", payload)


PAGE = r'''<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paper Scout · 论文相关性标注</title>
<style>
body{font-family:system-ui,"Microsoft YaHei",sans-serif;background:#f4f6f8;color:#1b2937;margin:0;line-height:1.7}
main{max-width:940px;margin:28px auto;padding:0 20px}h1{font-size:26px}h2{line-height:1.4;font-size:23px}
.panel{background:white;border:1px solid #dce3e9;border-radius:12px;padding:24px;margin:18px 0}
button,.file{background:#174d68;color:white;border:0;border-radius:6px;padding:10px 16px;cursor:pointer;font:inherit}
button:disabled{opacity:.45;cursor:default}select,textarea,input{font:inherit}textarea{box-sizing:border-box;width:100%;min-height:80px;padding:10px;border:1px solid #abbac6;border-radius:6px}
select{padding:8px;max-width:100%}label{display:block;font-weight:600;margin-top:16px}.muted{color:#586975;font-size:14px}
#abstract{white-space:pre-wrap;user-select:text}.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}a{color:#14628b}
#status{font-weight:600;color:#805000}summary{cursor:pointer;font-weight:600}progress{width:220px}.links a{margin-right:18px}
</style><main>
<h1>论文相关性标注</h1>
<p>阅读标题和原文摘要，判断它是否值得你为当前研究方向花时间阅读。无需猜测系统为何推荐。</p>
<details class="panel" open><summary>开始前：统一判断标准</summary>
<p>本批画像：强化学习（RL）、多智能体强化学习（MARL）、LLM 辅助外部 RL。是否接受某个应用场景，由你结合自己的研究需求判断。</p>
<ul><li><b>2 高度相关：</b>直接研究你关心的方法、问题或作用关系。</li>
<li><b>1 部分相关：</b>有可借鉴的方法，但任务或方向不完全一致。</li>
<li><b>0 不相关：</b>不符合你的当前研究需求；不是“论文质量差”。</li>
<li><b>待确认：</b>摘要证据不足或暂时不清楚。不要为了完成进度强行选 0。</li></ul>
<p>多智能体协作不一定是 MARL；用 RL 训练语言模型不一定是 LLM 帮助外部 RL。两者可在“研究角色”中区分。</p>
<p>每个确定判断请复制一段支持判断的<b>原文摘要</b>并写简短理由。全文链接可以辅助阅读，但本轮证据栏仅验证摘要原句。分数、排序和校准／测试划分均不显示。</p>
</details>
<div class="row"><button id="export">导出标注 JSON</button><label class="file" style="margin:0">导入继续<input type="file" id="import" accept=".json" hidden></label><span id="status" role="status"></span></div>
<p class="muted">浏览器本地保存仅作便利缓存；请定期导出 JSON。页面无需联网，没有提交或发送按钮。</p>
<div class="panel"><div class="row"><button id="prev">上一篇</button><span id="position"></span><button id="next">下一篇</button><progress id="progress"></progress><span id="done"></span></div>
<h2 id="title"></h2><p class="muted" id="meta"></p><p class="links" id="links"></p><p id="abstract"></p>
<label for="grade">相关程度</label><select id="grade"><option value="">未标注</option><option value="unknown">待确认</option><option value="2">2 · 高度相关</option><option value="1">1 · 部分相关</option><option value="0">0 · 不相关</option></select>
<label for="role">研究角色（按摘要判断）</label><select id="role"><option value="">未选择</option><option>通用 RL</option><option>MARL</option><option>LLM → 外部 RL</option><option>RL → LLM</option><option>其他 Agent</option><option>其他／不确定</option></select>
<label for="evidence">摘要证据原句</label><textarea id="evidence" placeholder="从上方英文摘要复制支持判断的原句"></textarea>
<label for="reason">你的判断理由</label><textarea id="reason" placeholder="例如：研究多智能体信用分配，与我的目标直接相关；或只有多智能体讨论，没有强化学习"></textarea>
</div><p class="muted">导出的文件交给助手进行校准集评估。方案固定后再开启独立测试集评估。</p>
</main><script id="dataset" type="application/json">__DATASET__</script>
<script>
const data=JSON.parse(document.getElementById('dataset').textContent), tasks=data.tasks;
const key='paper-scout-labels:'+data.dataset_id, ids=new Set(tasks.map(p=>p.id));
let index=0, labels={};
const el=id=>document.getElementById(id);
function status(message){el('status').textContent=message;}
function validate(payload){
 if(payload.dataset_id!==data.dataset_id || !Array.isArray(payload.labels)) throw Error('文件属于其他数据集或格式错误');
 const seen=new Set();
 for(const row of payload.labels){
  if(!ids.has(row.id)||seen.has(row.id)) throw Error('含未知或重复论文');
  if(row.grade!==null && ![0,1,2].includes(row.grade)) throw Error('相关程度必须为 0/1/2 或 null');
  for(const field of ['role','evidence','reason']) if(typeof row[field]!=='string') throw Error('文本字段格式错误');
  seen.add(row.id);
 }
 return Object.fromEntries(payload.labels.map(row=>[row.id,row]));
}
try{const saved=localStorage.getItem(key);if(saved) labels=validate(JSON.parse(saved));}catch(e){status('本地缓存不可用；请使用导入／导出备份。');}
function payload(){return {dataset_id:data.dataset_id,labels:tasks.map(p=>labels[p.id]||{id:p.id,grade:null,role:'',evidence:'',reason:''})};}
function complete(p){const r=labels[p.id];return r && r.grade!==null && r.reason.trim() && r.evidence.trim() && p.abstract.includes(r.evidence.trim());}
function save(){
 const p=tasks[index],value=el('grade').value;
 labels[p.id]={id:p.id,grade:['0','1','2'].includes(value)?Number(value):null,pending:value==='unknown',role:el('role').value,evidence:el('evidence').value,reason:el('reason').value};
 try{localStorage.setItem(key,JSON.stringify(payload()));status('已保存到本浏览器');}catch(e){status('浏览器无法保存，请立即导出备份');}
 if(labels[p.id].grade!==null && !complete(p)) status('请补充理由和可在摘要中找到的原句');
 progress();
}
function progress(){const count=tasks.filter(complete).length;el('progress').max=tasks.length;el('progress').value=count;el('done').textContent=`有效判断 ${count}/${tasks.length}`;}
function show(){
 if(!tasks.length){status('本数据集没有可标注论文');el('prev').disabled=el('next').disabled=true;return;}
 const p=tasks[index],row=labels[p.id]||{};
 el('position').textContent=`${index+1} / ${tasks.length}`;el('title').textContent=p.title;
 el('meta').textContent=[p.venue,p.year,Array.isArray(p.authors)?p.authors.join(', '):p.authors].join(' · ');
 el('abstract').textContent=p.abstract;el('links').replaceChildren();
 for(const [name,url] of [['论文记录',p.url],['原文入口',p.ee],['摘要来源',p.abstract_source_url]]){
  if(!/^https?:\/\//i.test(url||'')) continue;
  const a=document.createElement('a');a.textContent=name;a.href=url;a.target='_blank';a.rel='noopener noreferrer';el('links').append(a);
 }
 el('grade').value=row.grade===0||row.grade===1||row.grade===2?String(row.grade):row.pending?'unknown':'';
 for(const field of ['role','evidence','reason']) el(field).value=row[field]||'';
 el('prev').disabled=index===0;el('next').disabled=index===tasks.length-1;progress();
}
for(const field of ['grade','role','evidence','reason']) el(field).addEventListener('input',save);
el('prev').onclick=()=>{index--;show();};el('next').onclick=()=>{index++;show();};
el('export').onclick=()=>{const blob=new Blob([JSON.stringify(payload(),null,2)],{type:'application/json'});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='paper-labels-'+data.dataset_id.slice(0,8)+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);status('已导出，请保留下载的 JSON 文件');};
el('import').onchange=async event=>{try{const file=event.target.files[0];if(!file)return;const imported=validate(JSON.parse(await file.text()));labels={...labels,...imported};try{localStorage.setItem(key,JSON.stringify(payload()));}catch(e){}show();status('已导入；相同论文以导入文件为准');}catch(e){status('导入失败：'+e.message);}finally{event.target.value='';}};
show();
</script></html>'''
