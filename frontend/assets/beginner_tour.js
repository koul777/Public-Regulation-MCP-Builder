/* Local presentation only: never call workflow buttons or change application state. */
(() => {
  const win = window.parent;
  const doc = win.document;
  const owner = "__regulationBeginnerTour";
  win[owner]?.dispose();
  if (!config.enabled) return;
  const storageKey = "rr-beginner-tour-v2:" + win.location.pathname;
  let saved = {};
  try { saved = JSON.parse(win.sessionStorage.getItem(storageKey) || "{}"); } catch (_) {}
  if (!saved || typeof saved !== "object" || Array.isArray(saved)) saved = {};
  const persist = () => {
    try { win.sessionStorage.setItem(storageKey, JSON.stringify(saved)); } catch (_) {}
  };
  const requested = saved.request !== config.request;
  if (requested) saved = {request:config.request, seen:false, hidden:false, paused:false};
  const root = doc.createElement("div");
  root.id = "rr-tour-root";
  root.innerHTML = `<style>${tourCSS}</style>
    <div class="rr-tour-dock"><span><small>지금 할 일</small><b></b></span><button type="button">한 단계씩 안내</button></div>
    <div class="rr-tour-shade" hidden></div><div class="rr-tour-shade" hidden></div>
    <div class="rr-tour-shade" hidden></div><div class="rr-tour-shade" hidden></div>
    <div class="rr-tour-ring" hidden></div>
    <section class="rr-tour-card" role="dialog" aria-labelledby="rr-tour-title" aria-describedby="rr-tour-description" tabindex="-1" hidden>
      <div class="rr-tour-top"><span class="rr-tour-tag"></span><button class="rr-tour-skip" type="button">안내 잠시 멈춤 ×</button></div>
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
  let frame = 0, scanTimer = 0, lastFocus = null, suspended = false;
  let chatInput = null;
  const inline = () => slide === 2 && active?.presentation === "inline";
  const visible = element => !!element && element.getClientRects().length > 0
    && win.getComputedStyle(element).visibility !== "hidden";
  const resolve = marker => {
    for (const selector of marker.selectors || []) {
      let elements = [];
      try {elements = [...doc.querySelectorAll(selector)];} catch (_) {continue;}
      for (const element of elements) {
        // Let the user open collapsed sections before pointing at their contents.
        const closed = [];
        for (let parent=element.parentElement; parent; parent=parent.parentElement) {
          if (parent.tagName==="DETAILS" && !parent.open) closed.push(parent);
        }
        const summary=closed.at(-1)?.querySelector("summary");
        if (visible(summary)) return {target:summary, reveal:true};
        if (visible(element)) return {target:element, reveal:false};
      }
    }
    return null;
  };
  const identity = marker => marker ? JSON.stringify([
    config.page, marker.step, marker.substep, marker.title, marker.selectors, marker.presentation,
    marker.resolved.reveal,
  ]) : "";
  const box = (element, left, top, width, height) => Object.assign(element.style, {
    left: left + "px", top: top + "px", width: Math.max(0, width) + "px", height: Math.max(0, height) + "px"
  });
  const placeDock = () => {
    // Streamlit pins the composer to the bottom. Keep the collapsed guide above
    // it, including when a multiline draft grows or the window becomes narrow.
    const input = [...doc.querySelectorAll('[data-testid="stChatInput"]')].find(visible) || null;
    if (input !== chatInput) {
      chatInputObserver.disconnect(); chatInput = input;
      if (chatInput) chatInputObserver.observe(chatInput);
    }
    const rect = chatInput?.getBoundingClientRect();
    dock.style.bottom = rect && rect.top < win.innerHeight && rect.bottom > 0
      ? Math.max(12, win.innerHeight - rect.top + 12) + "px" : "";
  };
  const place = () => {
    frame = 0;
    if (disposed) return;
    placeDock();
    if (!opened) return;
    if (inline()) {
      shades.forEach(element => {element.hidden = true;}); ring.hidden = true;
      return;
    }
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
  const resizeObserver = new win.ResizeObserver(schedulePlace);
  const chatInputObserver = new win.ResizeObserver(schedulePlace);
  const watchLayout = () => {
    resizeObserver.disconnect();
    // Dataframes and other async widgets can move the target without scrolling.
    for (let element=target; element; element=element.parentElement) resizeObserver.observe(element);
  };
  const close = ({pause=false, dismiss=true, restoreFocus=true} = {}) => {
    opened=false; card.hidden=true; ring.hidden=true; shades.forEach(e=>{e.hidden=true;}); dock.hidden=false;
    root.classList.remove("rr-tour-inline");
    placeDock();
    saved.seen=true; saved.hidden=!!find("input").checked;
    if (pause) saved.paused=true;
    dock.querySelector("button").textContent=saved.paused || saved.hidden ? "안내 이어서 보기" : "한 단계씩 안내";
    if (dismiss) {saved.dismissedKey=identity(active); saved.openKey="";}
    persist();
    if (restoreFocus && lastFocus?.isConnected) lastFocus.focus({preventScroll:true});
  };
  const show = (index, focus = true) => {
    slide=index;
    const journey=doc.querySelector("[data-rr-journey]");
    const qwen = config.page === "qwen-chat";
    const steps = [
      {title:"처음이라면, 한 단계씩 같이 해요", description:qwen ? "기관과 승인된 규정을 고르고, Qwen 연결을 확인한 뒤 첫 질문을 보냅니다. 지금 누를 항목을 하나씩 밝혀 드릴게요." : "기관을 고르고 파일을 올린 뒤, 내용을 확인하고 AI에 연결합니다. 지금 눌러야 할 실제 항목을 하나씩 밝혀 드릴게요.", tip:"직접 작업을 마치면 다음 안내가 이어집니다. 잠시 멈춰도 오른쪽 아래에서 다시 시작할 수 있습니다.", target:null},
      {title:"지금 할 일은 초록색으로", description:qwen ? "밝게 표시된 기관·규정 선택 상자와 연결 확인 버튼을 차례로 누릅니다. 답변이 나오면 근거 조문까지 함께 확인하세요." : "완료한 일에는 체크가 붙고, 현재 단계는 초록색으로 표시됩니다. 실제 작업을 마쳐야 완료로 바뀝니다.", tip:"실제 작업은 직접 진행합니다. 안내의 ‘다음’은 설명만 넘깁니다.", target:journey},
      {title:active?.resolved.reveal ? "먼저 접힌 항목을 펼쳐 주세요" : active?.title || "이 화면에서 작업을 이어가세요",
       description:active?.resolved.reveal ? `밝게 표시된 제목을 눌러 펼친 뒤 ‘${active.title}’ 안내를 따라 주세요.` : active?.description || "화면에 표시된 준비 상태를 확인하세요. 작업할 항목이 준비되면 여기에 안내합니다.",
       tip:"밝게 보이는 실제 항목을 누르세요. 완료 상태가 바뀌면 다음 안내가 자동으로 이어집니다.", target:active?.resolved.target || null}
    ];
    const step=steps[slide]; target=step.target;
    watchLayout();
    find("h2").textContent=step.title; find("#rr-tour-description").textContent=step.description;
    find(".rr-tour-tip").textContent=step.tip;
    find(".rr-tour-tag").textContent=slide===2 && active ? `${active.step}-${active.substep} · 지금 누를 곳` : "시작 안내 " + (slide+1);
    find(".rr-tour-prev").hidden=slide===0;
    find(".rr-tour-primary").textContent=slide===2 ? "안내 접고 직접 하기" : slide===1 ? "한 단계씩 시작 →" : "다음 →";
    find(".rr-tour-dots").innerHTML=slide===2 ? "" : [0,1].map(i=>`<span class="rr-tour-dot ${i===slide?'active':''}"></span>`).join("");
    find(".rr-tour-dots").setAttribute("aria-label",slide===2 ? "실제 작업 따라하기" : `${slide+1} / 2 시작 안내`);
    root.classList.toggle("rr-tour-inline", inline());
    card.setAttribute("role", inline() ? "region" : "dialog");
    if (inline()) {
      // Review needs the surrounding original, editor and findings readable.
      // Keep guidance in document flow; no dimmer, focus trap or floating card.
      active.element.before(root);
      card.style.left = ""; card.style.top = "";
    } else if (root.parentElement !== doc.body) doc.body.append(root);
    card.hidden=false; dock.hidden=true; opened=true;
    if (slide===2) {saved.seen=true; saved.openKey=identity(active); saved.dismissedKey=""; persist();}
    if (focus) (inline() ? card : target)?.scrollIntoView({block:inline() || win.innerWidth<800 ? "start" : "center",behavior:"instant"});
    place(); if (focus) card.focus({preventScroll:true});
  };
  const open = index => {lastFocus=doc.activeElement; show(index);};
  const scan = () => {
    if (disposed) return;
    placeDock();
    // Don't cover Streamlit's own confirmation or progress dialogs.
    const otherDialog=[...doc.querySelectorAll('[role="dialog"]')].some(e=>!root.contains(e)&&visible(e));
    if (otherDialog) {
      if (opened) {suspended=true; close({dismiss:false, restoreFocus:false});}
      dock.hidden=true; return;
    }
    if (!opened) dock.hidden=false;
    const markers=[...doc.querySelectorAll("[data-rr-tour]")].map(element=>{
      try {
        const marker={...JSON.parse(element.dataset.rrTour), element};
        marker.resolved=resolve(marker);
        return marker;
      } catch (_) {return null;}
    }).filter(marker=>marker?.resolved).sort((a,b)=>
      (Number(b.element.dataset.rrTourPriority)||0)-(Number(a.element.dataset.rrTourPriority)||0)
      || Number(b.element.dataset.rrTourCurrent==="true")-Number(a.element.dataset.rrTourCurrent==="true"));
    active=markers[0] || null;
    find(".rr-tour-dock b").textContent=active?.title || "현재 작업 이어가기";
    dock.querySelector("button").textContent=saved.paused || saved.hidden ? "안내 이어서 보기" : "한 단계씩 안내";
    const key=identity(active);
    if (opened && slide===2) {
      if (!active) close({dismiss:false, restoreFocus:false});
      else if (saved.openKey!==key || target!==active.resolved.target) show(2,false);
      else schedulePlace();
    } else if (!opened && active && saved.seen && !saved.paused && !saved.hidden
      && (suspended || saved.dismissedKey!==key)) {
      open(2);
    }
    suspended=false;
  };
  const onKey = event => {
    if (!opened) return;
    if (event.key==="Escape") {event.preventDefault();event.stopPropagation();close({pause:true});}
    if (event.key==="Tab" && !inline()) {
      const selector='button,input:not([type="hidden"]),select,textarea,a[href],summary,[tabindex="0"]';
      const candidates=[...(target?.matches(selector) ? [target] : []),
        ...(target ? target.querySelectorAll(selector) : []),
        ...card.querySelectorAll('button,input')].filter(e=>visible(e)&&!e.disabled&&e.tabIndex>=0);
      if (!candidates.length) return;
      const index=candidates.indexOf(doc.activeElement);
      if (event.shiftKey && index<=0) {event.preventDefault();candidates.at(-1).focus();}
      else if (!event.shiftKey && (index<0 || index===candidates.length-1)) {event.preventDefault();candidates[0].focus();}
    }
  };
  const onClick = event => {
    if (opened && target?.contains(event.target) && !root.contains(event.target)
      && !event.target.closest?.('[disabled],[aria-disabled="true"]')) close({restoreFocus:false});
  };
  find(".rr-tour-skip").onclick=()=>close({pause:true});
  find(".rr-tour-prev").onclick=()=>show(Math.max(0,slide-1));
  find(".rr-tour-primary").onclick=()=>slide===2 ? close() : show(slide+1);
  dock.querySelector("button").onclick=()=>{
    saved.paused=false; saved.hidden=false; saved.dismissedKey="";
    find("input").checked=false; persist(); scan();
    if (!opened) open(active ? 2 : 0);
  };
  find("input").checked=!!saved.hidden;
  doc.addEventListener("keydown",onKey,true); doc.addEventListener("click",onClick,true);
  win.addEventListener("resize",schedulePlace); doc.addEventListener("scroll",schedulePlace,true);
  const observer=new win.MutationObserver(records=>{
    if (records.every(record=>root.contains(record.target))) return;
    schedulePlace();
    win.clearTimeout(scanTimer); scanTimer=win.setTimeout(scan,100);
  });
  observer.observe(doc.body,{childList:true,subtree:true,attributes:true,
    attributeFilter:["data-rr-tour","data-rr-tour-current","data-rr-tour-priority","open","disabled","class","style","hidden"]});
  const dispose=()=>{
    disposed=true; observer.disconnect(); resizeObserver.disconnect(); chatInputObserver.disconnect(); win.clearTimeout(scanTimer); win.cancelAnimationFrame(frame);
    doc.removeEventListener("keydown",onKey,true); doc.removeEventListener("click",onClick,true);
    win.removeEventListener("resize",schedulePlace); doc.removeEventListener("scroll",schedulePlace,true);
    window.removeEventListener("pagehide",dispose);
    root.remove(); if (win[owner]?.dispose===dispose) delete win[owner];
  };
  win[owner]={dispose};
  window.addEventListener("pagehide",dispose,{once:true});
  scan();
  if (!saved.seen && !saved.hidden && !saved.paused) open(0);
  persist();
})();
