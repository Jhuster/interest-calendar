'use strict';
const $=s=>document.querySelector(s),page=document.body.dataset.page;
let status,interests=[],events=[],view='list',month;
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const beijing=d=>new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(d);
const today=beijing(new Date());month=today.slice(0,7);
function dateAdd(s,n){const d=new Date(s+'T12:00:00Z');d.setUTCDate(d.getUTCDate()+n);return d.toISOString().slice(0,10)}
function notify(text,error=false){const el=$('#message');el.hidden=false;el.textContent=text;el.className=error?'error':'';el.scrollIntoView({block:'nearest'})}
async function api(path,method='GET',data){const r=await fetch('/api/v1'+path,{method,headers:{'Content-Type':'application/json','X-CSRF-Token':$('meta[name="csrf-token"]').content},body:data?JSON.stringify(data):undefined});const b=await r.json();if(!r.ok){if(r.status===401&&page!='/login')location.href='/login';throw Error(b.errors?.map(x=>x.message).join('；')||'请求失败，请刷新后重试')}return b}
async function action(fn){try{await fn()}catch(e){notify(e.message,true)}}
function bind(id,fn){$(id)?.addEventListener('click',()=>action(fn))}
function empty(title,text,link='',label=''){return `<div class="empty"><div class="empty-mark">＋</div><h2>${esc(title)}</h2><p class="muted">${esc(text)}</p>${link?`<a class="button" href="${link}">${label}</a>`:''}</div>`}
function timeText(e){let t=e.timing;if(t.kind==='date')return `${t.start_date}${dateAdd(t.end_date_exclusive,-1)!==t.start_date?' — '+dateAdd(t.end_date_exclusive,-1):''} · 具体时间待定`;const fmt=s=>new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(s));return fmt(t.start)+(t.end?' — '+fmt(t.end):' · 结束时间待定')}
function eventTitle(e){const t=e.timing;const n=t.kind==='date'?(Date.parse(t.end_date_exclusive)-Date.parse(t.start_date))/86400000:1;return n>1?`${e.title}（共${n}天）`:e.title}
function tags(e){return `${e.status==='cancelled'?'<span class="tag cancelled">已取消</span>':''}`+e.interest_ids.map(id=>`<span class="tag">${esc(interests.find(i=>i.id===id)?.keyword||'已归档兴趣')}</span>`).join('')}
function detailDate(value, timed=false){
  return new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'long',day:'numeric',weekday:'short',...(timed?{hour:'2-digit',minute:'2-digit',hour12:false}:{})}).format(new Date(timed?value:value+'T12:00:00+08:00'));
}
function detailTiming(t){
  if(t.kind==='date'){
    const last=dateAdd(t.end_date_exclusive,-1),n=(Date.parse(t.end_date_exclusive)-Date.parse(t.start_date))/86400000;
    return `<strong>${esc(detailDate(t.start_date))}${n>1?`<br>至 ${esc(detailDate(last))}`:''}</strong><span>${n>1?`共 ${n} 天 · 日历仅在首日展示 · `:''}具体时刻待公布</span>`;
  }
  return `<strong>${esc(detailDate(t.start,true))}${t.end?`<br>至 ${esc(detailDate(t.end,true))}`:''}</strong><span>北京时间${t.end?'':' · 结束时间待公布'}</span>`;
}
function sourceLink(value){try{const u=new URL(value);return ['http:','https:'].includes(u.protocol)?{url:u.href,name:u.hostname}:null}catch{return null}}
async function detail(id){
  const e=await api('/events/'+id),sources=e.evidence||[],intro=sources.find(x=>x.excerpt?.trim());
  $('#detail-content').innerHTML=`<div class="eyebrow">活动详情</div>
    <h2 id="detail-title">${esc(e.title)}</h2><div class="tags">${tags(e)}</div>
    <dl class="detail-facts"><div><dt>活动时间</dt><dd>${detailTiming(e.timing)}</dd></div><div><dt>活动地点</dt><dd>${esc(e.location||'暂未公布')}</dd></div></dl>
    <section class="detail-section"><h3>关于这场活动</h3><p class="detail-intro">${esc(intro?.excerpt||'暂无活动介绍，可查看下方来源了解详情。')}</p>${intro?'<p class="small muted">以上为来源摘录。</p>':''}</section>
    <section class="detail-section"><h3>来源与最新信息</h3><p class="small muted">活动安排可能调整，出发或观看前可再次查看来源。</p><div class="detail-sources">${sources.map((x,i)=>{const link=sourceLink(x.url);return `<article>${link?`<a href="${esc(link.url)}" target="_blank" rel="noopener noreferrer">${esc(link.name)} <span aria-hidden="true">↗</span></a>`:'<span>来源链接不可用</span>'}<p class="small muted">核验于 ${esc(detailDate(x.verified_at,true))} · 北京时间</p>${x!==intro?`<details><summary>查看来源摘录</summary><p class="detail-intro">${esc(x.excerpt)}</p></details>`:''}</article>`}).join('')}</div></section>`;
  $('#detail').setAttribute('aria-labelledby','detail-title');$('#detail').showModal();
}

async function schedule(){const filter=$('#interest-filter').value;let start=view==='list'?today:month+'-01',end=view==='list'?dateAdd(today,7):new Date(Date.UTC(Number(month.slice(0,4)),Number(month.slice(5)),1)).toISOString().slice(0,10);let offset=0;events=[];while(true){const result=await api(`/events?start_date=${start}&end_date=${end}&limit=200&offset=${offset}${filter?'&interest_id='+encodeURIComponent(filter):''}`);events.push(...result.items);if(events.length>=result.total)break;offset+=200}
$('#count-label').textContent=`${view==='list'?'未来 7 天':'本月'} · ${events.length} 个日程`;
$('#month-controls').hidden=view!=='month';$('#month-label').textContent=month.replace('-',' 年 ')+' 月';
for(const id of ['list','month']){$('#'+id+'-view').classList.toggle('active',view===id);$('#'+id+'-view').setAttribute('aria-pressed',String(view===id))}
const content=$('#schedule-content');
if(view==='list'){content.innerHTML=events.length?events.map(e=>`<button class="event-row" data-event="${e.id}"><span class="date-tile"><small>${Number(e.start_date.slice(5,7))} 月</small><strong>${Number(e.start_date.slice(8))}</strong></span><span class="event-body"><h3>${esc(eventTitle(e))}</h3><span class="event-meta">${esc(timeText(e))}${e.location?' · '+esc(e.location):''}</span><span class="tags">${tags(e)}</span></span><span class="arrow">↗</span></button>`).join(''):empty(interests.length?'还没有即将到来的日程':'从一份兴趣开始',interests.length?'兴趣已准备好。连接 Agent 后，核验过的活动会出现在这里。':'添加你关注的赛事、演出或展览，把下一份期待放进日历。',interests.length?'/settings#agent':'/interests',interests.length?'连接 Agent':'添加兴趣')}
else{const first=month+'-01',week=(new Date(first+'T12:00:00Z').getUTCDay()+6)%7;let s=dateAdd(first,-week);let html='<div class="calendar-grid">'+['一','二','三','四','五','六','日'].map(x=>`<div class="weekday">${x}</div>`).join('');for(let n=0;n<42;n++){let day=dateAdd(s,n),rows=events.filter(e=>e.timing.kind==='date'?e.start_date===day:e.start_date<=day&&e.end_date_exclusive>day);html+=`<div class="day ${day===today?'today':''} ${day.slice(0,7)!==month?'outside':''}"><span class="day-number">${Number(day.slice(8))}</span>${rows.map(e=>`<button class="mini-event ${e.status==='cancelled'?'cancelled':''}" data-event="${e.id}">${e.status==='cancelled'?'已取消 · ':''}${esc(eventTitle(e))}</button>`).join('')}</div>`}content.innerHTML=html+'</div>'}
content.querySelectorAll('[data-event]').forEach(el=>el.onclick=()=>action(()=>detail(el.dataset.event)))}
async function loadInterests(){const b=await api('/interests');interests=b.items;status={...status,state_epoch:b.state_epoch,config_version:b.config_version};$('#interest-content').innerHTML=interests.length?interests.map(i=>`<article class="panel interest-card"><div><h3>${esc(i.keyword)}</h3><p class="muted">${esc(i.conditions||'不限定额外条件')}</p><span class="small muted">${i.future_count} 个未来日程</span></div><button class="secondary danger" data-delete="${i.id}">移除</button></article>`).join(''):empty('还没有关注的兴趣','从左侧添加一个关键词。');document.querySelectorAll('[data-delete]').forEach(el=>el.onclick=()=>action(async()=>{const i=interests.find(x=>x.id===el.dataset.delete);if(!confirm(`移除“${i.keyword}”的关注？\n将撤下 ${i.exclusive_count} 个独占未来日程，保留 ${i.future_count-i.exclusive_count} 个共享未来日程及历史记录。这不会取消活动。`))return;await api('/interests/'+i.id,'DELETE',{state_epoch:status.state_epoch,config_version:status.config_version});await loadInterests();notify('已移除关注；受影响的日历变更将在发布后生效。')}))}
async function copy(id){
  const el=$(id),text=el.value??el.textContent;
  try{
    await navigator.clipboard.writeText(text);
    if(id==='#agent-prompt'){
      const button=$('#copy-agent');button.textContent='已复制 ✓';
      clearTimeout(button.copyReset);button.copyReset=setTimeout(()=>{button.textContent='复制内容'},2500);
    }else notify('已复制');
  }catch{
    el.focus();
    if(typeof el.select==='function')el.select();
    else{const range=document.createRange();range.selectNodeContents(el);const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range)}
    notify('无法自动复制，已选中完整内容，请手动复制。');
  }
}
async function copyAgentToken(){
  const input=$('#agent-token'),button=$('#copy-agent-token');
  try{
    const result=await api('/agent/token');
    input.value=result.token;
    input.type='text';
    await navigator.clipboard.writeText(result.token);
    button.textContent='已复制 ✓';
    clearTimeout(button.copyReset);button.copyReset=setTimeout(()=>{button.textContent='复制令牌';input.value='********';input.type='password'},2500);
  }catch(error){
    if(input.value && input.value!=='********'){
      input.type='text';input.focus();input.select();
      button.textContent='已选中，请按 ⌘C / Ctrl+C';
      notify('浏览器未授权自动复制，令牌已选中，请按 ⌘C / Ctrl+C。',true);
    }else notify(error.message,true);
  }
}
function publicationText(status){return status==='published'?'已发布':status==='pending'?'待发布':status==='before_restore'?'恢复前记录':'无需发布'}
async function runs(){const result=await api('/batches');$('#runs-content').innerHTML=result.items.length?result.items.map(r=>`<article class="panel"><h3>${r.receipt.import_status==='imported'?'已导入':'已拒绝'} · ${publicationText(r.receipt.publication_status)}</h3><p class="muted">${esc(new Date(r.received_at).toLocaleString('zh-CN'))}</p><p class="small">批次 ${esc(r.batch_id)}</p><details><summary>查看回执与处理建议</summary><pre>${esc(JSON.stringify(r.receipt,null,2))}</pre></details></article>`).join(''):empty('尚未收到结果','Agent 上传后，接收和发布记录会显示在这里。')}
const reasons={uncertain_date:'日期待确认',conflicting_sources:'来源冲突',possible_duplicate:'可能重复',insufficient_evidence:'证据不足',postponed:'延期，日期待定'};
async function candidates(){const result=await api('/candidates');$('#candidates-content').innerHTML=result.items.length?result.items.map(c=>`<article class="panel" data-candidate="${c.id}"><span class="tag">${esc(reasons[c.payload.reason])} · ${c.state==='pending'?'待核验':'已处理'}</span><h2>${esc(c.payload.title)}</h2><p>${esc(c.payload.notes)}</p>${c.payload.evidence.map(e=>`<blockquote>${esc(e.excerpt)}</blockquote><p><a href="${esc(e.url)}" target="_blank" rel="noopener noreferrer">查看来源 ↗</a></p>`).join('')}<details><summary>查看提案与当前日程</summary><pre>${esc(JSON.stringify(c.payload.proposed_event,null,2))}</pre>${c.payload.related_event_id?`<button class="secondary" data-event="${c.payload.related_event_id}">查看当前已保存的日程</button>`:''}</details>${c.state==='pending'?`<details><summary>补齐或修正提案（高级）</summary><p class="small muted">按接入协议填写完整提案，确认前请核对来源和日期。普通候选可直接确认已有完整提案。</p><textarea class="candidate-editor" rows="9" aria-label="完整事件提案">${esc(JSON.stringify(c.payload.proposed_event,null,2))}</textarea></details><div class="actions"><button data-action="approve">确认并发布</button>${c.payload.reason==='possible_duplicate'?'<button class="secondary" data-action="merge">并入已有日程</button>':''}${c.payload.reason==='postponed'?'<button class="secondary" data-action="withdraw">撤下旧日程</button>':''}<button class="secondary danger" data-action="reject">拒绝</button></div>`:'<button class="secondary" data-action="reopen">重新核验</button>'}</article>`).join(''):empty('没有待核验的日程','来源冲突、日期不明或可能重复的活动会留在这里。');
document.querySelectorAll('[data-event]').forEach(el=>el.onclick=()=>action(()=>detail(el.dataset.event)));
document.querySelectorAll('[data-candidate]').forEach(card=>card.querySelectorAll('[data-action]').forEach(button=>button.onclick=()=>action(async()=>{const c=result.items.find(x=>x.id===card.dataset.candidate),a=button.dataset.action,b={state_epoch:status.state_epoch,version:c.version,action:a};if(['approve','merge'].includes(a)){try{b.proposed_event=JSON.parse(card.querySelector('textarea').value)}catch{throw Error('提案不是有效 JSON，请检查或交给 Agent 补齐')}if(!b.proposed_event)throw Error('请先补齐完整的日期、来源和事件提案');}if(['merge','withdraw'].includes(a)){b.target_event_id=c.payload.related_event_id;const target=await api('/events/'+b.target_event_id);b.target_version=target.version}if(!confirm('确认执行此操作？请先核对来源与日期。'))return;await api('/candidates/'+c.id+'/resolve','POST',b);await candidates();notify('处理已保存。涉及订阅的变更将在发布后生效。')})))}
async function resetScope(scope){const label=scope==='all'?'恢复初始状态':'清空日历数据';if(!confirm(`确认${label}？\n\n${scope==='all'?'兴趣配置和全部日历数据都会删除。':'事件、候选和发布记录都会删除，但兴趣配置会保留。'}\n\n此操作不可撤销。`))return;await api('/admin/reset','POST',{scope,confirmation:'RESET',state_epoch:status.state_epoch,config_version:status.config_version});notify(`${label}已完成。`);location.reload()}
async function start(){bind('#close-detail',()=>$('#detail').close());if(page==='/login'){$('#login-form').onsubmit=e=>{e.preventDefault();action(async()=>{await api('/session','POST',{token:$('#token').value});location.href='/'})};return}
status=await api('/status');
if(page==='/'){interests=(await api('/interests')).items;$('#today-label').textContent=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'long',day:'numeric',weekday:'long'}).format(new Date());$('#publication-label').textContent=status.publish_error|| (status.data_revision>status.published_revision?'变更已保存，日历待发布':status.published_revision===0?'等待第一份日程':'订阅已处理至版本 '+status.published_revision);$('#candidate-link').textContent=`待核验 ${status.candidate_count}`;interests.forEach(i=>$('#interest-filter').add(new Option(i.keyword,i.id)));$('#interest-filter').onchange=()=>action(schedule);bind('#list-view',async()=>{view='list';await schedule()});bind('#month-view',async()=>{view='month';await schedule()});for(const [id,step] of [['previous-month',-1],['next-month',1]])bind('#'+id,async()=>{month=new Date(Date.UTC(Number(month.slice(0,4)),Number(month.slice(5))-1+step,1)).toISOString().slice(0,7);await schedule()});await schedule()}
if(page==='/interests'){await loadInterests();$('#interest-form').onsubmit=e=>{e.preventDefault();action(async()=>{await api('/interests','POST',{state_epoch:status.state_epoch,config_version:status.config_version,keyword:$('#keyword').value,conditions:$('#conditions').value});e.target.reset();await loadInterests();notify('兴趣已添加，Agent 下次采集将使用最新配置。')})}}
if(page==='/settings'){$('#agent-server-address').value=status.base_url;action(async()=>{try{$('#agent-token').value=(await api('/agent/token')).token}catch(error){$('#agent-token').value='令牌读取失败：'+error.message}});$('#subscribe-url').value=status.subscription_url||'';$('#subscribe-url').placeholder=status.development?'尚未配置局域网地址':'日历订阅地址';$('#deployment-note').textContent=status.development?'当前是本机开发预览，127.0.0.1 只能由这台电脑访问，不能复制到手机。请用固定局域网 IP 配置 BASE_URL 后再订阅。':'同一局域网可读取此订阅中的日程；管理功能仍需登录。';$('#publish-detail').textContent=status.published_revision===0?'当前为初始化空日历，尚未发布实际日程。':`最近发布：${new Date(status.published_at).toLocaleString('zh-CN')}；已处理至版本 ${status.published_revision}。`;$('#agent-prompt').textContent='首次接入请向我询问两项信息：兴趣日历服务器地址，以及 Agent Token。拿到后读取服务器地址/api/v1/agent/guide，并按指引接入兴趣日历。已配置地址后，只有连续三天（至少72小时）无法连接且重试仍失败时，才询问我服务器是否开机或地址是否变化；此前静默等待下次运行，连接恢复即清零计时，同一次故障只询问一次。';$('#receive-detail').textContent=status.last_received?`最近接收：${new Date(status.last_received).toLocaleString('zh-CN')}`:'尚未收到结果。';$('#version-detail').textContent=`当前数据版本 ${status.data_revision} · 当前订阅版本 ${status.published_revision}${status.publish_error?' · '+status.publish_error:''}${status.backup_error?' · 备份失败：'+status.backup_error:''}`;bind('#copy-subscribe',()=>copy('#subscribe-url'));bind('#copy-agent',()=>copy('#agent-prompt'));bind('#copy-agent-token',copyAgentToken);bind('#logout',async()=>{await api('/session','DELETE');location.href='/login'});bind('#retry-publish',async()=>{await api('/publications/retry','POST',{state_epoch:status.state_epoch});location.reload()});bind('#reset-calendar',()=>action(()=>resetScope('calendar')));bind('#reset-all',()=>action(()=>resetScope('all')))}
if(page==='/runs'){await runs();bind('#refresh-runs',runs)}if(page==='/candidates')await candidates()}
action(start);
