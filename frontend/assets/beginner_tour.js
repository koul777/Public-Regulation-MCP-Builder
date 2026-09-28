/* Local presentation only: never call workflow buttons or change application state. */
(() => {
  const win = window.parent;
  const doc = win.document;
  const owner = "__regulationBeginnerTour";
  win[owner]?.dispose();
  if (!config.enabled) return;
  const storageKey = "rr-beginner-tour-v1:" + win.location.pathname;
  let saved = {};
  try { saved = JSON.parse(win.sessionStorage.getItem(storageKey) || "{}"); } catch (_) {}
  if (!saved || typeof saved !== "object") saved = {};
  const persist = () => {
    try { win.sessionStorage.setItem(storageKey, JSON.stringify(saved)); } catch (_) {}
  };
  const requested = saved.request !== config.request;
  if (requested) { saved.request = config.request; saved.hidden = false; }
  const root = doc.createElement("div");
  root.id = "rr-tour-root";
  root.innerHTML = `<style>${tourCSS}</style>
    <div class="rr-tour-dock"><span><small>지금 할 일</small><b></b></span><button type="button">눌러볼 곳 안내</button></div>
    <div class="rr-tour-shade" hidden></div><div class="rr-tour-shade" hidden></div>
    <div class="rr-tour-shade" hidden></div><div class="rr-tour-shade" hidden></div>
    <div class="rr-tour-ring" hidden></div>
    <section class="rr-tour-card" role="dialog" aria-labelledby="rr-tour-title" aria-describedby="rr-tour-description" tabindex="-1" hidden>
      <div class="rr-tour-top"><span class="rr-tour-tag"></span><button class="rr-tour-skip" type="button">건너뛰기 ×</button></div>
      <h2 id="rr-tour-title"></h2><p id="rr-tour-description"></p><div class="rr-tour-tip"></div>
      <div class="rr-tour-footer"><div class="rr-tour-dots" aria-label="안내 진행"></div><div class="rr-tour-actions">
        <button class="rr-tour-prev" type="button">이전</button><button class="rr-tour-primary" type="button">다음 →</button>
      </div></div>
      <label class="rr-tour-optout"><input type="checkbox">이 탭에서 자동 안내 그만 보기</label>
    </section>`;
  doc.body.appendChild(root);
  const find = selector => root.querySelector(selector);
  const card = find(".rr-tour-card");
  const shades = [...root.querySelectorAll(".rr-tour-shade")];
  const ring = find(".rr-tour-ring");
  const dock = find(".rr-tour-dock");
  let active = null, target = null, slide = 0, opened = false, disposed = false;
  let frame = 0, scanTimer = 0, lastFocus = null;
  const visible = element => !!element && element.getClientRects().length > 0;
  const resolve = marker => {
    for (const selector of marker.selectors || []) {
      const element = [...doc.querySelectorAll(selector)].find(visible);
      if (element) return element;
    }
    return null;
  };
  const box = (element, left, top, width, height) => Object.assign(element.style, {
    left: left + "px", top: top + "px", width: Math.max(0, width) + "px", height: Math.max(0, height) + "px"
  });
  const place = () => {
    frame = 0;
    if (!opened || disposed) return;
    const w = win.innerWidth, h = win.innerHeight;
    const rect = visible(target) ? target.getBoundingClientRect() : null;
    const gap = 8;
    const r = rect ? {left:Math.max(4, rect.left-gap), top:Math.max(4,rect.top-gap),
      right:Math.min(w-4,rect.right+gap), bottom:Math.min(h-4,rect.bottom+gap)} : null;
    shades.forEach(element => {element.hidden = false;});
    ring.hidden = !r;
    if (r && r.bottom > r.top && r.right > r.left) {
      box(shades[0],0,0,w,r.top); box(shades[1],0,r.bottom,w,h-r.bottom);
      box(shades[2],0,r.top,r.left,r.bottom-r.top); box(shades[3],r.right,r.top,w-r.right,r.bottom-r.top);
      box(ring,r.left,r.top,r.right-r.left,r.bottom-r.top);
    } else {box(shades[0],0,0,w,h); shades.slice(1).forEach(e => {e.hidden=true;}); ring.hidden=true;}
    const cw = card.offsetWidth, ch = card.offsetHeight;
    let x = (w-cw)/2, y = (h-ch)/2;
    if (r) {
      if (w-r.right >= cw+24) {x=r.right+16; y=r.top;}
      else if (r.left >= cw+24) {x=r.left-cw-16; y=r.top;}
      else {x=r.left; y=h-r.bottom >= ch+24 ? r.bottom+16 : r.top-ch-16;}
    }
    card.style.left = Math.max(12,Math.min(w-cw-12,x)) + "px";
    card.style.top = Math.max(12,Math.min(h-ch-12,y)) + "px";
  };
  const schedulePlace = () => {if (!frame) frame=win.requestAnimationFrame(place);};
  const close = () => {
    opened=false; card.hidden=true; ring.hidden=true; shades.forEach(e=>{e.hidden=true;}); dock.hidden=false;
    saved.seen=true; saved.hidden=!!find("input").checked; persist();
    if (lastFocus?.isConnected) lastFocus.focus({preventScroll:true});
  };
  const show = (index, focus = true) => {
    slide=index;
    const journey=doc.querySelector("[data-rr-journey]");
    const steps = [
      {title:"처음이라면, 같이 한 바퀴 돌아요", description:"파일을 올리고, 내용을 확인하고, AI에 연결합니다. 지금 눌러야 할 곳을 하나씩 짚어드릴게요.", tip:"안내는 언제든 닫을 수 있어요. 오른쪽 아래 ‘눌러볼 곳 안내’로 다시 볼 수 있습니다.", target:null},
      {title:"지금 할 일은 초록색으로", description:"완료한 일에는 체크가 붙고, 현재 단계는 초록색으로 표시됩니다. 실제 작업을 마쳐야 완료로 바뀝니다.", tip:"원문 확인과 승인은 직접 진행합니다. 안내의 ‘다음’은 설명만 넘깁니다.", target:journey},
      {title:active?.title || "이 화면에서 작업을 이어가세요", description:active?.description || "화면에 표시된 준비 상태를 확인하세요. 작업할 항목이 준비되면 여기에 안내합니다.", tip:target ? "밝게 보이는 실제 항목을 누르거나, ‘직접 해보기’를 눌러 안내를 닫으세요." : "안내를 닫아도 입력한 내용과 현재 작업은 유지됩니다.", target:active ? resolve(active) : null}
    ];
    const step=steps[slide]; target=step.target;
    find("h2").textContent=step.title; find("#rr-tour-description").textContent=step.description;
    find(".rr-tour-tip").textContent=step.tip;
    find(".rr-tour-tag").textContent=slide===0 ? "시작하기" : "STEP " + slide;
    find(".rr-tour-prev").hidden=slide===0;
    find(".rr-tour-primary").textContent=slide===2 ? "직접 해보기 →" : "다음 →";
    find(".rr-tour-dots").innerHTML=steps.map((_,i)=>`<span class="rr-tour-dot ${i===slide?'active':''}"></span>`).join("");
    find(".rr-tour-dots").setAttribute("aria-label",`${slide+1} / ${steps.length} 안내`);
    card.hidden=false; dock.hidden=true; opened=true;
    if (target && focus) target.scrollIntoView({block:"center",behavior:"instant"});
    place(); if (focus) card.focus({preventScroll:true});
  };
  const open = index => {lastFocus=doc.activeElement; show(index);};
  const scan = () => {
    if (disposed) return;
    // Don't cover Streamlit's own confirmation or progress dialogs.
    const otherDialog=[...doc.querySelectorAll('[role="dialog"]')].some(e=>!root.contains(e)&&visible(e));
    if (otherDialog) {if (opened) close(); dock.hidden=true; return;}
    if (!opened) dock.hidden=false;
    const markers=[...doc.querySelectorAll("[data-rr-tour]")].map(element=>{
      try {return {...JSON.parse(element.dataset.rrTour), element};} catch (_) {return null;}
    }).filter(Boolean);
    const previous=active;
    active=markers.find(marker=>resolve(marker)) || markers.find(marker=>visible(marker.element)) || null;
    find(".rr-tour-dock b").textContent=active?.title || "현재 작업 이어가기";
    if (opened && slide===2 && (previous?.title!==active?.title || !target?.isConnected)) show(2,false);
  };
  const onKey = event => {
    if (!opened) return;
    if (event.key==="Escape") {event.preventDefault();event.stopPropagation();close();}
    if (event.key==="Tab") {
      const candidates=[...(target ? target.querySelectorAll('button,input,select,textarea,a[href],[tabindex="0"]') : []),
        ...card.querySelectorAll('button,input')].filter(e=>visible(e)&&!e.disabled);
      if (!candidates.length) return;
      const index=candidates.indexOf(doc.activeElement);
      if (event.shiftKey && index<=0) {event.preventDefault();candidates.at(-1).focus();}
      else if (!event.shiftKey && (index<0 || index===candidates.length-1)) {event.preventDefault();candidates[0].focus();}
    }
  };
  const onClick = event => {
    if (opened && target?.contains(event.target) && !root.contains(event.target)) close();
  };
  find(".rr-tour-skip").onclick=close;
  find(".rr-tour-prev").onclick=()=>show(Math.max(0,slide-1));
  find(".rr-tour-primary").onclick=()=>slide===2 ? close() : show(slide+1);
  dock.querySelector("button").onclick=()=>open(2);
  find("input").checked=!!saved.hidden;
  doc.addEventListener("keydown",onKey,true); doc.addEventListener("click",onClick,true);
  win.addEventListener("resize",schedulePlace); doc.addEventListener("scroll",schedulePlace,true);
  const observer=new win.MutationObserver(records=>{
    if (records.every(record=>root.contains(record.target))) return;
    win.clearTimeout(scanTimer); scanTimer=win.setTimeout(scan,100);
  });
  observer.observe(doc.body,{childList:true,subtree:true,attributes:true,attributeFilter:["data-rr-tour","open"]});
  const dispose=()=>{
    disposed=true; observer.disconnect(); win.clearTimeout(scanTimer); win.cancelAnimationFrame(frame);
    doc.removeEventListener("keydown",onKey,true); doc.removeEventListener("click",onClick,true);
    win.removeEventListener("resize",schedulePlace); doc.removeEventListener("scroll",schedulePlace,true);
    root.remove(); if (win[owner]?.dispose===dispose) delete win[owner];
  };
  win[owner]={dispose};
  window.addEventListener("pagehide",dispose,{once:true});
  scan();
  if (requested || (!saved.seen && !saved.hidden)) open(0);
  persist();
})();
