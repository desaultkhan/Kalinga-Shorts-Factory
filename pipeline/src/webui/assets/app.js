let S=null, pollTimer=null, VIEW=null, homeTimer=null, watching=null, ASM_DIRTY=false, OVN=0;
let HOMEVIEW=null, CED=null;   // home sub-view ('cast') · open cast edit form {name}
const $=s=>document.querySelector(s);
const esc=s=>(s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
// attribute-safe JSON arg for an inline handler in a "double-quoted" onclick:
// JSON.stringify emits literal quotes, so escape " and ' to entities too — the
// browser decodes them back to a valid JS string before the handler runs.
const aj=o=>esc(JSON.stringify(o)).replace(/"/g,'&quot;').replace(/'/g,'&#39;');
const art=a=>a?`/artifact/${a.name}?v=${a.mtime}`:'';

// job `secs` tick every poll — compare without them so a quiet poll doesn't
// rebuild the DOM (the innerHTML swap is the page-wide flicker); the running
// counters are patched in place instead
const stateKey=o=>JSON.stringify(o,(k,v)=>k==='secs'?0:v);
function patchSecs(){ (S.jobs||[]).forEach(j=>{const el=document.querySelector(`[data-jsecs="${j.id}"]`); if(el&&j.status==='running')el.textContent=j.status+' · '+fmtSecs(j.secs);}); }
async function getState(){ const ns=await (await fetch('/api/state')).json(); const same=S&&stateKey(S)===stateKey(ns); S=ns; if(same)patchSecs(); else render(); if(S.job&&S.job.status==='running'&&(watching==null||watching===S.job.id))watch(S.job.id); }
async function nav(i){ VIEW=null; S=await (await fetch('/api/nav',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({stage:i})})).json(); render(); }

async function action(stage,actName,extra={}){
  if(stage==='assemble'&&actName!=='ov_bulk'&&ASM_DIRTY)await flushAsm();
  const body=Object.assign({stage,action:actName},extra);
  const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json();
  if(!r.ok){ toast(d.error||'error'); return d; }
  if(d.kind==='job'){ openLog(d.label); watch(d.jobId); }
  else { if(d.result&&d.result.problems&&d.result.problems.length) toast('saved — '+d.result.problems.join('; ')); await getState(); }
  return d;
}

function watch(id,onDone){
  if(watching===id&&pollTimer)return;         // already on this job's console
  watching=id;
  $('#console').classList.add('show'); $('#spin').style.display='inline-block';
  if(pollTimer)clearInterval(pollTimer);
  const tick=async()=>{
    const j=await (await fetch('/api/job/'+id)).json();
    if(watching!==id)return;                  // the user moved to another log
    $('#log').textContent=j.log||''; $('#log').scrollTop=$('#log').scrollHeight;
    $('#jobLabel').textContent=(j.label||'working')+' · '+j.status;
    if(j.status!=='running'){
      clearInterval(pollTimer); pollTimer=null; watching=null;
      $('#spin').style.display='none';
      if(j.status==='error')$('#jobLabel').innerHTML='<span class="bad">'+esc(j.label||'job')+' failed — see log</span>';
      await getState();
      if(onDone)await onDone(j.status);
    }
  };
  tick(); pollTimer=setInterval(tick,700);
}
function openLog(label){ $('#console').classList.add('show'); $('#log').textContent='starting…'; if(label)$('#jobLabel').textContent=label+' · starting'; }
$('#hideLog').onclick=()=>$('#console').classList.remove('show');
let toastT=null;
function toast(m){ let t=$('#toast'); if(!t){t=document.createElement('div');t.id='toast';
  t.style.cssText='position:fixed;left:50%;top:14px;transform:translateX(-50%);background:#2a1014;border:1px solid var(--rd);color:#ffd;padding:9px 16px;border-radius:8px;z-index:20';
  document.body.appendChild(t);} t.textContent=m; t.style.display='block';
  clearTimeout(toastT); toastT=setTimeout(()=>t.style.display='none',4200); }

$('#autoBtn').onclick=()=>{ if(confirm('Finish every remaining stage headless (~10–30 min)?')) action('global','auto'); };
$('#quitBtn').onclick=async()=>{ await fetch('/api/quit',{method:'POST'}); const r=(S&&S.topic)?('Resume with <code>kalinga.py make '+esc(S.topic)+'</code> or <code>kalinga.py ui</code>.'):'Reopen with <code>kalinga.py ui</code>.'; document.body.innerHTML='<main style="padding:40px"><h2>Server stopped.</h2><p class="sub">'+r+'</p></main>'; };

function render(){
  if(!S)return;
  syncHeader();
  syncSetupBanner();
  if(S.mode==='home')return renderHome();
  renderVideo();
}
function syncSetupBanner(){
  const b=$('#setupBanner'); if(!b)return;
  if(!S.setupWarning){ b.style.display='none'; return; }
  b.style.display='';
  b.innerHTML=`<span class="ico">⚠</span><div><b>Setup incomplete</b> — you can browse and edit,
    but generation stages will fail until it's fixed:
    <pre>${esc(S.setupWarning)}</pre>
    Run the command above in a terminal (or <code>python3 kalinga.py init</code> for the guided
    walkthrough), then restart the UI.</div>`;
}
function syncHeader(){
  const m=S.mode||'video';
  $('#nav').style.display = m==='video'?'':'none';
  $('#autoBtn').style.display = m==='video'?'':'none';
  $('#tpl').style.display = m==='video'?'':'none';
  $('#homeBtn').style.display = m==='home'?'none':'';
  if(m==='home'){ $('#topic').textContent='control center';
    $('#chan').innerHTML = S.channel?('channel <b>'+esc(S.channelTitle||S.channel)+'</b>'):'';
    $('#cred').innerHTML='credits <b>'+(S.credits==null?'?':S.credits)+'</b>'; }
}
function renderVideo(){
  $('#topic').textContent=S.topic;
  $('#chan').innerHTML='channel <b>'+esc(S.channelTitle||S.channel)+'</b>';
  const tpls=(S.templates||[]);
  if(tpls.length>1){
    $('#tpl').innerHTML='template <select id="tplSel" title="Switch the visual world (models, look)">'
      +tpls.map(t=>`<option ${t===S.template?'selected':''}>${esc(t)}</option>`).join('')+'</select>';
    const sel=$('#tplSel'); if(sel)sel.onchange=()=>switchTemplate(sel.value);
  } else $('#tpl').innerHTML='template <b>'+esc(S.template||'')+'</b>';
  const spent=(S.creditsStart!=null&&S.credits!=null)?(S.creditsStart-S.credits):null;
  $('#cred').innerHTML='credits <b>'+(S.credits==null?'?':S.credits)+'</b>'+(spent!=null?' · spent '+spent:'')+(S.budget?' / budget '+S.budget:'');
  const busy=S.job&&S.job.status==='running';
  $('#autoBtn').disabled=busy;
  // rail
  const boardRow=S.board?`<div class="stage ${VIEW==='board'?'cur':''}" onclick="showBoard()">
       <span class="n">◈</span><span class="dot"></span><span>storyboard</span></div>`:'';
  $('#nav').innerHTML=boardRow+S.stages.map((st,i)=>
    `<div class="stage ${st.done?'done':''} ${st.current&&VIEW!=='board'?'cur':''}" onclick="nav(${i})">
       <span class="n">${i+1}</span><span class="dot"></span><span>${st.name}</span></div>`).join('');
  // panel
  if(VIEW==='board'&&S.board){ $('#main').innerHTML=renderBoard(); return; }
  const fn=PANELS[S.stageName]||(()=>'<div class="card">—</div>');
  $('#main').innerHTML=`<h2>${S.stageName.toUpperCase()}</h2>`+fn(S.data||{},busy);
  bind();
}

// helper builders
const accBar=(stage,label='Accept & continue →')=>`<div class="bar"><button class="good" data-act="${stage}:accept">${label}</button></div>`;
const fb=(id,ph)=>`<textarea id="${id}" placeholder="${ph}"></textarea>`;
// ---- script SECTION preview: how each beat's text will break into voiced/
// captioned/shot sections, with clickable break points between sentences ----
function secPreview(i,sec,sg){
  if(!sec||!sec.sents||!sec.sents.length)
    return `<div class="txt">${esc(sg.text||'')}</div>`;
  if(sec.sents.length===1)
    return `<div class="txt">${esc(sg.text||'')}</div>`;
  const cuts=new Set(); let acc=0;
  (sec.sizes||[]).slice(0,-1).forEach(z=>{acc+=z;cuts.add(acc);});
  let html='<div class="secpre">';
  sec.sents.forEach((sn,j)=>{
    if(j>0){
      const on=cuts.has(j);
      html+=`<span class="secbrk ${on?'on':''}" onclick="toggleBreak(${i},${j})"
        title="${on?'merge — remove this section break':'split — a new section starts here'}">${on?'∥':'∙'}</span>`;
    }
    html+=`<span class="secsent">${esc(sn)}</span>`;
  });
  html+='</div>';
  const wc=[]; let k=0;
  (sec.sizes||[]).forEach(z=>{
    const words=sec.sents.slice(k,k+z).join(' ').split(/\s+/).filter(Boolean).length;
    wc.push(words+'w'); k+=z;});
  html+=`<div class="hint">${sec.sizes.length} section(s): ${wc.join(' · ')}
    ${sec.custom?`<span class="tag">custom ✎</span> <button class="mini" onclick="setBreaks(${i},[])">auto</button>`:''}</div>`;
  return html;
}
function toggleBreak(i,j){
  const sec=((S.data||{}).sections||[])[i]; if(!sec)return;
  const cuts=new Set(); let acc=0;
  (sec.sizes||[]).slice(0,-1).forEach(z=>{acc+=z;cuts.add(acc);});
  if(cuts.has(j))cuts.delete(j); else cuts.add(j);
  const pts=[...cuts].sort((a,b)=>a-b);
  const sizes=[]; let prev=0;
  pts.concat([sec.sents.length]).forEach(c=>{sizes.push(c-prev);prev=c;});
  setBreaks(i,sizes);
}
async function setBreaks(i,sizes){
  const d=await api('/api/action',{stage:'script',action:'sections',index:i,sizes});
  if(d.error)return;
  S=d.state;render();
}

const PANELS={
 research(d,busy){
   if(!d.facts) return `<div class="card">No facts yet.<div class="bar"><button class="primary" data-act="research:run">Run research</button></div></div>`;
   const f=d.facts;
   // structured, magazine-style rendering for the story-research keys; every
   // other adapter's fields fall through to the kv grid below
   const skip=new Set(['title','topic','verdict','one_liner','summary','origin','struggle','breakthrough','lesson','timeline','numbers','quotes','sources','hook_angles','telling_details','notes','context']);
   let h='';
   const vc=f.verdict?`<span class="vchip ${String(f.verdict).toUpperCase().includes('NOT')?'vbad':'vok'}">${esc(f.verdict)}</span>`:'';
   h+=`<div class="card"><div class="row"><span class="rttl">${esc(f.title||f.topic||'')}</span>${vc}</div>
     ${f.one_liner?`<div class="rlede">${esc(f.one_liner)}</div>`:''}</div>`;
   const story=['summary','origin','struggle','breakthrough','lesson'].filter(k=>f[k]);
   if(story.length) h+=`<div class="card">${story.map(k=>`<div class="rk">${k}</div><div class="rv">${esc(f[k])}</div>`).join('')}</div>`;
   const nums=Array.isArray(f.numbers)?f.numbers:[];
   if(nums.length) h+=`<div class="card"><div class="rk">the numbers</div><div class="statrow">${nums.map(n=>`<div class="stat"><div class="sv">${esc(n.value)}</div><div class="sl">${esc(n.label)}</div></div>`).join('')}</div></div>`;
   const tl=Array.isArray(f.timeline)?f.timeline:[];
   if(tl.length) h+=`<div class="card"><div class="rk">timeline</div>${tl.map(t=>`<div class="trow"><span class="tyr">${esc(t.year)}</span><span>${esc(t.event)}</span></div>`).join('')}</div>`;
   const qs=Array.isArray(f.quotes)?f.quotes:[];
   if(qs.length) h+=`<div class="card">${qs.map(q=>`<blockquote class="rq">“${esc(q.text)}”${q.who?`<span class="muted"> — ${esc(q.who)}</span>`:''}</blockquote>`).join('')}</div>`;
   const ha=Array.isArray(f.hook_angles)?f.hook_angles:[], td=Array.isArray(f.telling_details)?f.telling_details:[];
   if(ha.length||td.length) h+=`<div class="card">${ha.length?`<div class="rk">hook angles</div>${ha.map(x=>`<div class="rv">↳ ${esc(x)}</div>`).join('')}`:''}
     ${td.length?`<div class="rk">telling details</div>${td.map(x=>`<div class="rv">· ${esc(x)}</div>`).join('')}`:''}</div>`;
   let rows='';
   for(const k in f){ if(skip.has(k))continue;
     let v=f[k]; if(typeof v==='object'){v=JSON.stringify(v).slice(0,160)+'…';}
     rows+=`<div class="k">${esc(k)}</div><div>${esc(v)}</div>`; }
   if(rows) h+=`<div class="card"><div class="kv">${rows}</div></div>`;
   const srcs=Array.isArray(f.sources)?f.sources:[];
   if(srcs.length) h+=`<div class="card"><div class="rk">sources</div>${srcs.map(s=>`<div class="rv"><a href="${esc(s)}" target="_blank" rel="noopener">${esc(s)}</a></div>`).join('')}</div>`;
   h+=`<div class="card"><div class="bar"><button data-act="research:rerun">↻ Re-run research</button>
       <button class="ghost" onclick="editJson('facts.json','research:save')">Edit facts JSON</button></div></div>`+accBar('research');
   return h;
 },
 concept(d,busy){
   const lo=(d.targetWords||[])[0], hi=(d.targetWords||[])[1];
   let h='';
   h+=`<div class="card"><b>Concept</b> <span class="muted">— your hook idea + recurring theme/motif (drives the writer's opening + the director's world)</span>`;
   if(d.concept){ h+=`<div class="seg"><div class="txt">${esc(d.concept)}</div></div>`; }
   else { h+=`<div class="hint">No concept set — write one, let the AI suggest, or skip for the default treatment.</div>`; }
   h+=`</div>`;
   // write / paste your own
   h+=`<div class="card"><b>Write or paste your own</b>
     ${fb('cpt','e.g. A chai-stall skit — someone bursts in asking if MSFT is halal; the chai stall is the recurring motif')}
     <div class="bar"><button class="primary" onclick="saveConcept(${d.hasScript?'true':'false'})">Save concept →</button></div></div>`;
   // AI suggest / refine
   let sug='';
   if(d.suggestion){ sug=`<div class="seg"><div class="txt">${esc(d.suggestion)}</div>
     <div class="bar"><button class="good" onclick="useSuggestion(${JSON.stringify(d.suggestion).replace(/"/g,'&quot;')},${d.hasScript?'true':'false'})">Use this →</button>
       <button data-act="concept:suggest">↻ Another</button></div></div>`; }
   h+=`<div class="card"><b>Let the AI write it</b> <span class="muted">(reads what's been done, won't repeat it)</span>
     ${sug}
     <div class="bar"><button class="primary" data-act="concept:suggest">${d.suggestion?'Suggest another':'Suggest a concept'}</button></div>
     <div class="hint">…or give it your rough idea/notes to refine:</div>
     ${fb('crf','rough primers / notes → the AI writes a polished concept')}
     <div class="bar"><button data-send="concept:refine" data-src="crf" data-inline="1">Refine with AI</button></div></div>`;
   // length
   h+=`<div class="card"><b>Video length</b> <span class="muted">(now ${lo}-${hi} words)</span>
     <div class="row"><input id="clen" placeholder="200 · 180-240 · 45s · 1m30s"/>
     <button data-send="concept:length" data-src="clen" data-inline="1">Set length</button></div></div>`;
   // reference by link
   h+=`<div class="card"><b>Copy a video's look by link</b> <span class="muted">(TikTok / Instagram)</span>
     <div class="row"><input id="clink" placeholder="https://… video URL"/></div>
     <div class="row"><input id="cfocus" placeholder="what to pull — e.g. fast jump-cuts + bold captions (optional)"/></div>
     <div class="bar"><button onclick="pullRef()">Pull reference</button>
       <span class="hint">${d.refImages||0} image(s), ${d.refVideos||0} video(s) in concept/</span></div></div>`;
   if(d.used&&d.used.length){
     h+=`<div class="card"><b>Already used</b>`+d.used.map(u=>`<div class="hint">${esc(u.topic)} — ${esc((u.concept||'').slice(0,60))}</div>`).join('')+`</div>`;
   }
   h+=`<div class="bar"><button class="good" data-act="concept:accept">${d.concept?'Keep & continue →':'Continue →'}</button>
     <button data-act="concept:skip">Skip — no concept</button></div>`;
   return h;
 },
 script(d,busy){
   const sc=d.script||{}; const segs=sc.segments||[];
   if(!segs.length) return `<div class="card">No script yet.<div class="bar"><button class="primary" data-act="script:gen">Write the script</button></div></div>`;
   const j=d.judge||{};
   const sco=j.scores||{};
   const scoStr=Object.keys(sco).length?Object.entries(sco).map(([k,v])=>`${k.slice(0,4)} ${v}`).join(' · '):'';
   let h=`<div class="card"><div class="row"><span class="pill">mode <b>${esc(d.mode||'?')}</b></span>
     <span class="pill ${j.pass?'ok':''}">script judge <b>${esc(j.score!=null?j.score+'/10':'?')}</b>${j.pass?' ✓':''}</span>
     ${scoStr?`<span class="pill">${esc(scoStr)}</span>`:''}
     ${d.drafts?`<span class="pill">${d.drafts} revision(s)</span>`:''}</div>
     ${j.pass?`<div class="hint">✓ good enough to ship${j.reason?` — ${esc(j.reason)}`:''}</div>`
       :`${j.reason?`<div class="hint">judge: ${esc(j.reason)}</div>`:''}
     ${j.improve?`<div class="hint">hint: ${esc(j.improve)}</div>`:''}
     ${j.improve?`<div class="bar"><button class="primary" data-act="script:judge" title="Auto-applies the judge's own critique, then re-scores">⚖ Apply the judge's hint</button></div>`:''}`}</div>`;
   h+=segs.map((sg,i)=>`<div class="seg"><span class="lbl">${esc(sg.label)}</span>
     ${secPreview(i,(d.sections||[])[i],sg)}
     ${sg.overlay?`<span class="tag">overlay: ${esc(sg.overlay)}</span>`:''}</div>`).join('');
   h+=`<div class="hint">∥ = a SECTION break — each section is voiced, captioned and shot on its own.
     Click a ∙ between sentences to split there, click a ∥ to merge; <i>auto</i> re-balances by length.</div>`;
   h+=`<div class="card"><b>Revision notes</b> <span class="muted">(the writer remembers every round)</span>
     ${fb('scfb','what to change — tone, a number framing, the hook…')}
     <div class="bar"><button class="primary" data-send="script:feedback" data-src="scfb">Revise</button>
       <button data-act="script:reroll">↻ Re-roll fresh (best-of-N)</button>
       <button class="ghost" onclick="editJson('script.json','script:save','segments')">Edit JSON</button></div>
     ${history(d.versions)}</div>`;
   return h+accBar('script','Accept script →');
 },
 direction(d,busy){
   if(!d.directed) return `<div class="card">Not directed yet — the director designs a bespoke visual world, reveal arc, and per-shot framing/motion.
     <div class="bar"><button class="primary" data-act="direction:gen">Design the visuals</button></div></div>`;
   const dir=d.direction||{};
   let h=`<div class="card">`;
   if(dir.visual_concept)h+=`<div><b>${esc(dir.visual_concept)}</b></div>`;
   if(dir.anchor_motif)h+=`<div class="hint">motif: ${esc(dir.anchor_motif)}</div>`;
   if(dir.reveal_arc)h+=`<div class="hint">teases: ${esc(dir.reveal_arc)}</div>`;
   h+=`</div>`;
   const j=d.judge||{};
   if(j.overall){
     const sc=j.scores||{};
     const cls=j.overall>=7?'ok':(j.overall>=5?'':'bad');
     const jsegs=j.segments||[];
     const canFix=jsegs.length||(j.improve||'').trim();
     h+=`<div class="card"><div class="row"><span class="pill ${cls}">director judge <b>${j.overall}/10</b></span>`
       + Object.entries(sc).map(([k,v])=>`<span class="pill">${esc(k)} <b>${esc(v)}</b></span>`).join('')
       + (jsegs.length?`<span class="pill">${jsegs.length} beat(s) to fix</span>`:'<span class="pill ok">every beat good ✓</span>')+`</div>`
       + (j.weakest?`<div class="hint">weakest: ${esc(j.weakest)} — ${esc(j.reason||'')}</div>`:'')
       + (canFix?`<div class="bar"><button class="primary" data-act="direction:judgefix" title="The per-beat director redesigns each flagged beat with its own fix; beats marked good are left untouched">⚖ Apply the judge's fixes${jsegs.length?` (${jsegs.length} beat${jsegs.length>1?'s':''})`:''}</button>
              <button data-act="direction:judge" title="Re-score the current direction">↻ Re-judge</button></div>`
          :`<div class="bar"><button data-act="direction:judge" title="Re-score the current direction">↻ Re-judge</button></div>`)
       + `</div>`;
   } else {
     h+=`<div class="card"><b>Director judge</b> <span class="muted">— score this plan (physics / theme / freshness / audience / clarity) before spending on keyframes</span>
       <div class="bar"><button class="primary" data-act="direction:judge">⚖ Run the director judge</button></div></div>`;
   }
   const judged=!!j.overall;
   h+=d.segments.map(sg=>`<div class="seg"><span class="lbl">${esc(sg.label)}</span>
     ${sg.animate?'<span class="tag warnc">▶ animated clip</span>':'<span class="tag ok">▮ Ken Burns still</span>'}
     ${sg.judgeNote?'<span class="tag warnc">⚖ judge: fix</span>':(judged?'<span class="tag ok">⚖ good</span>':'')}
     ${sg.overlay?`<div style="margin:4px 0"><span class="tag">on screen: ${esc(sg.overlay)}</span></div>`:''}
     <div class="txt">${esc(sg.visual||'')}</div>
     ${sg.motion?`<div class="hint">move: ${esc(sg.motion)}</div>`:''}
     ${sg.tease?`<div class="hint">hides: ${esc(sg.tease)}</div>`:''}
     ${sg.judgeNote?`<div class="hint" style="color:var(--warn)">judge fix: ${esc(sg.judgeNote)}</div>`:''}</div>`).join('');
   h+=`<div class="card"><b>Design notes</b> <span class="muted">(facts &amp; verdict stay locked)</span>
     ${fb('dnotes','change the look, the motif, a specific shot…')}
     <div class="bar"><button class="primary" data-send="direction:revise" data-src="dnotes">Revise design</button>
       <button data-act="direction:fill">Fill gaps only</button>
       <button class="warn" data-act="direction:replan">↻ Re-plan from scratch</button></div></div>`;
   return h+accBar('direction','Accept design →');
 },
 voice(d,busy){
   if(!d.segments.length) return `<div class="card">No script yet — write the script first.</div>`;
   let warn=d.cachedEngine&&d.cachedEngine!==d.engine?`<div class="card warnc">cached audio used '${esc(d.cachedEngine)}', current setting is '${esc(d.engine)}'.
     <div class="bar"><button data-act="voice:engine">Regenerate with ${esc(d.engine)}</button></div></div>`:'';
   const recCap=d.whisper?'recorded takes get word-synced karaoke':'recorded takes show the section text held (pip install faster-whisper for word-sync)';
   const genLabel=d.generated?'↻ Re-generate all (AI)':'Generate AI voiceover';
   // engine + voice DROPDOWNS with an audition button — pick by ear, not by
   // guessing names (edge samples are free; paid engines ≈1 tiny TTS call)
   const engOpts=(d.engines||[]).map(e=>`<option value="${av(e.name)}" ${e.name===d.engine?'selected':''} ${e.ok?'':'disabled'} title="${av(e.label||'')}">${esc(e.name)}${e.free?' (free)':''}${e.ok?'':' — '+esc(e.why)}</option>`).join('');
   const inCat=(d.voices||[]).some(v=>v.voice===d.voice);
   const vOpts=(inCat?'':`<option selected>${esc(d.voice)}</option>`)
     +(d.voices||[]).map(v=>`<option value="${av(v.voice)}" ${v.voice===d.voice?'selected':''}>${esc(v.voice)}${v.gender?' ('+v.gender[0].toUpperCase()+')':''}${v.note?' — '+esc(v.note):''}</option>`).join('');
   let h=`<div class="card"><div class="row"><span class="pill">engine <b>${esc(d.engine)}</b></span>
     <span class="pill">voice <b>${esc(d.voice)}</b></span>
     <span class="pill">${d.engine==='edge'?'karaoke captions: yes (free)':(d.whisper?'karaoke via whisper alignment':'no word timings')}</span>
     <span class="pill">your voice: ${d.whisper?'whisper ✓':'no whisper'}</span></div>
     <div class="hint">Two ways to voice this — let the AI read it, or 🎙 <b>record your OWN</b> on any unit below (${recCap}). You don't have to generate first.</div>
     <div class="row" style="margin-top:8px;align-items:center">
       <label class="hint">engine <select id="vEng" onchange="voiceEngine(this.value)">${engOpts}</select></label>
       <label class="hint">voice <select id="vSel">${vOpts}</select></label>
       <input type="text" id="vname" placeholder="…or any ${esc(d.engine)} voice" style="max-width:200px">
       <button class="mini" onclick="voiceSample()">▶ sample${d.engine==='edge'?' (free)':''}</button>
       <button class="mini" onclick="voiceSet()">✓ use this voice</button></div>
     <div class="bar"><button class="primary" data-act="voice:gen">${genLabel}</button></div></div>`+warn;
   // one unit = a section of a section-native beat, else the whole segment
   const recBtn=(i,sec,rec)=>{const key=i+'_'+(sec===null?'x':sec);
     return `<button class="rec" data-rec="${key}" onclick="recordUnit(${i},${sec===null?'null':sec})">${rec?'● Re-record':'● Record'}</button>`
       +(rec?`<button class="ghost" onclick="action('voice','unrecord',{index:${i}${sec===null?'':',section:'+sec}})" title="Revert to AI voice">↺ AI voice</button>`:'');};
   // per-line dialogue editor for multi-speaker beats — set when each line
   // STARTS (a number of seconds, or 'auto' for back-to-back) + its EXPRESSION
   const dlg=(m)=>{if(!m.lines||m.lines.length<2)return '';
     const rows=m.lines.map(ln=>{
       const rs=(ln.renderStart!=null?'@'+ln.renderStart+'s':'@?')
         +(ln.renderDur!=null?' ('+ln.renderDur+'s)':'');
       const pin=(ln.start!=null?'start='+ln.start+'s':(ln.gap?'gap='+ln.gap+'s':'auto'));
       const sid='dlg_s_'+m.index+'_'+ln.k, eid='dlg_e_'+m.index+'_'+ln.k;
       return `<div style="border-left:2px solid var(--line);padding:4px 0 4px 10px;margin:6px 0">
         <div class="row"><span class="muted">[${ln.k}] <b>${esc(ln.speaker)}</b> ${rs} · ${esc(pin)}${ln.expression?' · expr '+esc(ln.expression):''}</span></div>
         <div class="txt" style="font-size:13px">“${esc(ln.text)}”</div>
         <div class="bar">
           <input type="text" id="${sid}" placeholder="start s / auto" style="max-width:120px" value="${ln.start!=null?ln.start:''}">
           <input type="text" id="${eid}" placeholder="expression (- = none)" style="max-width:190px" value="${esc(ln.expression||'')}">
           <button class="ghost" onclick="dlgApply(${m.index},${ln.k},'${sid}','${eid}')" title="Retime / re-express this line">Apply line</button>
         </div></div>`;}).join('');
     return `<details style="margin-top:8px"><summary class="muted">🗣 dialogue timing — ${m.lines.length} lines</summary>
       <div class="hint">Set when each line STARTS (a number of seconds, or “auto” for back-to-back) and how it's said (expression). A timing-only change is FREE; changing an expression re-voices just that line.</div>${rows}</details>`;};
   h+=d.segments.map(m=>{
     if(m.native) return `<div class="seg"><span class="lbl">${esc(m.label)}</span> <span class="muted">seg${m.index} · clip audio (no voiceover)</span><div class="txt">${esc(m.text)}</div></div>`;
     if(m.sections&&m.sections.length){
       const rows=m.sections.map(sx=>`<div style="border-left:2px solid var(--line);padding:4px 0 4px 10px;margin:6px 0">
         <div class="row"><span class="muted">·s${sx.k}${sx.duration?' · '+sx.duration+'s':''}</span>${sx.recorded?'<span class="tag ok">🎙 your take</span>':''}</div>
         <div class="txt" style="font-size:13px">${esc(sx.say)}</div>
         ${sx.audio?`<audio controls src="${art(sx.audio)}"></audio>`:'<div class="hint">not voiced yet — record, or generate AI</div>'}
         <div class="bar"><button class="ghost" onclick="action('voice','reread',{index:${m.index},section:${sx.k}})" title="Re-generate just this section's AI voice">↻ Re-read</button>${recBtn(m.index,sx.k,sx.recorded)}</div></div>`).join('');
       return `<div class="seg"><span class="lbl">${esc(m.label)}</span>
         <span class="muted">seg${m.index} · ${m.sections.length} sections${m.duration?' · '+m.duration+'s':''}</span>
         <div class="hint">section-native — each shot voices + captions its own line</div>${rows}
         <div class="bar"><button data-act="voice:reread" data-i="${m.index}">↻ Re-read all (AI)</button>
           <button class="ghost" onclick="editText('voice:text',${m.index},${aj(m.text)})">Edit text</button></div>${dlg(m)}${history(m.versions)}</div>`;
     }
     return `<div class="seg"><span class="lbl">${esc(m.label)}</span>
       <span class="muted">seg${m.index}${m.duration?' · '+m.duration+'s':''}</span>${m.recorded?'<span class="tag ok">🎙 your take</span>':''}
       <div class="txt">${esc(m.text)}</div>
       ${m.audio?`<audio controls src="${art(m.audio)}"></audio>`:'<div class="hint">not voiced yet — record, or generate AI</div>'}
       <div class="bar">${recBtn(m.index,null,m.recorded)}
         <button data-act="voice:reread" data-i="${m.index}">↻ Re-read (AI)</button>
         <button class="ghost" onclick="editText('voice:text',${m.index},${aj(m.text)})">Edit text</button></div>${dlg(m)}${history(m.versions)}</div>`;
   }).join('');
   return h+accBar('voice','Accept voiceover →');
 },
 keyframes(d,busy){
   const nodes=d.nodes||[];
   const total=nodes.length;
   const missing=nodes.filter(n=>!n.img).length;
   let h=`<div class="card"><div class="row"><span class="pill">${total} shots</span>
     <span class="pill">${d.perKeyframe} cr each</span>${missing?`<span class="pill warnc">${missing} to generate ≈ ${missing*d.perKeyframe} cr</span>`:'<span class="pill ok">all generated</span>'}</div>
     <div class="bar"><button class="primary" data-act="keyframes:gen" ${missing?'':'disabled'}>Generate ${missing} missing</button></div>
     <div class="hint">Scroll the timeline →. Each line shows the shot a keyframe is <b>built from</b> (its reference): <span class="kfk cross">▬ matches another beat</span> &nbsp; <span class="kfk intra">▬ continues its own beat</span>. Click a shot to edit, upload, or regenerate.</div></div>`;
   // the scrollable shot TIMELINE (#2): one compact card per shot, in render
   // order; dependency lines (drawn by drawKfLines) connect each to its ref
   h+=`<div class="card kfwrap"><div id="kftl" class="kftl"><svg id="kfsvg"></svg>
       <div class="kftrack">`+nodes.map(kfNodeCard).join('')+`</div></div></div>`;
   // the detail editor for the selected shot
   const sel=nodes.find(n=>n.id===_kfSel)||nodes[0];
   h+=`<div class="card" id="kfdetail">`+(sel?kfDetail(sel,d):'<span class="muted">no shots yet — accept the direction first</span>')+`</div>`;
   return h+accBar('keyframes','Keyframes done →');
 },
 clips(d,busy){
   const nodes=d.nodes||[];
   const anim=nodes.filter(n=>n.animate);
   const made=nodes.filter(n=>n.clip).length;
   const todo=anim.filter(n=>!n.clip);
   const cost=todo.reduce((a,n)=>a+n.cost,0);
   let h=`<div class="card"><div class="row">
     <span class="pill warnc">${anim.length} animated shots</span>
     <span class="pill ok">${nodes.length-anim.length} free Ken Burns stills</span>
     <span class="pill">${made}/${nodes.length} clips made</span></div>
     <div class="bar"><button class="primary" data-act="clips:genall" ${todo.length?'':'disabled'}>Generate ${todo.length} planned ≈ ${cost} cr</button></div>
     <div class="hint">Scroll the shot timeline →. Every clip is built from that shot's keyframe; the detail shows the EXACT <b>video prompt</b> to copy/edit before spending. The director marked which shots need real motion — the rest pan/zoom free at assembly.</div></div>`;
   h+=`<div class="card kfwrap"><div id="cltl" class="kftl"><div class="kftrack">`+nodes.map(clipNodeCard).join('')+`</div></div></div>`;
   const sel=nodes.find(n=>n.id===_clipSel)||nodes[0];
   h+=`<div class="card" id="cldetail">`+(sel?clipDetail(sel,d):'<span class="muted">no shots yet — accept the keyframes first</span>')+`</div>`;
   return h+accBar('clips','Clips done →');
 },
 music(d,busy){ return musicCard(d)+accBar('music','Continue →'); },
 assemble(d,busy){
   if(!d.short){
     // PRE-BUILD: the whole cut is editable BEFORE the first assemble — speed,
     // subtitles/overlays, every beat's reveal/overlays/audio mode, music —
     // so the first build already renders the cut you designed (edits stage
     // into script.json instantly; nothing renders until you build)
     const pend0=d.pending||{changes:[]}; const n0=(pend0.changes||[]).length;
     const staged0=n0?`<div class="hint" style="margin-top:6px">already staged for the first cut: ${pend0.changes.map(esc).join(' · ')}${pend0.revoice?' · <span class="warnc">re-voices affected beats</span>':''}</div>`:'';
     return `<div class="card"><b>Not assembled yet</b> <span class="muted">— set the cut up first: everything below is editable now, and the first build (with the AI review) renders it exactly as you set it.</span>
       <div class="row" style="margin-top:10px">
         <span class="pill">subtitles <b>${d.captions?'ON':'OFF'}</b></span>
         <span class="pill">overlays <b>${d.overlays?'ON':'OFF'}</b></span>
         <span class="pill">voice <b>${d.speed}x</b></span>${n0?`<span class="pill warnc">${n0} staged</span>`:''}</div>
       <div class="bar">
         <button data-act="assemble:captions">Subtitles ${d.captions?'OFF':'ON'}</button>
         <button data-act="assemble:overlays">Overlays ${d.overlays?'OFF (plain cut)':'ON'}</button></div>
       <div class="bar">speed:
         ${[1,1.25,1.5,2].map(x=>`<button class="${d.speed==x?'primary':''}" data-act="assemble:speed" data-speed="${x}">${x}x</button>`).join('')}</div>
       ${staged0}
       <div class="bar"><button class="primary" data-act="assemble:build">🎬 Assemble + AI review</button></div></div>`
       +segEditor(d)+musicCard(d.music);
   }
   const r=d.review; let rv='';
   if(r){ rv=`<div class="card">${r.passed?'<b class="ok">✓ AI review passed</b>':'<b class="warnc">⚠ AI review — changes suggested</b>'} <span class="muted">(iter ${r.iters})</span>
     ${r.scores&&Object.keys(r.scores).length?`<div class="row" style="margin:6px 0">`+Object.entries(r.scores).map(([k,v])=>`<span class="pill">${esc(k)} <b>${esc(v)}/10</b></span>`).join('')+`</div>`:''}
     <div class="hint">${esc(r.summary||'')}</div>`+
     (r.fixes&&r.fixes.length?r.fixes.map(f=>`<div class="hint">• ${esc(f.segment)}: ${esc(f.problem)}</div>`).join(''):'')+
     `<div class="bar"><button class="ghost" data-act="assemble:reinforce">👍 Agree — bank as a standard</button>
       ${r.fixes&&r.fixes.length?`<button class="primary" data-send="assemble:apply_notes" data-src="ainotes">Regenerate flagged shots</button>`:''}</div>
       ${r.fixes&&r.fixes.length?fb('ainotes','optional: your own comments, merged into the regen (Enter = AI notes only)'):''}</div>`;
   }
   const pend=d.pending||{changes:[],revoice:false}; const np=(pend.changes||[]).length;
   // staged-changes banner: every edit writes script.json instantly but DEFERS
   // the rebuild — the preview stays the previous cut until the creator applies
   const banner = np? `<div class="card warnc" style="border-color:var(--warn,#caa23b)">
       <b>⚠ ${np} staged change${np===1?'':'s'}</b> <span class="muted">— saved, but the video below is still the PREVIOUS cut. Nothing rebuilds until you apply.</span>
       <div class="hint" style="margin:6px 0">${pend.changes.map(esc).join(' · ')}${pend.revoice?' · <span class="warnc">re-voices affected beats</span>':''}</div>
       <div class="bar"><button class="primary" onclick="applyAssemble(${np},${pend.revoice?1:0})">▶ Apply ${np} change${np===1?'':'s'} &amp; re-assemble</button></div></div>`:'';
   const buildBtn = np
     ? `<button class="primary" onclick="applyAssemble(${np},${pend.revoice?1:0})">▶ Apply ${np} &amp; re-assemble</button>`
     : `<button data-act="assemble:build">↻ Re-assemble</button>`;
   return banner+`<div class="card"><video class="vid" controls src="${art(d.short)}"></video>
     <div class="row" style="margin-top:10px"><span class="pill">${d.duration}s</span>
       <span class="pill">subtitles <b>${d.captions?'ON':'OFF'}</b></span>
       <span class="pill">overlays <b>${d.overlays?'ON':'OFF'}</b></span>
       <span class="pill">voice <b>${d.speed}x</b></span>${np?`<span class="pill warnc">${np} staged</span>`:''}</div>
     <div class="bar">
       <button data-act="assemble:captions">Subtitles ${d.captions?'OFF':'ON'}</button>
       <button data-act="assemble:overlays">Overlays ${d.overlays?'OFF (plain cut)':'ON'}</button>
       <button data-act="assemble:review">Re-run AI review</button>
       ${buildBtn}</div>
     <div class="bar">speed:
       ${[1,1.25,1.5,2].map(x=>`<button class="${d.speed==x?'primary':''}" data-act="assemble:speed" data-speed="${x}">${x}x</button>`).join('')}</div>
     ${d.iterations.length>1?`<div class="hint">AI-review iterations: ${d.iterations.map(v=>`<a href="/artifact/${v.name}?v=${v.mtime}" target="_blank">${esc(v.name)}</a>`).join(', ')}</div>`:''}
     ${history(d.history)}
     </div>`+segEditor(d)+rv+musicCard(d.music)+accBar('assemble','Accept the cut →');
 },
 thumbnail(d,busy){
   if(!d.img) return `<div class="card">No thumbnail yet — a bespoke cover (AI background + tease headline) the click is won on.
     <div class="bar"><button class="primary" data-act="thumbnail:gen">Generate the thumbnail · ${d.perImage}cr</button></div></div>`;
   return `<div class="card"><div class="row" style="align-items:flex-start">
       <div><img src="${art(d.img)}" style="max-width:300px;border-radius:12px;border:1px solid var(--line)" onclick="window.open(this.src)"><div class="hint" style="text-align:center">cover</div></div>
       ${d.teaser?`<div><img src="${art(d.teaser)}" style="max-width:160px;border-radius:12px;border:1px solid var(--line)" onclick="window.open(this.src)"><div class="hint" style="text-align:center">teaser</div></div>`:''}
     </div>
     <div class="hint" style="margin-top:8px">headline: <b>${esc(d.text||'(hook overlay)')}</b>${d.elements.length?` · <b>${d.elements.length} custom text element(s)</b> override it`:''}</div>
     ${d.concept?`<div class="hint">concept: ${esc(d.concept)}</div>`:''}
     ${d.prompt?`<details style="margin-top:8px"><summary class="hint" style="cursor:pointer">background prompt used</summary>
       <pre style="white-space:pre-wrap;color:var(--dim);font-size:12px;margin:8px 0">${esc(d.prompt)}</pre>
       <div class="hint">steer it via the concept below — the concept slots into this prompt</div></details>`:''}
     ${thumbElCard(d)}
     <div class="card" style="background:var(--panel2);margin-top:12px"><b>Headline</b> <span class="muted">(the default layout's bottom line — recomposes free; ignored while custom elements exist)</span>
       <input type="text" id="thl" placeholder="2-4 word tease headline" style="margin:8px 0">
       <div class="bar"><button class="primary" data-send="thumbnail:headline" data-src="thl" data-inline="thl">Update headline</button></div></div>
     <div class="card" style="background:var(--panel2)"><b>Background concept</b> <span class="muted">(regenerates · ${d.perImage}cr)</span>
       ${fb('thc','new background concept — the hero shot, no text')}
       <div class="bar"><button class="primary" data-send="thumbnail:concept" data-src="thc">Regenerate with concept</button>
         <button data-act="thumbnail:regen">↻ Regenerate (${d.perImage}cr)</button></div></div>
     ${history(d.versions)}</div>`+accBar('thumbnail','Accept thumbnail →');
 },
 seo(d,busy){
   if(!d.seo) return `<div class="card">No metadata yet.<div class="bar"><button class="primary" data-act="seo:gen">Write SEO</button></div></div>`;
   const m=d.seo;
   return `<div class="card"><div><b>${esc(m.title)}</b> <span class="muted">(${m.title.length} chars)</span></div>
     <pre style="white-space:pre-wrap;color:var(--dim);margin:10px 0">${esc(m.description)}</pre>
     <div>${(m.hashtags||[]).map(t=>`<span class="tag">${esc(t)}</span>`).join('')}</div>
     <div style="margin-top:6px">${(m.tags||[]).map(t=>`<span class="tag muted">${esc(t)}</span>`).join('')}</div>
     <div class="hint">lint: ${d.lint.length?`<span class="bad">${esc(d.lint.join('; '))}</span>`:'<span class="ok">clean ✓</span>'}</div></div>
     <div class="card"><b>Notes for the SEO writer</b>${fb('seofb','e.g. punchier title, add a keyword…')}
       <div class="bar"><button class="primary" data-send="seo:feedback" data-src="seofb">Apply notes</button>
         <button data-act="seo:regen">↻ Regenerate</button>
         <button class="ghost" onclick="editJson('seo.json','seo:save')">Edit JSON</button></div></div>`+accBar('seo','Accept metadata →');
 },
 gates(d,busy){
   if(!d.result) return `<div class="card">Gates not run yet.<div class="bar"><button class="primary" data-act="gates:run">Score + run gates</button></div></div>`;
   const r=d.result, v=r.virality||{};
   const vc={weak:'bad',ok:'ok',strong:'ok'}[v.status]||'warnc';
   let h=`<div class="card">
     <div>tech QC: ${r.qc&&r.qc.length?`<span class="warnc">${esc(r.qc.join('; '))}</span>`:'<span class="ok">clean ✓</span>'}</div>
     <div>virality: <span class="${vc}">${esc(v.status||'?')}</span> ${esc(JSON.stringify(v.scores||''))} <span class="muted">${esc(v.why||'')}</span></div>
     <div>SEO lint: ${r.seo&&r.seo.length?`<span class="warnc">${esc(r.seo.join('; '))}</span>`:'<span class="ok">clean ✓</span>'}</div>
     <div class="bar"><button data-act="gates:run">↻ Re-check</button></div></div>`;
   if(v.status==='weak'&&d.retries<d.maxRetries)
     h+=`<div class="card warnc">Weak hook. <div class="bar"><button class="primary" data-act="gates:rehook">Rewrite hook + redo seg0 (~15–25 cr)</button></div></div>`;
   return h+accBar('gates','Accept gates →');
 },
 wrap(d,busy){
   return `<div class="card"><b>Upload checklist</b>
     <div class="kv" style="margin-top:8px">
       <div class="k">video</div><div><a href="/artifact/short.mp4" target="_blank">short.mp4</a></div>
       <div class="k">metadata</div><div>${esc(d.seo)}</div>
       <div class="k">report</div><div>${esc(d.report)}</div>
       ${d.hasMusic?'<div class="k">music</div><div>credit note in seo.md</div>':''}</div>
     <div class="bar"><button class="primary" data-act="wrap:finalize">Write report &amp; mark done</button></div></div>
     <div class="card"><b>Anything you learned?</b> <span class="muted">(${d.learned} critique(s) already saved this session)</span>
       ${fb('lnote','a takeaway — distilled into the global learnings if transferable')}
       <div class="bar"><button class="primary" data-send="wrap:learn" data-src="lnote">Save &amp; finish</button></div></div>`;
 },
};

function history(vers){
  if(!vers||!vers.length) return '';
  return `<details><summary>history · ${vers.length} kept (.versions/)</summary><div class="vh">`+
    vers.slice().reverse().map(v=>{
      const u='/artifact/'+v.file, ext=v.file.split('.').pop().toLowerCase();
      let prev='';
      if(['png','jpg'].includes(ext))prev=`<img src="${u}" onclick="window.open(this.src)">`;
      else if(ext==='mp4')prev=`<video src="${u}" controls muted></video>`;
      else if(['mp3','wav','m4a'].includes(ext))prev=`<audio controls src="${u}"></audio>`;
      else prev=`<a href="${u}" target="_blank">view</a>`;
      return `<div class="vrow">${prev}<div class="vmeta"><span class="muted">${esc(v.ts)}</span>
        <button class="ghost" onclick="action('versions','restore',{file:${JSON.stringify(v.file)}})">↩ Restore this</button></div></div>`;
    }).join('')+`</div></details>`;
}

function musicCard(d){
  if(!d) return '';
  if(!d.tracks||!d.tracks.length) return `<div class="card muted">No music tracks in the channel's assets/music/.</div>`;
  return `<div class="card"><b>Background music</b> <span class="muted">(current: ${esc(d.current)})</span>
    <div class="pillrow" style="margin-top:8px">
      <span class="tag ${d.current==='none'?'sel':''}" onclick="action('music','set',{track:'none'})">none</span>
      ${d.tracks.map(t=>`<span class="tag ${d.current===t?'sel':''}" onclick="action('music','set',{track:'${esc(t)}'})">${esc(t)}</span>`).join('')}
    </div></div>`;
}

// confirm + apply the staged assemble changes as ONE re-assemble
function applyAssemble(n,revoice){
  let msg = n>0 ? ('Apply '+n+' staged change'+(n===1?'':'s')+' and re-assemble the video?') : 'Re-assemble the video now?';
  if(revoice) msg += '\n(re-voices the affected beats — a little slower)';
  if(!confirm(msg)) return;
  action('assemble','build');
}
// ---- per-segment assemble editor (browser equivalent of the terminal [g]) ----
// every edit writes script.json instantly + STAGES a change; the rebuild waits
// for Apply (applyAssemble) so a burst of edits costs ONE re-assemble
async function segAct(i,field,value){ action('assemble','seg',{index:i,field,value:value===undefined?null:value}); }
// on-screen text overlays + reveal timing: ALL edits stay LOCAL — type freely
// across every beat, then ONE Save stages everything (owner call 2026-07-04:
// saving each row was irritating). Toggles auto-flush your edits first.
function asmDirty(){ ASM_DIRTY=true;
  const b=document.getElementById('asmSave');
  if(b){ b.classList.add('primary'); b.textContent='💾 Save edits ●'; } }
function collectAsm(){
  const out=[];
  document.querySelectorAll('.segrow[data-seg]').forEach(r=>{
    const i=parseInt(r.dataset.seg,10); if(isNaN(i))return;
    const ovs=[...r.querySelectorAll('.ovrow.ovdata')].map(row=>{
      const g=c=>{const e=row.querySelector('input.'+c);return e?e.value.trim():'';};
      return {text:g('ovtext'),pos:g('ovpos'),start:g('ovs'),end:g('ove')};
    }).filter(o=>o.text);
    const rv=r.querySelector('input.rvin');
    out.push({index:i,reveal:rv?rv.value.trim():'',overlays:ovs});
  });
  return out;
}
async function flushAsm(){ if(!ASM_DIRTY)return; ASM_DIRTY=false;
  await api('/api/action',{stage:'assemble',action:'ov_bulk',segs:collectAsm()}); }
function saveAsm(){ if(!ASM_DIRTY){toast('no unsaved edits');return;}
  ASM_DIRTY=false; action('assemble','ov_bulk',{segs:collectAsm()}); }
function ovKill(btn){ btn.closest('.ovrow').remove(); asmDirty(); }
function ovAdd(i){ const e=document.getElementById('ovnew'+i); const t=e?e.value.trim():'';
  if(!t){toast('type the overlay text first');return;}
  e.closest('.ovrow').insertAdjacentHTML('beforebegin',ovRow(i,'n'+(OVN++),{text:t}));
  e.value=''; asmDirty(); }
function ovRow(i,k,o){
  return `<div class="ovrow ovdata">
    <input type="text" value="${esc(o.text||'')}" placeholder="overlay text" class="ovtext" oninput="asmDirty()">
    <input type="text" value="${esc(o.pos||'')}" placeholder="top-center" class="ovpos" title="top/middle/bottom [+ left/center/right]" oninput="asmDirty()">
    <input type="text" value="${o.start==null?'':o.start}" placeholder="start%" class="ovnum ovs" title="start at % of the beat (blank = default)" oninput="asmDirty()">
    <input type="text" value="${o.end==null?'':o.end}" placeholder="end%" class="ovnum ove" title="end at % of the beat (blank = hold to end)" oninput="asmDirty()">
    <button class="ghost bad" onclick="ovKill(this)" title="remove this overlay (saved with 💾)">✕</button></div>`;
}
function segEditor(d){
  const segs=d.segments||[]; if(!segs.length) return '';
  const rows=segs.map(s=>{
    // text-reveal timing (% of the beat; blank = default early)
    const reveal = `reveal <input type="text" class="rvin" value="${s.reveal==null?'':s.reveal}" placeholder="def" style="width:48px;padding:3px 5px;font-size:12px" oninput="asmDirty()" title="text-reveal start, % of the beat — saved with 💾"><span class="muted" style="font-size:11px">%</span>`;
    // clip-only controls
    const clip = s.hasClip ? `
      <button class="ghost" onclick="segAct(${s.i},'fill')" title="slow→loop→boomerang→freeze">fill: ${esc(s.fill||'')}</button>
      <button class="ghost ${s.native?'on':''}" onclick="segAct(${s.i},'native')" title="use the clip's own audio, skip TTS">native ${s.native?'ON':'off'}</button>
      <button class="ghost ${s.full?'on':''}" onclick="segAct(${s.i},'full')" title="let the whole clip play out">full ${s.full?'ON':'off'}</button>
      <button class="ghost ${s.mix?'on':''}" onclick="segAct(${s.i},'mix')" title="mix clip sfx under the voiceover">mix ${s.mix?'ON':'off'}</button>` : '';
    const ovs=(s.overlayList||[]).map((o,k)=>ovRow(s.i,k,o)).join('');
    const overlays=`<div class="ovbox"><div class="ovhd muted">on-screen text — text · position · start%→end%</div>
      ${ovs||'<div class="muted" style="font-size:11px;padding:2px 0">(none yet)</div>'}
      <div class="ovrow"><input id="ovnew${s.i}" type="text" placeholder="+ add an overlay…" class="ovtext">
        <button class="ghost" onclick="ovAdd(${s.i})">add</button></div></div>`;
    return `<div class="segrow" data-seg="${s.i}"><div class="seghd"><b>${s.i+1}· ${esc(s.label)}</b><span class="muted" style="font-size:11px">${s.overlays} overlay${s.overlays===1?'':'s'}</span></div>
      <div class="segctl">${reveal} ${clip}</div>${overlays}</div>`;
  }).join('');
  return `<div class="card"><div class="row"><b>Per-segment</b>
      <span class="muted" style="flex:1">— edit text, timing and reveals across EVERY beat freely, then save once. Toggles (fill/audio) apply instantly and keep your unsaved edits.</span>
      <button id="asmSave" onclick="saveAsm()">💾 Save edits</button></div>
    <div class="seglist">${rows}</div></div>`;
}
function bind(){
  document.querySelectorAll('[data-act]').forEach(b=>b.onclick=()=>{
    const [stage,a]=b.dataset.act.split(':');
    const ex={}; if(b.dataset.i!=null)ex.index=b.dataset.i; if(b.dataset.speed)ex.speed=b.dataset.speed;
    if(b.dataset.plat)ex.platform=b.dataset.plat;
    if(b.dataset.sec!=null)ex.section=b.dataset.sec; if(b.dataset.regen)ex.regen=1;
    action(stage,a,ex);
  });
  document.querySelectorAll('[data-send]').forEach(b=>b.onclick=()=>{
    const [stage,a]=b.dataset.send.split(':');
    const el=document.getElementById(b.dataset.src);
    const text=el?el.value.trim():'';
    if(!text&&b.dataset.inline){toast('enter a value first');return;}
    const ex={text}; if(b.dataset.i!=null)ex.index=b.dataset.i;
    if(b.dataset.sec!=null)ex.section=b.dataset.sec;
    action(stage,a,ex);
  });
  // draw the keyframe-timeline dependency lines once the cards are laid out
  if(document.querySelector('.kftrack')) requestAnimationFrame(drawKfLines);
}
// voice stage: engine/voice pickers + audition
function voiceEngine(name){
  if(!S.data||name===S.data.engine)return;
  if(confirm('Switch the TTS engine to '+name+'? The AI voiceover regenerates in the new engine (recorded takes are kept).'))
    action('voice','engine',{engine:name});
  else{ const s=document.getElementById('vEng'); if(s)s.value=S.data.engine; }
}
async function voiceSample(){
  const v=(val('vname').trim()||val('vSel'));
  if(!v){toast('pick a voice first');return;}
  toast('synthesizing the sample…');
  // straight fetch, NOT action(): a sample must not refresh state — that
  // would rebuild the panel and reset the dropdown mid-audition
  const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({stage:'voice',action:'sample',text:v})});
  const d=await r.json();
  if(!r.ok){toast(d.error||'sample failed');return;}
  if(d.result&&d.result.sampleUrl)new Audio(d.result.sampleUrl).play();
}
function voiceSet(){
  const v=(val('vname').trim()||val('vSel'));
  if(!v){toast('pick a voice first');return;}
  action('voice','setvoice',{text:v});
}
function editText(act,index,cur,regen,section){
  const [stage,a]=act.split(':');
  const v=prompt('Enter text (use "-" to clear):',cur||'');
  if(v===null)return;
  const ex={index,text:v,regen:regen!==false};
  if(section!=null)ex.section=section;
  action(stage,a,ex);
}
function dlgApply(index,line,sid,eid){
  // send only the fields the user actually touched (blank start = keep,
  // blank expression = keep; the backend treats "-" as "clear")
  const sEl=document.getElementById(sid), eEl=document.getElementById(eid);
  const ex={index,line};
  const sv=(sEl?sEl.value:'').trim();  if(sv!=='')ex.start=sv;
  if(eEl)ex.expression=eEl.value.trim();   // "" keeps, "-" clears
  if(ex.start===undefined&&(ex.expression===''||ex.expression===undefined)){
    toast('nothing to change on this line'); return; }
  action('voice','dialogue',ex);
}
function switchTemplate(name){
  if(name===S.template){return;}
  // ask each time: regenerate the old-look visuals, or keep them as-is
  const regen=confirm('Switch template to "'+name+'"?\n\nOK = adopt the new look: the DIRECTOR re-runs in the "'+name+'" world so the prompts + visuals re-style, and the old keyframes / clips / thumbnail are archived to regenerate. (Re-plans the shots; the spoken script + facts stay locked.)\nCancel = switch models/mechanics only — keep the current direction, visuals and prompts (the look may be mixed until you regen).\n\nYour voice / engine / speed / length pins carry over either way.');
  action('global','template',{name,regen:regen?1:0});
}
// ---- keyframe timeline (#2) ----
let _kfSel=null;
const kfNodeCard=n=>`<div class="kfn${n.img?'':' empty'}${n.id===_kfSel?' sel':''}" data-node="${n.id}" style="--h:${(n.seg*47)%360}" onclick="selectKfNode('${n.id}')" title="${esc(n.label)} · shot ${n.shotNo}">
   <div class="kfn-h">${n.main?`<span class="kfn-seg">${n.seg+1}· ${esc(n.label)}</span>`:`<span class="kfn-sub">shot ${n.shotNo}</span>`}</div>
   <div class="kfn-th">${n.img?`<img src="${art(n.img)}">`:'<span class="muted">— not made</span>'}${n.stale?'<span class="kfstar" title="the prompt that generated this image has changed — regenerate to match">⭐</span>':''}</div>
   <div class="kfn-f">${n.override?'<span class="kfn-tag">custom</span>':''}${n.depKind==='fresh'?'<span class="kfn-tag fresh">fresh</span>':''}</div>
 </div>`;
function kfDetail(n,d){
  const single=(n.sub===null||n.sub===undefined);
  const pid='kp_'+n.id.replace(/\\W/g,'_');
  const gen = single
    ? (n.img?`<button data-act="keyframes:regen" data-i="${n.seg}">↻ regenerate ${d.perKeyframe}cr</button>`
            :`<button class="primary" data-act="keyframes:one" data-i="${n.seg}">Generate ${d.perKeyframe}cr</button>`)
    : (n.img?`<button data-act="keyframes:subgen" data-i="${n.seg}" data-sec="${n.sub}" data-regen="1">↻ regenerate ${d.perKeyframe}cr</button>`
            :`<button class="primary" data-act="keyframes:subgen" data-i="${n.seg}" data-sec="${n.sub}">Generate ${d.perKeyframe}cr</button>`);
  const note = single?`editText('keyframes:tweak',${n.seg},'',true)`
                     :`editText('keyframes:subtweak',${n.seg},'',true,${n.sub})`;
  const up = single?`uploadKeyframe(${n.seg},null)`:`uploadKeyframe(${n.seg},${n.sub})`;
  const promptSend = `data-send="keyframes:prompt" data-src="${pid}" data-i="${n.seg}"`+(single?'':` data-sec="${n.sub}"`);
  const autoPrompt = single?`{index:${n.seg},text:'-'}`:`{index:${n.seg},section:${n.sub},text:'-'}`;
  const depTag = n.dep?`<span class="tag">${esc(n.depLabel)}</span>`
                      :`<span class="tag">${esc(n.depLabel||'no reference')}</span>`;
  return `<div class="kfd">
    <div class="kfd-img">${n.img?`<img src="${art(n.img)}" onclick="window.open(this.src)">`:'<div class="kfd-ph muted">not generated yet</div>'}</div>
    <div class="kfd-body">
      <div class="row" style="gap:8px"><b>${n.seg+1}· ${esc(n.label)}</b><span class="muted">shot ${n.shotNo}${n.main?' · main':''}</span>${n.override?'<span class="tag ok">custom prompt</span>':''}</div>
      <div class="hint" style="display:flex;align-items:center;gap:6px;margin:6px 0">↳ built from: ${n.refArt?`<img src="${art(n.refArt)}" style="width:26px;border-radius:3px" onclick="window.open(this.src)">`:''}${depTag}</div>
      ${n.stale?`<div class="hint warnc">⭐ The prompt that generated this image has CHANGED since it was made (a rewrite kept the picture instead of deleting it) — it may no longer match the plan. ↻ Regenerate to update; the star clears itself.</div>`:''}
      ${n.extra?`<div class="hint">note: ${esc(n.extra)}</div>`:''}
      <div class="bar">${gen}
        <button class="ghost" onclick="${note}">✎ Note + regen</button>
        <button class="ghost" onclick="${up}">⬆ Upload your image</button></div>
      <details class="kp"><summary>${n.override?'✎ custom prompt (yours)':'prompt'} — view / copy / edit</summary>
        <textarea id="${pid}" rows="6" style="width:100%;margin-top:6px;font-size:12px">${esc(n.prompt||'')}</textarea>
        <div class="bar"><button class="ghost" onclick="copyEl('${pid}')">⧉ Copy</button>
          <button ${promptSend} title="Send this exact prompt next generation">Use my prompt → regen</button>
          ${n.override?`<button class="ghost" onclick="action('keyframes','prompt',${autoPrompt})" title="Back to the auto prompt">↺ Auto prompt</button>`:''}</div></details>
      ${history(n.versions)}
    </div></div>`;
}
function selectKfNode(id){
  _kfSel=id;
  const tl=$('#kftl'); const sl=tl?tl.scrollLeft:0;   // keep timeline scroll
  render();
  const tl2=$('#kftl'); if(tl2)tl2.scrollLeft=sl;
}
function drawKfLines(){
  const svg=$('#kfsvg'), track=document.querySelector('.kftrack');
  if(!svg||!track||!S||!S.data||!S.data.nodes)return;
  const pos={};
  track.querySelectorAll('.kfn').forEach(el=>{
    pos[el.getAttribute('data-node')]={l:el.offsetLeft,t:el.offsetTop,w:el.offsetWidth,h:el.offsetHeight};
  });
  const W=track.scrollWidth, H=track.offsetHeight;
  let p=`<defs><marker id="kfar" markerWidth="7" markerHeight="7" refX="5" refY="3" orient="auto"><path d="M0,0 L6,3 L0,6 Z" fill="currentColor"/></marker></defs>`;
  S.data.nodes.forEach(n=>{
    if(!n.dep||!pos[n.id]||!pos[n.dep])return;
    if(n.depKind!=='cross'&&n.depKind!=='intra')return;
    const c=pos[n.id], a=pos[n.dep];
    const cx=c.l+c.w/2, cy=c.t;             // child top-center
    const px=a.l+a.w/2, py=a.t;             // parent top-center
    const dist=Math.abs(cx-px);
    const arc=Math.max(16,Math.min(64,dist*0.16));
    const top=Math.min(cy,py)-arc;
    const cls=n.depKind;
    p+=`<path class="kfline ${cls}" d="M${cx},${cy} C${cx},${top} ${px},${top} ${px},${py}" fill="none" marker-end="url(#kfar)"/>`;
  });
  svg.setAttribute('width',W); svg.setAttribute('height',H);
  svg.setAttribute('viewBox',`0 0 ${W} ${H}`);
  svg.innerHTML=p;
}
// ---- clip timeline (per-shot, mirrors the keyframe timeline) ----
let _clipSel=null;
const clipNodeCard=n=>`<div class="kfn${n.clip?'':(n.animate?' need':' empty')}${n.id===_clipSel?' sel':''}" data-node="${n.id}" style="--h:${(n.seg*47)%360}" onclick="selectClipNode('${n.id}')" title="${esc(n.label)} · shot ${n.shotNo} · ${n.dur}s">
   <div class="kfn-h">${n.main?`<span class="kfn-seg">${n.seg+1}· ${esc(n.label)}</span>`:`<span class="kfn-sub">shot ${n.shotNo}</span>`}</div>
   <div class="kfn-th">${n.img?`<img src="${art(n.img)}">`:'<span class="muted">— no key</span>'}${n.clip?'<span class="kfn-badge">🎬</span>':''}${n.stale?'<span class="kfstar" title="the prompt that generated this clip has changed — regenerate to match">⭐</span>':''}</div>
   <div class="kfn-f">${n.clip?'<span class="kfn-tag clipmade">clip</span>':(n.animate?`<span class="kfn-tag need">▶ ${n.cost}cr</span>`:'<span class="kfn-tag still">still</span>')}${n.override?'<span class="kfn-tag">custom</span>':''}</div>
 </div>`;
function clipDetail(n,d){
  const single=(n.sub===null||n.sub===undefined);
  const pid='cp_'+n.id.replace(/\\W/g,'_');
  const sub=single?'':` data-sec="${n.sub}"`;
  const autoArg=single?`{index:${n.seg},text:'-'}`:`{index:${n.seg},section:${n.sub},text:'-'}`;
  const note=single?`editText('clips:tweak',${n.seg},${aj(n.motion_extra||'')},true)`
                   :`editText('clips:tweak',${n.seg},${aj(n.motion_extra||'')},true,${n.sub})`;
  // toggle: render this beat as a free Ken Burns still vs a generated video
  const stillBtn=`<button class="ghost ${n.useStill?'on':''}" data-act="clips:still" data-i="${n.seg}"${sub} title="Use a free Ken Burns still instead of a generated video — ignores any clip on disk (kept), takes effect on re-assemble">${n.useStill?'▶ Use video':'🎞 Use still'}</button>`;
  let gen;
  if(n.useStill) gen=`<span class="tag ok">🎞 still (forced) — free</span>`;
  else if(n.clip) gen=`<button data-act="clips:regen" data-i="${n.seg}"${sub}>↻ regenerate · ${n.cost}cr</button>`;
  else if(n.animate) gen=`<button class="primary" data-act="clips:gen" data-i="${n.seg}"${sub}>Generate · ${n.cost}cr</button>`;
  else gen=`<span class="tag ok">free Ken Burns still</span> <button data-act="clips:gen" data-i="${n.seg}"${sub}>Animate anyway · ${n.cost}cr</button>`;
  const media=n.clip?`<video src="${art(n.clip)}" controls></video>`:(n.img?`<img src="${art(n.img)}" onclick="window.open(this.src)">`:'<div class="kfd-ph muted">no keyframe yet</div>');
  // a still with no dynamic clip_motion would animate as a mere zoom — offer to
  // write a real motion brief (the director, $0) before spending
  const needsBrief=!n.dynamic && !n.override;
  const briefRow=needsBrief?`<div class="hint warnc" style="margin:6px 0">⚠ No dynamic motion brief — this shot only has the gentle Ken&nbsp;Burns push, so a clip would just zoom. Write a real one first:
     <button class="ghost" data-act="clips:brief" data-i="${n.seg}">✨ Write dynamic motion brief ($0)</button></div>`:'';
  return `<div class="kfd">
    <div class="kfd-img">${media}</div>
    <div class="kfd-body">
      <div class="row" style="gap:8px"><b>${n.seg+1}· ${esc(n.label)}</b><span class="muted">shot ${n.shotNo}${n.main?' · main':''} · ${n.dur}s</span>${n.override?'<span class="tag ok">custom prompt</span>':(n.dynamic?'<span class="tag ok">dynamic brief</span>':'<span class="tag warnc">zoom only</span>')}${n.useStill?'<span class="tag ok">🎞 still (forced)</span>':(n.clip?'<span class="tag ok">clip ✓</span>':(n.animate?'<span class="tag warnc">not generated</span>':'<span class="tag">free still</span>'))}</div>
      <div class="hint">Built from this shot's keyframe. ${n.animate?'The director marked this for real motion.':"A still by default — animate only if it needs motion a Ken Burns pan can't fake. Generating one auto-writes a dynamic brief if it lacks one."}</div>
      ${n.stale?`<div class="hint warnc">⭐ The prompt that generated this clip has CHANGED since it was made — it may no longer match the current script/plan. ↻ Regenerate to update; the star clears itself.</div>`:''}
      ${briefRow}
      ${n.motion_extra?`<div class="hint">+ note: ${esc(n.motion_extra)}</div>`:''}
      <div class="bar">${gen}${stillBtn}<button class="ghost" onclick="${note}">✎ Note + regen</button>
        <button class="ghost" onclick="uploadClip(${n.seg},${single?'null':n.sub})" title="Upload your own video for this shot — conformed to 1080x1920, no credits">↥ Upload video ($0)</button></div>
      <details class="kp" open><summary>${n.override?'✎ custom video prompt (yours)':'video prompt'} — the EXACT brief sent to the model · copy / edit</summary>
        <textarea id="${pid}" rows="6" style="width:100%;margin-top:6px;font-size:12px">${esc(n.prompt||'')}</textarea>
        <div class="bar"><button class="ghost" onclick="copyEl('${pid}')">⧉ Copy</button>
          <button data-send="clips:prompt" data-src="${pid}" data-i="${n.seg}"${sub} title="Send this exact prompt — regenerates (costs credits)">Use my prompt → regen · ${n.cost}cr</button>
          ${n.override?`<button class="ghost" onclick="action('clips','prompt',${autoArg})" title="Back to the auto prompt">↺ Auto prompt</button>`:''}</div></details>
      ${history(n.versions)}
    </div></div>`;
}
function selectClipNode(id){
  _clipSel=id;
  const tl=$('#cltl'); const sl=tl?tl.scrollLeft:0;   // keep timeline scroll
  render();
  const tl2=$('#cltl'); if(tl2)tl2.scrollLeft=sl;
}
function uploadKeyframe(i,sec){
  const inp=document.createElement('input');
  inp.type='file'; inp.accept='image/*';
  inp.onchange=async()=>{
    const f=inp.files&&inp.files[0]; if(!f)return;
    const qs='kind=keyframe&index='+i+(sec===null?'':'&section='+sec);
    openLog(); $('#log').textContent='uploading image…'; $('#spin').style.display='inline-block';
    try{
      const r=await fetch('/api/upload?'+qs,{method:'POST',headers:{'Content-Type':f.type||'application/octet-stream'},body:f});
      const j=await r.json(); $('#spin').style.display='none';
      if(!r.ok){toast('upload failed: '+(j.error||r.status));return;}
      toast('keyframe saved'+((j.result&&j.result.has_clip)?' — a clip exists & still plays; regenerate it to use this image':''));
      await getState();
    }catch(e){$('#spin').style.display='none';toast('upload error');}
  };
  inp.click();
}
function uploadClip(i,sub){
  const inp=document.createElement('input');
  inp.type='file'; inp.accept='video/*';
  inp.onchange=async()=>{
    const f=inp.files&&inp.files[0]; if(!f)return;
    const qs='kind=clip&index='+i+(sub===null||sub===undefined?'':'&section='+sub);
    openLog(); $('#log').textContent='uploading & conforming video…'; $('#spin').style.display='inline-block';
    try{
      const r=await fetch('/api/upload?'+qs,{method:'POST',headers:{'Content-Type':f.type||'application/octet-stream'},body:f});
      const j=await r.json(); $('#spin').style.display='none';
      if(!r.ok){toast('upload failed: '+(j.error||r.status));return;}
      toast('clip saved — re-assemble to use it');
      await getState();
    }catch(e){$('#spin').style.display='none';toast('upload error');}
  };
  inp.click();
}
function copyEl(id){
  const el=document.getElementById(id); if(!el)return;
  el.select();
  (navigator.clipboard?navigator.clipboard.writeText(el.value):Promise.reject())
    .then(()=>toast('prompt copied')).catch(()=>{document.execCommand('copy');toast('prompt copied');});
}
// ---- record your own voice (MediaRecorder → /api/upload) ----
let _rec={mr:null,chunks:[],key:null};
async function recordUnit(index,section){
  const key=index+'_'+(section===null?'x':section);
  const btn=document.querySelector('[data-rec="'+key+'"]');
  if(_rec.mr&&_rec.key===key){ _rec.mr.stop(); return; }   // toggle stop
  if(_rec.mr){ try{_rec.mr.stop();}catch(e){} }
  let stream;
  try{ stream=await navigator.mediaDevices.getUserMedia({audio:true}); }
  catch(e){ toast('mic permission needed'); return; }
  const mr=new MediaRecorder(stream);
  _rec={mr,chunks:[],key};
  mr.ondataavailable=e=>{ if(e.data&&e.data.size)_rec.chunks.push(e.data); };
  mr.onstop=async()=>{
    stream.getTracks().forEach(t=>t.stop());
    const blob=new Blob(_rec.chunks,{type:'audio/webm'});
    _rec={mr:null,chunks:[],key:null};
    if(btn){ btn.classList.remove('on'); btn.textContent='● Record'; }
    await uploadRec(index,section,blob);
  };
  mr.start();
  if(btn){ btn.classList.add('on'); btn.textContent='■ Stop'; }
}
async function uploadRec(index,section,blob){
  const qs='index='+index+(section===null?'':'&section='+section);
  openLog(); $('#log').textContent='ingesting your recording…'; $('#spin').style.display='inline-block';
  try{
    const r=await fetch('/api/upload?'+qs,{method:'POST',headers:{'Content-Type':'audio/webm'},body:blob});
    const j=await r.json();
    $('#spin').style.display='none';
    if(!r.ok){ toast('upload failed: '+(j.error||r.status)); return; }
    const cap=j.result&&j.result.aligned?'word-synced karaoke':'section text held';
    toast('recorded '+(j.result?j.result.duration:'')+'s — captions: '+cap);
    await getState();
  }catch(e){ $('#spin').style.display='none'; toast('upload error'); }
}
function saveConcept(hasScript){
  const el=document.getElementById('cpt'); const text=el?el.value.trim():'';
  let rewrite=false;
  if(hasScript) rewrite=confirm('A script already exists. Rewrite the script + visuals for this concept? (Cancel = keep the current script, save the concept for next time)');
  action('concept','set',{text,rewrite});
}
function useSuggestion(text,hasScript){
  let rewrite=false;
  if(hasScript) rewrite=confirm('A script already exists. Rewrite the script + visuals for this concept? (Cancel = keep the current script)');
  action('concept','set',{text,rewrite});
}
function pullRef(){
  const url=((document.getElementById('clink')||{}).value||'').trim();
  if(!url){toast('paste a video URL');return;}
  const focus=((document.getElementById('cfocus')||{}).value||'').trim();
  action('concept','link',{url,focus});
}
async function editJson(file,act,key){
  const raw=await (await fetch('/artifact/'+file)).text();
  let obj=JSON.parse(raw);
  let edit=key?JSON.stringify(obj[key],null,2):raw;
  const v=prompt('Edit '+file+(key?' ('+key+')':'')+' — paste valid JSON:',edit);
  if(v===null)return;
  const [stage,a]=act.split(':');
  action(stage,a,{json:v});
}
// ---- launcher ----
$('#homeBtn').onclick=()=>goHome();
window.addEventListener('resize',()=>{if(document.querySelector('.kftrack'))drawKfLines();});
const ja=s=>String(s==null?'':s).replace(/\\/g,'\\\\').replace(/'/g,"\\'");
const val=id=>{const e=document.getElementById(id);return e?e.value:'';};
async function api(path,body){
  const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
  const d=await r.json(); if(!r.ok){toast(d.error||'error');return d;} return d; }
async function setChannel(name){ S=await api('/api/home/channel',{name}); render(); }
async function openS(channel,topic,mode,template,show){ const d=await api('/api/home/open',{channel,topic,mode,template:template||null,show:show||null}); if(d.error)return; S=d; render(); }
async function goHome(){ S=await api('/api/back',{}); render(); }
async function runTool(tool,topic){ const d=await api('/api/home/run',{tool,topic:topic||null}); if(d.kind==='job'){openLog(d.label);watch(d.jobId);} }
async function newSeries(){
  const title=((document.getElementById('serTitle')||{}).value||'').trim();
  const raw=((document.getElementById('serTopics')||{}).value||'').trim();
  const topics=raw.split(/[,\n]/).map(s=>s.trim()).filter(Boolean);
  if(!title||!topics.length){toast('need a series title + its parts (comma-separated topics, in order)');return;}
  const d=await api('/api/home/run',{tool:'series_new',args:{title,topics}});
  if(d.kind==='job'){openLog();watch(d.jobId);}
}
async function addToSeries(){
  const slug=((document.getElementById('serSlug')||{}).value||'').trim();
  const topic=((document.getElementById('serAddTopic')||{}).value||'').trim();
  if(!slug||!topic){toast('need the series slug + the new part topic');return;}
  const d=await api('/api/home/run',{tool:'series_add',args:{slug,topics:[topic]}});
  if(d.kind==='job'){openLog();watch(d.jobId);}
}
function homeShow(){ return ((document.getElementById('newShow')||{}).value||'')||null; }
// the queue dropdown follows the selected SHOW — each pending row is tagged
// with the show it was queued under ('' = the channel base format). Picking a
// row just FILLS the topic field (one start path, not a second parallel one).
function fillQueueSel(){
  const sel=document.getElementById('queueSel'); if(!sel)return;
  const show=homeShow()||'';
  const mine=(S.queue||[]).filter(it=>(it.show||'')===show);
  const label=show?show:((S.channelTitle||'channel')+' base');
  sel.style.display=mine.length?'':'none';
  sel.innerHTML=`<option value="">📋 pick from the ${esc(label)} queue (${mine.length})…</option>`
    +mine.map(it=>`<option value="${ja(it.topic)}">${esc(it.topic)}${it.concept?' · has concept':''}</option>`).join('');
}
function pickQueued(){
  const sel=document.getElementById('queueSel'); if(!sel||!sel.value)return;
  const i=document.getElementById('newTopic'); if(i)i.value=sel.value;
}
function updateStartBtns(){
  const el=document.getElementById('startBtns'); if(!el)return;
  el.innerHTML=`<button class="go" onclick="newRun('video')">🎬 Video</button>`;
}
function ideaChips(){
  const I=S.ideas; if(!I||!(I.ideas||[]).length)return '';
  const queued=new Set((S.queue||[]).map(it=>it.topic));
  const src=I.show?`for 📺 ${esc(I.show)}`:'for the channel base';
  const seed=I.seed?` · refined from “${esc(I.seed)}”`:'';
  return `<div class="card" style="margin-top:8px"><div class="lbl">✨ AI topic ideas ${src}${seed}</div>
    ${(I.ideas||[]).map(it=>`<div class="seg"><b>${esc(it.topic)}</b>${queued.has(it.topic)?' <span class="tag ok">queued ✓</span>':''}
      <div class="hint">${esc(it.why||'')}</div>
      <div class="bar"><button class="mini" onclick="useIdea('${ja(it.topic)}')">→ use as topic</button>
      ${queued.has(it.topic)?'':`<button class="mini" onclick="queueIdea('${ja(it.topic)}')">➕ queue</button>`}</div></div>`).join('')}
    <div class="sub">generate again anytime — a new batch replaces this one</div></div>`;
}
async function suggestTopics(){
  const seed=((document.getElementById('ideaSeed')||{}).value||'').trim();
  const d=await api('/api/home/run',{tool:'ideas',args:{show:homeShow(),seed:seed||null}});
  if(d.kind==='job'){openLog();watch(d.jobId);}   // done → state refresh shows the chips
}
function useIdea(t){ const i=document.getElementById('newTopic'); if(i){i.value=t;i.focus();} toast('topic set — pick the format and start'); }
async function queueIdea(t){
  const d=await api('/api/home/run',{tool:'queue_topic',args:{topic:t,show:(S.ideas&&S.ideas.show)||homeShow()}});
  if(d.error)return;
  toast(d.queued?('queued '+d.topic):(d.topic+' was already queued'));
  await getState();
}
// ---- new-channel form: describe the idea → ✨ AI designs the whole channel
// (premise, persona, beats, voice/visual rules, SEO) → review/edit → scaffold.
// The quick path (name + premise, no AI) still works from the same form.
function ncSegRow(sg){
  sg=sg||{};
  return `<div class="row ncsegrow" style="margin:6px 0;align-items:flex-start">
    <input class="ncs-l" value="${av(sg.label||'')}" placeholder="LABEL" style="max-width:110px;text-transform:uppercase"/>
    <input class="ncs-w" value="${sg.max_words!=null?sg.max_words:''}" placeholder="max words" style="max-width:90px" title="cap for this beat (optional)"/>
    <textarea class="ncs-g" placeholder="guidance — what this beat must do, written to the scriptwriter" style="flex:1;min-height:40px">${esc(sg.guidance||'')}</textarea>
    <button class="mini" onclick="this.closest('.ncsegrow').remove()" title="remove this beat">✕</button></div>`;
}
function ncAddSeg(){ document.getElementById('ncSegs').insertAdjacentHTML('beforeend',ncSegRow()); }
function newChannelForm(){
  const D=S.channelDraft;
  const slug=s=>(s||'').toLowerCase().trim().replace(/[^a-z0-9]+/g,'-').replace(/^-|-$/g,'');
  let h=`<div class="row" style="margin-top:8px">
      <input id="ncSeed" placeholder="describe the channel idea — niche, angle, audience (e.g. 'underdog football matches retold like thrillers')" style="flex:1" value="${av(D?D.seed:'')}"/>
      <button class="tool" onclick="channelDesign()">✨ ${D?'Redesign':'Design it with AI'} ($0)</button></div>`;
  if(!D){
    return h+`<div class="sub" style="margin:6px 0">…or skip the AI and scaffold bare:</div>
      <div class="row"><input id="ncName" placeholder="folder name, e.g. cooking"/>
        <input id="ncTitle" placeholder="display title (optional)"/></div>
      <textarea id="ncPremise" placeholder="premise — what this channel does, one video at a time (optional; everything is editable in channel.yaml later)"></textarea>
      <div class="bar"><button class="good" onclick="channelNew()">Scaffold the channel</button></div>
      <div class="sub">creates channels/&lt;name&gt;/ with a commented channel.yaml (beat structure, persona, SEO) + a default template — then add looks with “new template” below</div>`;
  }
  const seo=D.seo||{};
  return h+`<div class="hint" style="margin:8px 0">the AI's draft — every field is yours to edit before scaffolding:</div>
    <div class="row"><input id="ncName" value="${av(slug(D.title))}" placeholder="folder name"/>
      <input id="ncTitle" value="${av(D.title||'')}" placeholder="display title"/>
      <input id="ncNoun" value="${av(D.topic_noun||'topic')}" placeholder="topic noun (what ONE video covers)" style="max-width:200px"/></div>
    <textarea id="ncPremise" placeholder="premise">${esc(D.premise||'')}</textarea>
    <textarea id="ncPersona" placeholder="persona — the narrator">${esc(D.persona||'')}</textarea>
    <div class="hint" style="margin-top:8px">the beat structure (segments) — label · optional word cap · guidance to the writer:</div>
    <div id="ncSegs">${(D.segments||[]).map(ncSegRow).join('')}</div>
    <div class="bar"><button class="mini" onclick="ncAddSeg()">➕ add a beat</button></div>
    <div class="row" style="margin-top:8px;align-items:flex-start">
      <label class="hint" style="flex:1">voice rules (one per line)<textarea id="ncVoice">${esc((D.voice_rules||[]).join('\n'))}</textarea></label>
      <label class="hint" style="flex:1">visual rules (one per line)<textarea id="ncVisual">${esc((D.visual_rules||[]).join('\n'))}</textarea></label></div>
    <div class="row" style="margin-top:8px">
      <input id="ncHashtags" value="${av((seo.base_hashtags||[]).join(', '))}" placeholder="base hashtags, comma-separated"/>
      <input id="ncTags" value="${av((seo.base_tags||[]).join(', '))}" placeholder="base search tags, comma-separated"/></div>
    <div class="bar"><button class="good" onclick="channelNew()">Scaffold “${esc(D.title||'')}”</button>
      <button onclick="channelDraftDiscard()">✕ discard draft</button></div>
    <div class="sub">writes a complete channel.yaml from these sections + a default template — refine the yaml anytime</div>`;
}
async function channelDesign(){
  const seed=val('ncSeed').trim();
  if(!seed){toast('describe the channel idea first');return;}
  const d=await api('/api/home/run',{tool:'channel_design',args:{seed}});
  if(d.kind==='job'){openLog(d.label);watch(d.jobId);}   // done → draft in state
}
async function channelDraftDiscard(){
  const d=await api('/api/home/run',{tool:'channel_draft_discard',args:{}});
  if(!d.error)await getState();
}
async function channelNew(){
  const name=val('ncName').trim();
  if(!name){toast('give the channel a folder name');return;}
  const args={name,title:val('ncTitle').trim(),premise:val('ncPremise').trim()};
  if(S.channelDraft){
    args.persona=val('ncPersona').trim();
    args.topic_noun=val('ncNoun').trim();
    args.segments=[...document.querySelectorAll('.ncsegrow')].map(r=>({
      label:r.querySelector('.ncs-l').value.trim(),
      max_words:r.querySelector('.ncs-w').value.trim(),
      guidance:r.querySelector('.ncs-g').value.trim()})).filter(s=>s.label&&s.guidance);
    args.voice_rules=val('ncVoice').split('\n').map(s=>s.trim()).filter(Boolean);
    args.visual_rules=val('ncVisual').split('\n').map(s=>s.trim()).filter(Boolean);
    args.seo={base_hashtags:val('ncHashtags').split(',').map(s=>s.trim()).filter(Boolean),
              base_tags:val('ncTags').split(',').map(s=>s.trim()).filter(Boolean)};
  }
  const d=await api('/api/home/run',{tool:'channel_new',args});
  if(d.error)return;
  toast('scaffolded channels/'+d.channel+' — it is selected; refine channel.yaml anytime');
  await getState();
}
async function templateNew(){
  const name=val('ntName').trim(), world=val('ntWorld').trim(), style=val('ntStyle').trim();
  if(!name||!world||!style){toast('a template needs a name, a world and a style');return;}
  const d=await api('/api/home/run',{tool:'template_new',
    args:{name,description:val('ntDesc').trim(),world,style}});
  if(d.error)return;
  toast('template “'+d.template+'” created — pick it when starting a run');
  await getState();
}
function newRun(mode){ const t=val('newTopic').trim(); if(!t){toast('enter a topic');return;}
  const tpl=(document.getElementById('newTpl')||{}).value||null;
  openS(S.channel,t,'video',tpl,homeShow()); }
// the PART PLANNER: AI drafts each part's focus + cliffhanger, creates the
// series + queues the parts (streamed to the console), then opens Part 1
async function planSeries(topic,n,tpl,show){
  const d=await api('/api/home/run',{tool:'series_plan',args:{topic,parts:n,show:show||null}});
  if(d.kind!=='job')return;
  openLog();
  watch(d.jobId,async(st)=>{
    if(st==='done'){toast('series planned — opening Part 1');openS(S.channel,topic+'-Part-1','video',tpl,show);}
  });
}
function showBoard(){ VIEW='board'; render(); }
function stageIdx(name){ return Math.max(0,(S.stages||[]).findIndex(x=>x.name===name)); }
// the video STORYBOARD — the whole project at a glance, one card per beat
function renderBoard(){
  const b=S.board, beats=b.beats||[];
  const v=beats.filter(x=>x.voiced).length, k=beats.filter(x=>x.kf).length,
        c=beats.filter(x=>x.clip).length, need=beats.filter(x=>!x.clip&&!x.still).length;
  const card=sg=>`<div class="dcard" onclick="nav(${stageIdx('keyframes')})" title="open the shot timeline">
    ${sg.kf?`<div class="dthumb"><img src="${art(sg.kf)}"></div>`:''}
    <div class="drow"><b>${sg.i+1}</b><span class="dkick">${esc(sg.label)}</span>
      ${sg.nsec>1?`<span class="chip">${sg.nsec} shots</span>`:''}
      ${sg.overlays?`<span class="chip">${sg.overlays} overlay${sg.overlays>1?'s':''}</span>`:''}</div>
    <div class="dbody">${esc(sg.text)}${sg.words?` <span class="muted">· ${sg.words}w</span>`:''}</div>
    <div class="drow">
      <span class="tag ${sg.voiced?'ok':''}">${sg.recorded?'your voice ✓':(sg.voiced?'voice ✓':'no voice')}</span>
      <span class="tag ${sg.kf?'ok':''}">${sg.kf?'keyframe ✓':'no keyframe'}</span>
      <span class="tag ${sg.clip?'ok':(sg.still?'':'warnc')}">${sg.clip?'clip ✓':(sg.still?'ken-burns still':'clip needed')}</span>
      ${sg.speaker?`<span class="tag">${esc(sg.speaker)}</span>`:''}
    </div></div>`;
  return `<h2>Storyboard</h2>
    <div class="sub">${beats.length} beats · ${v}/${beats.length} voiced · ${k}/${beats.length} keyframes · ${c} clip${c===1?'':'s'}${need?` · ${need} still to generate`:''}${b.cut?' · <span class="ok">cut assembled ✓</span>':''}
      — the whole video at a glance; click a beat for its shots, or jump to any stage on the rail</div>
    <div class="dboard">${beats.map(card).join('')}</div>`;
}
function fmtSecs(s){ return s>=60?Math.floor(s/60)+'m'+(s%60?(' '+(s%60)+'s'):''):s+'s'; }
// attribute-safe text for value="…" (esc covers &<>; quotes must not end the attr)
const av=s=>esc(s).replace(/"/g,'&quot;');
// ---- thumbnail cover editor: free text elements (text · size · color · a
// vertical position slider), recomposed onto the SAME background for free ----
function thumbElRow(e){
  e=e||{};
  return `<div class="row thelrow" style="margin:6px 0;align-items:center">
    <input class="the-t" value="${av(e.text||'')}" placeholder="text line" style="flex:2;min-width:160px"/>
    <select class="the-s" title="size">${['xl','l','m','s'].map(z=>`<option ${z===(e.size||'l')?'selected':''}>${z}</option>`).join('')}</select>
    <select class="the-c" title="colour">${['accent','ivory','white','black'].map(c=>`<option ${c===(e.color||'ivory')?'selected':''}>${c}</option>`).join('')}</select>
    <label class="hint" style="display:flex;align-items:center;gap:6px;flex:1;min-width:140px">top
      <input type="range" class="the-p" min="0" max="0.95" step="0.01" value="${e.pos!=null?e.pos:0.5}" title="vertical position (top → bottom)" style="flex:1"/>
    bottom</label>
    <button class="mini" onclick="this.closest('.thelrow').remove()" title="remove this line">✕</button></div>`;
}
function thumbAddEl(){ document.getElementById('thels').insertAdjacentHTML('beforeend',thumbElRow({pos:0.5})); }
function thumbSeedEls(){
  // start the editor from the CURRENT default layout (big top ticker + bottom headline)
  const d=S.data||{};
  const el=document.getElementById('thels');
  el.innerHTML=thumbElRow({text:'',size:'xl',color:'accent',pos:0.06})
              +thumbElRow({text:d.text||'',size:'l',color:'ivory',pos:0.78});
}
function thumbApplyEls(){
  const els=[...document.querySelectorAll('.thelrow')].map(r=>({
    text:r.querySelector('.the-t').value.trim(),
    size:r.querySelector('.the-s').value,
    color:r.querySelector('.the-c').value,
    pos:parseFloat(r.querySelector('.the-p').value)})).filter(e=>e.text);
  if(!els.length){toast('no text lines — use “clear” to go back to the automatic layout');return;}
  action('thumbnail','elements',{elements:els});
}
function thumbClearEls(){ if(confirm('Remove the custom text and go back to the automatic ticker + headline layout?'))action('thumbnail','elements',{elements:[]}); }
function thumbElCard(d){
  const rows=(d.elements&&d.elements.length?d.elements:[]).map(thumbElRow).join('');
  return `<div class="card" style="background:var(--panel2);margin-top:12px"><b>Cover text editor</b>
    <span class="muted">(your own lines + positions replace the automatic layout — recomposes FREE on the same background)</span>
    <div id="thels">${rows}</div>
    <div class="bar"><button class="mini" onclick="thumbAddEl()">➕ add a line</button>
      ${!d.elements.length?`<button class="mini" onclick="thumbSeedEls()">✎ start from the current layout</button>`:''}
      <button class="primary" onclick="thumbApplyEls()">Apply text</button>
      ${d.elements.length?`<button onclick="thumbClearEls()">↩ clear (auto layout)</button>`:''}</div></div>`;
}
// every project's jobs — start work, go Home, open another project, come back
function jobsCard(){
  const js=(S.jobs||[]);
  if(!js.length)return '';
  const rows=js.map(j=>`<div class="row">
    ${j.status==='running'?'<span class="spin"></span>':(j.status==='done'?'<span class="ok">✓</span>':'<span class="bad">✕</span>')}
    <b>${esc(j.topic||j.channel||'—')}</b><span class="muted">${esc(j.label)}</span>
    <span class="tag ${j.status==='done'?'ok':(j.status==='error'?'warnc':'')}" data-jsecs="${j.id}">${j.status}${j.status==='running'?' · '+fmtSecs(j.secs):''}</span>
    <span style="flex:1"></span>
    ${(j.topic&&j.mode==='video')?`<button class="mini" onclick="openS('${ja(j.channel)}','${ja(j.topic)}','${j.mode}')">Open project</button>`:''}
    <button class="mini" onclick="openLog();watch(${j.id})">Log</button></div>`).join('');
  return `<div class="card"><div class="lbl">In flight</div>
    <div class="sub" style="margin-bottom:8px">jobs keep running when you leave — open another project and come back when they're done</div>${rows}</div>`;
}
function renderHome(){
  clearTimeout(homeTimer);
  if((S.jobs||[]).some(j=>j.status==='running'))
    homeTimer=setTimeout(()=>{
      const a=document.activeElement;
      if(CED||(a&&a.closest&&a.closest('#main')&&/INPUT|TEXTAREA|SELECT/.test(a.tagName))){ renderHome.refresh=setTimeout(getState,4000); return; }
      if(S&&S.mode==='home')getState();
    },4000);
  if(HOMEVIEW==='cast'){
    // don't clobber an OPEN edit form on a background refresh — close/save
    // re-renders explicitly (renderCast has no such guard)
    if(CED&&document.getElementById('cName'))return;
    return renderCast();
  }
  const m=$('#main');
  const chans=(S.channels||[]).map(c=>`<button class="chip ${c===S.channel?'on':''}" onclick="setChannel('${ja(c)}')">${esc(c)}</button>`).join('');
  let h=`<div class="home"><h1>kalinga control center</h1>
    <div class="sub">Pick a channel, then open a run folder or start fresh — choose a workflow or run a tool.</div>
    <div class="card"><div class="lbl">Channel</div><div class="chips">${chans||'<span class="sub">no channels yet — create your first one below</span>'}</div>
      <details ${S.channelDraft?'open':''} style="margin-top:8px"><summary class="hint" style="cursor:pointer">➕ create a new channel${S.channelDraft?' — AI draft ready':''}</summary>
        ${newChannelForm()}
      </details></div>`;
  h+=jobsCard();
  if(S.channel){
    const tpls=(S.templates||[]);
    const tplSel = tpls.length>1 ? `<select id="newTpl" title="visual world / template">`+
      tpls.map(t=>`<option value="${ja(t)}" ${t===S.defaultTemplate?'selected':''}>${esc(t)}${t===S.defaultTemplate?' (default)':''}</option>`).join('')+
      `</select>` : (tpls.length===1?`<input type="hidden" id="newTpl" value="${ja(tpls[0])}"/>`:'');
    h+=`<div class="card"><div class="lbl">New ${esc(S.channelTitle||S.channel)} run</div>
      ${(S.shows&&S.shows.length)?`<div class="row" style="margin-bottom:8px">
      <select id="newShow" onchange="fillQueueSel();updateStartBtns()" title="which SHOW (format) this run belongs to — its own premise, structure, art style and researcher">
        <option value="">${esc(S.channelTitle||'channel')} (base)</option>
        ${S.shows.map(sh=>`<option value="${esc(sh.name)}" title="${esc(sh.premise||'')}">🎬 ${esc(sh.name)}</option>`).join('')}</select>
      <span class="sub" style="align-self:center">← the show sets the format; its queue + ideas follow</span></div>`:''}
      <div class="row" style="margin-bottom:8px">
      <select id="queueSel" onchange="pickQueued()" title="pending topics queued under the selected show — picking one fills the topic field" style="min-width:240px"></select>
      <input id="newTopic" placeholder="…or type a topic"/></div>
      <div class="row">${tplSel}
      <span id="startBtns" class="row" style="gap:8px"></span></div>
      <div class="row" style="margin-top:8px"><input id="ideaSeed" placeholder="rough idea to refine (optional — empty = fresh ideation)"/>
      <button class="tool" onclick="suggestTopics()">✨ Suggest topics ($0)</button></div>
      ${ideaChips()}
      ${tpls.length>1?'<div class="sub">pick the visual world (template) for this run</div>':''}</div>`;
    const cards=(S.folders||[]).map(folderCard).join('');
    h+=`<div class="card"><div class="lbl">Run folders (${(S.folders||[]).length})</div>
      <div class="grid">${cards||'<span class="sub">none yet — start a new run above</span>'}</div></div>`;
    h+=`<div class="card"><div class="lbl">Channel tools</div><div class="row wrap">
      <button class="tool" onclick="runTool('condense')">🧹 Condense learnings</button>
      <button class="tool" onclick="openCast()">🎭 Cast</button>
      <button class="tool" onclick="runTool('status')">🩺 Status</button></div>
      <details style="margin-top:8px"><summary class="hint" style="cursor:pointer">➕ new template (a visual look) for ${esc(S.channelTitle||S.channel)}</summary>
        <div class="row" style="margin-top:8px"><input id="ntName" placeholder="template name, e.g. noir"/>
          <input id="ntDesc" placeholder="one-line description (optional)"/></div>
        <textarea id="ntWorld" placeholder="world — the visual world every shot lives in (required)"></textarea>
        <textarea id="ntStyle" placeholder="style — the rendering style appended to every keyframe prompt; keep the bottom third dark and empty for captions (required)"></textarea>
        <div class="bar"><button class="good" onclick="templateNew()">Create the template</button></div>
        <div class="sub">writes channels/${esc(S.channel)}/templates/&lt;name&gt;.yaml — world + style are the two required keys, every other knob is optional in the yaml; it appears in the template picker immediately</div>
      </details></div>`;
  }
  m.innerHTML=h+`</div>`;
  fillQueueSel();          // the queue dropdown follows the selected show
  updateStartBtns();       // …and the start buttons say what it will produce
}
function folderCard(f){
  const flags=[f.busy?'<span class="spin" title="a job is running on this project"></span>':'',f.video?'🎬':'',f.facts?'📄':'',f.brief?'📋':'',f.refs?'🎨':''].filter(Boolean).join(' ');
  const t=ja(f.topic), c=ja(S.channel);
  return `<div class="fcard"><div class="ft">${esc(f.topic)}</div><div class="fd">${esc(f.date)} &nbsp;${flags}</div>
    <div class="frow"><button onclick="openS('${c}','${t}','video')">Video</button>
    <button class="mini" onclick="runTool('refs','${t}')">refs</button>
    <button class="mini" onclick="runTool('usage','${t}')">usage</button>
    <button class="mini" onclick="runTool('show','${t}')">status</button></div></div>`;
}

// ---- 🎭 cast editor (home sub-view) — the browser version of `kalinga.py cast`:
// roster cards + edit form + voice sampling; avatar / reference-library
// generation stream to the job console like any other work -----------------
function openCast(){ HOMEVIEW='cast'; CED=null; render(); }
function closeCast(){ HOMEVIEW=null; CED=null; render(); }
function castEdit(name){ CED={name:name||''}; renderCast(); }
async function castTool(tool,args){
  const d=await api('/api/home/run',{tool,args});
  if(d.kind==='job'){openLog(d.label);watch(d.jobId);}
  return d;
}
async function castSave(){
  const name=val('cName').trim();
  if(!name){toast('the character needs a name');return;}
  const voice=(val('cVoiceCustom').trim()||val('cVoice'));
  const d=await castTool('cast_save',{name,personality:val('cPers').trim(),
    gender:val('cGender'),appearance:val('cApp').trim(),voice});
  if(!d.error){ CED=null; toast('saved '+name+' — generate the avatar from their card'); await getState(); if(HOMEVIEW==='cast')renderCast(); }
}
async function castDelete(name){
  if(!confirm(`Remove ${name} from the cast? (their images stay on disk)`))return;
  const d=await castTool('cast_delete',{name});
  if(!d.error){ toast('removed '+name); await getState(); if(HOMEVIEW==='cast')renderCast(); }
}
async function castAvatar(name){
  if(!confirm(`Generate a new avatar portrait for ${name}? (~2 credits)`))return;
  castTool('cast_avatar',{name});
}
async function castRefs(name,outfit,done){
  const msg=done?`${name}'s ${outfit} set is complete — regenerate (overwrite) all 12 images? (~24 credits)`
               :`Build ${name}'s ${outfit} reference set — the missing images only? (12 images ≈ 24 credits when starting from zero)`;
  if(!confirm(msg))return;
  castTool('cast_refs',{name,outfits:[outfit],regen:done});
}
async function castSample(){
  const v=(val('cVoiceCustom').trim()||val('cVoice'));
  if(!v){toast('pick a voice first');return;}
  toast('synthesizing the sample…');
  const d=await castTool('cast_sample',{voice:v,name:val('cName').trim()});
  if(d.url)new Audio(d.url).play();
}
function castForm(){
  const C=S.cast, isNew=!CED.name;
  const mm=(C.members||[]).find(x=>x.name===CED.name)
         ||{name:'',personality:'',gender:'neutral',appearance:'',voice:''};
  const inCat=(C.voices||[]).some(v=>v.voice===mm.voice);
  const g=mm.gender;
  const ordered=[...(C.voices||[])].sort((a,b)=>(b.gender===g)-(a.gender===g));
  const opts=(inCat?'':(mm.voice?`<option value="${av(mm.voice)}" selected>${esc(mm.voice)} — current</option>`:''))
    +ordered.map(v=>`<option value="${av(v.voice)}" ${v.voice===mm.voice?'selected':''}>${esc(v.voice)} (${v.gender[0].toUpperCase()})${v.note?' — '+esc(v.note):''}</option>`).join('');
  return `<div class="card" style="background:var(--panel2)"><div class="lbl">${isNew?'New cast member':'Edit '+esc(mm.name)}</div>
    <div class="row"><input id="cName" placeholder="name (e.g. Maya)" value="${av(mm.name)}" ${isNew?'':'disabled'}/>
      <select id="cGender" title="voice gender — orders the voice list and sets the wardrobe defaults">${(C.genders||[]).map(x=>`<option ${x===mm.gender?'selected':''}>${x}</option>`).join('')}</select></div>
    <input id="cPers" placeholder="personality — who they are on camera" value="${av(mm.personality)}" style="margin-top:8px"/>
    <textarea id="cApp" placeholder="appearance — what the avatar + every keyframe should show (face, hair, style)">${esc(mm.appearance)}</textarea>
    <div class="row" style="margin-top:8px">
      <select id="cVoice" title="the ${esc(C.engine)} voice catalog (gender matches first)">${opts}</select>
      <input id="cVoiceCustom" placeholder="…or type any ${esc(C.engine)} voice" style="max-width:220px"/>
      <button class="mini" onclick="castSample()">▶ sample${C.engine==='edge'?' (free)':' (~1cr)'}</button></div>
    <div class="bar"><button class="good" onclick="castSave()">💾 Save</button>
      <button onclick="CED=null;renderCast()">cancel</button></div>
    <div class="sub">saving stores the roster entry — the avatar (~2cr) and reference library (~24cr/outfit) generate from the member card</div></div>`;
}
function castCard(mm){
  const C=S.cast;
  const refBtns=(C.outfits||[]).map(o=>{
    const n=mm.refs[o.name]||0, done=n>=o.per;
    return `<button class="mini" title="${av(o.when)}" onclick="castRefs('${ja(mm.name)}','${ja(o.name)}',${done})">${esc(o.name)} ${n}/${o.per}${done?' ✓':''}</button>`;
  }).join('');
  return `<div class="ccard">
    ${mm.avatar?`<img class="cava" src="${mm.avatar.url}?v=${mm.avatar.mtime}" onclick="window.open(this.src)">`:`<div class="cava none">no<br>avatar</div>`}
    <div class="cbody"><b>${esc(mm.name)}</b>
      <div class="hint">${esc(mm.personality||'—')}</div>
      <div class="hint">${esc(mm.gender)} · ${esc(mm.voice||'auto voice')}</div>
      <div class="bar" style="margin-top:6px">
        <button class="mini" onclick="castEdit('${ja(mm.name)}')">✏️ edit</button>
        <button class="mini" onclick="castAvatar('${ja(mm.name)}')">🖼 ${mm.avatar?'redo avatar':'avatar'} · ~2cr</button>
        <button class="mini" onclick="castDelete('${ja(mm.name)}')">✕</button></div>
      <div class="hint" style="margin-top:6px">reference library <span class="muted">(outfit × angle × light — the director picks from it)</span></div>
      <div class="row wrap">${refBtns}</div>
    </div></div>`;
}
function renderCast(){
  const C=S.cast, m=$('#main');
  if(!C){ m.innerHTML=`<div class="home"><h1>🎭 Cast</h1><div class="card">pick a channel first
    <div class="bar"><button class="mini" onclick="closeCast()">← back</button></div></div></div>`; return; }
  const cards=(C.members||[]).map(castCard).join('');
  m.innerHTML=`<div class="home">
    <div class="row" style="align-items:center"><h1>🎭 Cast — ${esc(S.channelTitle||S.channel)}</h1>
      <span style="flex:1"></span><button class="mini" onclick="closeCast()">← back to the control center</button></div>
    <div class="sub">A fixed roster of recurring characters: the writer only casts these people, and every keyframe is conditioned on the character's avatar so the SAME face shows up episode after episode. Voice engine: <b>${esc(C.engine)}</b>. Same cast.json as <code>kalinga.py cast</code> — edit from either.</div>
    ${CED?castForm():`<div class="bar" style="margin:10px 0"><button class="good" onclick="castEdit('')">➕ Add a character</button></div>`}
    <div class="cast-grid">${cards||'<div class="sub" style="padding:14px">no cast yet — add your first character (or skip casting entirely: without a roster the writer invents characters per episode)</div>'}</div>
  </div>`;
}


if(location.hash==='#board')VIEW='board';
getState();
