'use strict';
const $=s=>document.querySelector(s), root='/pylon/market/api/';
let state, tab='sale', dirty=false, busy=false, pending=Promise.resolve(), previewResult, facetKey;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const clone=x=>JSON.parse(JSON.stringify(x));
const list=s=>s.split(/[、,，;；\n]/).map(x=>x.trim()).filter(Boolean);
function notice(text,error=false){$('#notice').textContent=text;$('#notice').classList.toggle('error',error);}
async function api(action,data){
  const response=await fetch(root+action,{method:data===undefined?'GET':'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':state?.csrf||''},body:data===undefined?undefined:JSON.stringify(data)});
  const value=await response.json();if(!response.ok)throw Error(value.error||'请求未完成');return value;
}
function kind(key){for(const group of Object.values(state.hierarchy))for(const [type,v] of Object.entries(group.types))if(v.categories.includes(key))return type;return key;}
function selected(key,sale=state.draft.content.sale){const group=key.split('.')[0];return !(sale.blocked_groups||[]).includes(group)&&!(sale.blocked_types||[]).includes(kind(key))&&!(sale.blocked_categories||[]).includes(key)&&((sale.partial_types||[]).includes(kind(key))?(sale.categories||[]).includes(key):((sale.groups||[]).includes(group)||(sale.types||[]).includes(kind(key))||(sale.categories||[]).includes(key)));}
function replaceSet(object,key,value,on){const set=new Set(object[key]||[]);on?set.add(value):set.delete(value);object[key]=[...set];}
function changed(){dirty=true;$('#save-state').textContent='正在保存草稿…';summary();pending=pending.then(save).catch(e=>notice(e.message,true));}
async function save(){
  if(!dirty)return;dirty=false;const d=state.draft, sent=clone(d.content);
  try{const next=await api('draft',{id:d.id,revision:d.revision,content:sent});
    next.draft.content=state.draft.content;state=next;$('#save-state').textContent=dirty?'正在保存草稿…':'草稿已保存';
  }catch(error){dirty=true;$('#save-state').textContent='草稿未保存';throw error;}
}
async function guarded(fn){if(busy)return;busy=true;try{await pending;if(dirty)await save();await fn();}catch(e){notice(e.message,true);}finally{busy=false;}}
function summary(){
  const s=state.draft.content.sale, rows=[];
  for(const [g,label]of Object.entries(state.groups)){
    if((s.blocked_groups||[]).includes(g)){rows.push(`<p>${esc(label)}：整类屏蔽</p>`);continue;}
    const keys=Object.keys(state.catalog).filter(k=>k.startsWith(g+'.')&&selected(k));
    if(keys.length){const exclusions=Object.keys(state.catalog).filter(k=>k.startsWith(g+'.')&&!selected(k));rows.push(`<p><strong>${esc(label)}</strong>：${(s.groups||[]).includes(g)?'全部现有及未来子类'+(exclusions.length?'，但排除 '+exclusions.map(k=>esc(state.catalog[k].split('/').pop())).join('、'):''):keys.map(k=>esc(state.catalog[k].split('/').pop())).join('、')}</p>`);}
  }
  for(const [key,rules]of Object.entries(s.filters||{}))for(const [f,r]of Object.entries(rules)){
    const title=state.catalog[key]||state.groups[key]||(key==='*'?'全部类别':key), bits=[];
    if(r.include?.length)bits.push('只看 '+r.include.join('、'));if(r.exclude?.length)bits.push('排除 '+r.exclude.join('、'));
    if(r.min!==undefined||r.max!==undefined)bits.push(`${r.min??'不限'} ～ ${r.max??'不限'}`);
    if(bits.length)rows.push(`<p>${esc(title)} · ${esc(state.fields[f])}<br>${esc(bits.join('；'))}</p>`);
  }
  $('#summary').innerHTML=rows.join('')||'<p class="muted">尚未关注出售商品</p>';
  $('#version').textContent=`当前版本 ${state.active.version}；修改先保存到草稿，确认后生效。`;
}
function render(){
  $('#login').hidden=true;$('#workspace').hidden=false;$('#logout').hidden=false;
  document.querySelectorAll('nav button').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.tab===tab)));
  if(tab==='sale')renderSale();if(tab==='wanted')renderWanted();if(tab==='review')renderReviews();if(tab==='check')renderCheck();summary();
}
function renderSale(){
  const sale=state.draft.content.sale;
  $('#content').innerHTML='<h2>出售与拼车</h2><input class="search" id="category-search" type="search" placeholder="查找类别，例如：手机、耳机、会员" aria-label="查找类别"><button id="global-filter" class="quiet">全部类别的排除词与条件</button>'+Object.entries(state.groups).map(([group,label])=>{
    const blocked=(sale.blocked_groups||[]).includes(group),mode=blocked?'block':(sale.groups||[]).includes(group)?'all':'partial';
    const types=state.hierarchy[group]?.types||{};
    const leaves=Object.entries(types).map(([type,value])=>`<details class="type"><summary>${esc(value.label)} <span>${value.categories.filter(k=>selected(k)).length}/${value.categories.length}</span></summary><div class="mode"><button data-type="${type}" data-on="yes" ${blocked?'disabled':''}>全选此小类</button><button data-type="${type}" data-on="no" ${blocked?'disabled':''}>屏蔽此小类</button><button data-type="${type}" data-on="partial" ${blocked?'disabled':''}>只看勾选项</button></div>${value.categories.map(k=>`<div class="leaf"><label><input type="checkbox" data-category="${k}" ${selected(k)?'checked':''} ${blocked||(sale.blocked_types||[]).includes(type)?'disabled':''}>${esc(state.catalog[k].split('/').pop())}</label><button data-filter="${k}">品牌／型号／条件</button></div>`).join('')}</details>`).join('');
    return `<details class="group ${blocked?'off':''}" data-group="${group}"><summary>${esc(label)}<span>${mode==='block'?'整类屏蔽':mode==='all'?'全部关注':'展开细选'}</span></summary><div class="group-body"><div class="mode">${[['all','全部关注'],['block','全部屏蔽'],['partial','只看勾选项']].map(([v,l])=>`<button class="${mode===v?'selected':''}" data-mode="${v}" data-group="${group}">${l}</button>`).join('')}<button data-filter="${group}">大类条件</button></div><div class="leaves">${leaves}</div></div></details>`;
  }).join('');
  const prices=state.draft.content.ai_prices?.rules||[];
  if(prices.length){const delivery={recharge:'代充',account:'账号',seat:'席位',shared:'拼车',codex:'Codex',api:'API'};
    $('#content').insertAdjacentHTML('beforeend',`<details class="group"><summary>AI 交易价格条件 <span>${prices.length} 条，四个平台共用</span></summary>${prices.map(r=>`<p>${esc(r.match.product)} ${esc(r.match.tier)} · ${esc(delivery[r.match.delivery]||r.match.delivery)}<br><span class="muted">总支出最多 ${esc(r.total_max)} ${esc(r.match.currency)}${r.monthly_max?'；每月最多 '+esc(r.monthly_max):''}${r.minimum_days?'；至少 '+esc(r.minimum_days)+' 天':''}${r.shared_price_mode==='equal_share'?'；按确认的人数平摊整车价格':''}</span></p>`).join('')}</details>`);
  }
  $('#category-search').oninput=e=>{const q=e.target.value.trim().toLowerCase();document.querySelectorAll('details.group').forEach(d=>{d.hidden=!d.textContent.toLowerCase().includes(q);if(q&&!d.hidden)d.open=true;});};
  $('#global-filter').onclick=()=>openFacets('*');
  $('#content').onclick=e=>{const b=e.target.closest('button');if(!b)return;
    if(b.dataset.filter){openFacets(b.dataset.filter);return;}
    if(b.dataset.mode){const g=b.dataset.group,mode=b.dataset.mode;
      if(mode==='partial'&&(sale.groups||[]).includes(g))for(const k of Object.keys(state.catalog).filter(k=>k.startsWith(g+'.')&&selected(k)))replaceSet(sale,'categories',k,true);
      replaceSet(sale,'groups',g,mode==='all');replaceSet(sale,'blocked_groups',g,mode==='block');
      if(mode==='all'){sale.blocked_categories=(sale.blocked_categories||[]).filter(k=>!k.startsWith(g+'.'));sale.blocked_types=(sale.blocked_types||[]).filter(k=>!k.startsWith(g+'.'));sale.partial_types=(sale.partial_types||[]).filter(k=>!k.startsWith(g+'.'));}
      const opened=[...document.querySelectorAll('details[open]')].map(d=>d.dataset.group).filter(Boolean);changed();renderSale();opened.forEach(g=>{$(`[data-group="${g}"].group`).open=true;});
    }
    if(b.dataset.type){const type=b.dataset.type,on=b.dataset.on,keys=state.hierarchy[type.split('.')[0]].types[type].categories;if(on==='partial')for(const k of keys)replaceSet(sale,'categories',k,selected(k));else for(const k of keys){replaceSet(sale,'categories',k,on==='yes');replaceSet(sale,'blocked_categories',k,on==='no');}replaceSet(sale,'types',type,on==='yes');replaceSet(sale,'blocked_types',type,on==='no');replaceSet(sale,'partial_types',type,on==='partial');changed();const open=b.closest('details.group').dataset.group;renderSale();$(`[data-group="${open}"].group`).open=true;}
  };
  $('#content').onchange=e=>{if(e.target.dataset.category){replaceSet(sale,'categories',e.target.dataset.category,e.target.checked);replaceSet(sale,'blocked_categories',e.target.dataset.category,!e.target.checked);changed();}};
}
async function openFacets(key){
  facetKey=key;const scope=tab==='wanted'?'wanted':'sale';const rules=state.draft.content[scope].filters?.[key]||{};
  $('#facet-title').textContent=(state.catalog[key]||state.groups[key]||'全部类别')+' · 细化筛选';
  const available=state.applicable[key]||Object.keys(state.fields);const fields=Object.entries(state.fields).filter(([f])=>available.includes(f));
  $('#facets').innerHTML=fields.map(([f,label])=>{const r=rules[f]||{},numeric=['price','ram_gb','storage_gb'].includes(f);
    return `<section class="field" data-field="${f}"><h3>${esc(label)}</h3><div class="field-grid">${numeric?`<label>最低<input type="number" min="0" data-rule="min" value="${r.min??''}"></label><label>最高<input type="number" min="0" data-rule="max" value="${r.max??''}"></label>`:`<label>只看这些（留空为不限）<input data-rule="include" value="${esc((r.include||[]).join('、'))}" placeholder="可输入多个，用顿号分隔"></label><label>排除这些<input data-rule="exclude" value="${esc((r.exclude||[]).join('、'))}" placeholder="例如：Apple Music"></label>`}</div>${!numeric&&state.catalog[key]?`<button class="quiet" data-load="${f}">从历史商品中多选</button><div class="choices"></div>`:''}</section>`;
  }).join('');
  $('#facets').onclick=async e=>{const field=e.target.dataset.load;if(!field)return;try{const value=await api('options?category='+encodeURIComponent(key)+'&field='+field);const [options,total,known,samples]=value.options;const dest=e.target.nextElementSibling;
    dest.innerHTML=`<p class="muted">${total} 件中 ${known} 件有明确属性。</p><label>点选用途<select class="choice-mode"><option value="exclude">排除所选</option><option value="include">只看所选</option></select></label><input type="search" class="option-search" placeholder="搜索这些选项"><div class="chips">${options.map(o=>`<button data-value="${esc(o)}">${esc(o)} <span>${value.counts[o]}</span></button>`).join('')}</div>${samples.map((u,i)=>`<a href="${esc(u)}" target="_blank" rel="noreferrer">样本 ${i+1}</a> `).join('')}`;
    dest.querySelector('.choice-mode').onchange=()=>{const mode=dest.querySelector('.choice-mode').value,vals=list(dest.closest('.field').querySelector('[data-rule='+mode+']').value);dest.querySelectorAll('[data-value]').forEach(b=>b.classList.toggle('selected',vals.includes(b.dataset.value)));};dest.querySelector('.choice-mode').onchange();
    dest.querySelector('.option-search').oninput=e=>dest.querySelectorAll('[data-value]').forEach(b=>b.hidden=!b.dataset.value.toLowerCase().includes(e.target.value.toLowerCase()));
    dest.querySelectorAll('[data-value]').forEach(b=>b.onclick=()=>{const mode=dest.querySelector('.choice-mode').value,input=b.closest('.field').querySelector('[data-rule='+mode+']'),vals=new Set(list(input.value));vals.has(b.dataset.value)?vals.delete(b.dataset.value):vals.add(b.dataset.value);input.value=[...vals].join('、');b.classList.toggle('selected',vals.has(b.dataset.value));});
  }catch(error){notice(error.message,true);}};
  $('#facet-done').onclick=()=>{const result={};$('#facets').querySelectorAll('.field').forEach(section=>{const r={};section.querySelectorAll('[data-rule]').forEach(input=>{if(input.value.trim())r[input.dataset.rule]=['min','max'].includes(input.dataset.rule)?Number(input.value):list(input.value);});if(Object.keys(r).length)result[section.dataset.field]=r;});state.draft.content[scope].filters||={};state.draft.content[scope].filters[key]=result;changed();$('#facet-dialog').close();};
  $('#facet-dialog').showModal();
}
function renderWanted(){
  const targets=state.draft.content.wanted.targets||[];
  $('#content').innerHTML='<h2>只看你能满足的求购</h2><p class="muted">与出售关注分别设置；型号、规格和使用地区需要对应。</p>'+targets.map((t,i)=>`<div class="review"><strong>${esc(t.model)}</strong><p>${esc(t.region||'不限地区')} ${esc(Object.entries(t.specs||{}).map(([k,v])=>k+': '+v).join('、'))}</p><button data-remove="${i}">移除此目标</button><button data-filter="${t.category}">附加条件</button></div>`).join('')+'<div class="row"><input id="wanted-new" placeholder="例如：北京 京东家政 2小时"><button id="wanted-add">加入草稿</button></div>';
  $('#content').onclick=e=>{if(e.target.dataset.remove!==undefined){targets.splice(Number(e.target.dataset.remove),1);changed();renderWanted();}if(e.target.dataset.filter)openFacets(e.target.dataset.filter);};
  $('#wanted-add').onclick=()=>guarded(async()=>{state=await api('wanted-add',{id:state.draft.id,revision:state.draft.revision,text:$('#wanted-new').value});render();});
}
async function renderReviews(){
  $('#content').innerHTML='<h2>待核对与新品种</h2><p>正在读取…</p>';
  try{const data=await api('reviews');if(tab!=='review')return;
    $('#content').innerHTML='<h2>新品种待确认</h2>'+(data.novelty.length?'<div class="mode"><button id="review-all">全选新品种</button><select id="review-mode"><option value="map">归入现有类别</option><option value="follow">新增细类并关注</option><option value="classify">归类但不关注</option><option value="ignored">忽略此品种</option><option value="later">稍后</option></select><select id="review-target">'+Object.entries(state.catalog).map(([k,v])=>`<option value="${k}">${esc(v)}</option>`).join('')+'</select><button id="review-choice">加入草稿</button></div>'+data.novelty.map(r=>`<div class="review"><label><input type="checkbox" data-novel="${r.id}"> <strong>${esc(r.kind)}</strong></label><p>建议上级：${esc(state.groups[r.parent])}</p><p>${esc(r.suggestion.difference)}</p><p>${esc(r.suggestion.evidence)}</p>${r.posts.map(p=>`<a href="${esc(p.url)}" rel="noreferrer" target="_blank">代表帖子</a>`).join(' ')}</div>`).join(''):'<p class="muted">没有需要你确认的新品种。</p>')+'<h2>待核对</h2>'+(data.reviews.map(r=>`<div class="review"><a href="${esc(r.url)}" rel="noreferrer" target="_blank">${esc(r.title)}</a><p>${esc(r.reason)}</p><button data-check="${esc(r.url)}">查看处理依据</button></div>`).join('')||'<p class="muted">没有待核对记录。</p>');
    if($('#review-all'))$('#review-all').onclick=()=>document.querySelectorAll('[data-novel]').forEach(i=>i.checked=true);
    if($('#review-choice'))$('#review-choice').onclick=()=>guarded(async()=>{const ids=[...document.querySelectorAll('[data-novel]:checked')].map(i=>i.dataset.novel);if(!ids.length)throw Error('请先选择新品种');state=await api('review-choice',{id:state.draft.id,revision:state.draft.revision,ids,mode:$('#review-mode').value,target:$('#review-target').value});notice('审核选择已加入草稿，预览并确认后生效。');summary();});
    document.querySelectorAll('[data-check]').forEach(b=>b.onclick=()=>{tab='check';render();$('#post-url').value=b.dataset.check;$('#post-check').click();});
  }catch(e){notice(e.message,true);}
}
function renderCheck(){
  $('#content').innerHTML='<h2>检查一条帖子</h2><p class="muted">查看是否抓到正文、识别到的商品，以及推送或过滤的原因。</p><div class="row"><input type="url" id="post-url" placeholder="粘贴四个平台的原帖链接"><button id="post-check">检查</button></div><pre id="check-result"></pre>';
  $('#post-check').onclick=async()=>{try{const result=await api('check?url='+encodeURIComponent($('#post-url').value));$('#check-result').textContent=result.text;}catch(e){notice(e.message,true);}};
}
$('#preview').onclick=()=>guarded(async()=>{previewResult=await api('preview',{id:state.draft.id,revision:state.draft.revision});const p=previewResult;
  $('#preview-result').innerHTML=`<h3>确认有效关注清单</h3>${$('#summary').innerHTML}<p class="muted">统计截至 ${new Date(p.at*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})}（北京时间）</p><div class="counts">${[['match','保留'],['exclude','排除'],['review','待核对']].map(([k,l])=>`<div>${l}<strong>${p.counts[k]}</strong>件</div>`).join('')}</div><p>回放 ${p.posts} 帖，原记录完整 ${p.complete}，缺正文 ${p.missing}。历史回放不发送通知。</p>${Object.entries(p.details).map(([k,entries])=>`<h3>${{match:'保留样本',exclude:'排除样本',review:'待核对样本'}[k]}</h3>${entries.map(d=>`<p><a href="${esc(d.url)}" target="_blank" rel="noreferrer">${esc(d.title)}</a><br>${esc(d.reasons.join('；'))}${d.price!==null?' · '+d.price+'（原帖标价）':''}</p>`).join('')}`).join('')}<details><summary>历史覆盖与缺口</summary>${Object.entries(p.coverage).map(([source,v])=>`<p>${esc(source)}：${v.posts} 帖，原记录完整 ${v.complete} 帖。</p>`).join('')}<p>CHH 留存不足90天；V2EX 受公开频道覆盖限制；Linux.do 使用已有 Helper 与公开内容，均不代表全站。</p></details><p>确认后用于后续处理。已有通知不会重发。</p>`;
  $('#preview-dialog').showModal();});
$('#apply').onclick=()=>guarded(async()=>{const result=await api('apply',{id:state.draft.id,revision:state.draft.revision,hash:previewResult.hash});state=await api('state');$('#preview-dialog').close();notice(`已应用版本 ${result.version}。`);render();});
$('#back').onclick=()=>$('#preview-dialog').close();
$('#rollback').onclick=()=>guarded(async()=>{state=await api('rollback',{});notice('上一版本已放入草稿，请预览后确认。');render();});
$('#logout').onclick=()=>guarded(async()=>{await api('logout',{});location.reload();});
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{tab=b.dataset.tab;render();});
(async()=>{try{const fragment=new URLSearchParams(location.hash.slice(1)),token=fragment.get('login');history.replaceState(null,'',location.pathname);if(token)await api('login',{token});state=await api('state');render();}catch(e){notice(e.message,true);}})();
