// Real Chromium coverage of the shipped controller. Synthetic DOM, no app data.
// Run with Playwright available: node tests/browser/beginner-tour.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '../..');
const script = fs.readFileSync(path.join(root, 'frontend/assets/beginner_tour.js'), 'utf8');
const css = fs.readFileSync(path.join(root, 'frontend/assets/beginner_tour.css'), 'utf8');

(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1280,height:900}});
    const errors = [];
    page.on('pageerror', error => errors.push(String(error)));
    await page.route('http://guide.test/**', route => route.fulfill({contentType:'text/html; charset=utf-8', body:`
      <style>body{font-family:sans-serif;padding:60px}button,input,summary{padding:14px} .widget{margin:30px 0;width:260px}</style>
      <div data-rr-journey>기관 → 파일 → 승인 → 연결</div><div id="markers"></div>
      <div class="widget" id="institution"><input aria-label="기관명"></div>
      <div class="widget" id="upload"><button>파일 선택</button></div>
      <details id="settings"><summary>연결 설정</summary><div class="widget" id="connect"><button>연결 확인</button></div></details>
      <div class="widget" id="approve"><button>직접 승인</button></div>
      <script>window.approvals=0;document.querySelector('#approve button').onclick=()=>window.approvals++;</script>`}));
    await page.goto('http://guide.test/');
    const marker = async (id, title, substep=1, extra={}) => page.evaluate(({id,title,substep,extra}) => {
      const node=document.createElement('div');
      node.dataset.rrTour=JSON.stringify({title,description:'밝은 항목을 직접 눌러 진행하세요.',step:1,substep,selectors:['#'+id],presentation:extra.presentation});
      node.dataset.rrTourCurrent=extra.current ? 'true' : 'false';
      node.dataset.rrTourPriority=String(extra.priority || 0);
      document.querySelector('#markers').append(node);
    }, {id,title,substep,extra});
    const clear = () => page.locator('#markers').evaluate(el=>el.replaceChildren());
    const mount = () => page.addScriptTag({content:`(() => {const config={enabled:true,request:1,page:'synthetic'};const tourCSS=${JSON.stringify(css)};${script}\n})();`});
    const title = value => page.waitForFunction(text=>document.querySelector('#rr-tour-title')?.textContent===text,value);
    await marker('institution','기관명을 입력하세요');
    await mount();
    await page.locator('.rr-tour-primary').click();
    await page.locator('.rr-tour-primary').click();
    await title('기관명을 입력하세요');
    await page.getByRole('textbox',{name:'기관명'}).fill('공개 합성 기관');
    // An actual click hides this prompt but never triggers another workflow action.
    await page.getByRole('textbox',{name:'기관명'}).click();
    await clear(); await marker('upload','파일을 선택하세요',2,{current:true});
    await title('파일을 선택하세요');
    await page.locator('#institution').evaluate(el=>el.style.height='180px');
    await page.waitForFunction(()=>Math.abs(document.querySelector('.rr-tour-ring').getBoundingClientRect().top-(document.querySelector('#upload').getBoundingClientRect().top-8))<1);
    await page.getByRole('button',{name:'파일 선택',exact:true}).click();
    await mount();
    assert.equal(await page.locator('.rr-tour-card').isVisible(),false,'same unfinished action must not loop on rerun');
    // Current action wins over an unrelated marker earlier in the DOM.
    await clear(); await marker('institution','이전 항목');
    await marker('connect','연결을 확인하세요',3,{current:true});
    await title('먼저 접힌 항목을 펼쳐 주세요');
    await page.locator('#settings summary').click();
    await title('연결을 확인하세요');
    // The app owns confirmation dialogs. The guide suspends and then resumes.
    await page.evaluate(()=>{const el=document.createElement('div');el.role='dialog';el.id='app-dialog';el.textContent='앱 확인';document.body.append(el);});
    await page.waitForFunction(()=>document.querySelector('.rr-tour-card').hidden);
    await page.locator('#app-dialog').evaluate(el=>el.remove());
    await page.waitForFunction(()=>!document.querySelector('.rr-tour-card').hidden);
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('.rr-tour-dock button').textContent(),'안내 이어서 보기');
    await clear(); await marker('approve','승인 내용을 확인하세요',4);
    await mount();
    assert.equal(await page.locator('.rr-tour-card').isVisible(),false,'pause survives a new action and rerun');
    await page.locator('.rr-tour-dock button').click();
    await title('승인 내용을 확인하세요');
    assert.equal(await page.evaluate(()=>window.approvals),0,'tour must never approve');
    await page.locator('.rr-tour-primary').click();
    await page.getByRole('button',{name:'직접 승인',exact:true}).click();
    assert.equal(await page.evaluate(()=>window.approvals),1);
    // Missing targets never produce a floating, blocking ghost prompt.
    await clear(); await marker('missing','없는 버튼');
    await mount();
    assert.equal(await page.locator('.rr-tour-card').isVisible(),false);
    await clear(); await marker('upload','파일을 다시 선택하세요',5);
    await title('파일을 다시 선택하세요');
    await page.locator('.rr-tour-optout input').check();
    await page.locator('.rr-tour-primary').click();
    await clear(); await marker('approve','다른 작업',6); await mount();
    assert.equal(await page.locator('.rr-tour-card').isVisible(),false,'opt-out survives action changes');
    await page.locator('.rr-tour-dock button').click();
    await title('다른 작업');
    assert.equal(await page.locator('#rr-tour-root').count(),1,'reruns dispose old controllers');
    await page.setViewportSize({width:390,height:844});
    await page.locator('.rr-tour-primary').click();
    await page.locator('.rr-tour-dock button').click();
    const bounds=await page.locator('.rr-tour-card').boundingBox();
    assert(bounds.x>=0 && bounds.x+bounds.width<=391 && bounds.y>=0 && bounds.y+bounds.height<=845);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.keyboard.press('Tab');
    assert(await page.evaluate(()=>document.querySelector('#rr-tour-root').contains(document.activeElement)||document.querySelector('#approve').contains(document.activeElement)));
    await page.emulateMedia({reducedMotion:'reduce'});
    assert.equal(await page.locator('.rr-tour-card').evaluate(el=>getComputedStyle(el).animationName),'none');
    // The closed guide must leave Streamlit's fixed chat composer clickable.
    await page.keyboard.press('Escape');
    await page.setViewportSize({width:1280,height:900});
    await page.evaluate(()=>{
      const composer=document.createElement('div');
      composer.dataset.testid='stChatInput';
      composer.style.cssText='position:fixed;bottom:20px;left:80px;right:80px;height:60px;background:white';
      composer.innerHTML='<textarea aria-label="다음 질문"></textarea><button style="position:absolute;right:0;bottom:0" aria-label="질문 보내기">전송</button>';
      window.sentQuestions=0;
      composer.querySelector('button').onclick=()=>window.sentQuestions++;
      document.body.append(composer);
    });
    const dockAboveComposer=()=>page.waitForFunction(()=>{
      const dock=document.querySelector('.rr-tour-dock').getBoundingClientRect();
      const input=document.querySelector('[data-testid="stChatInput"]').getBoundingClientRect();
      return dock.bottom<=input.top-8;
    },null,{timeout:3000});
    await dockAboveComposer();
    await page.getByRole('button',{name:'질문 보내기',exact:true}).click();
    await page.locator('[data-testid="stChatInput"]').evaluate(el=>el.style.height='150px');
    await dockAboveComposer();
    await page.setViewportSize({width:390,height:844});
    await dockAboveComposer();
    await page.getByRole('button',{name:'질문 보내기',exact:true}).click();
    assert.equal(await page.evaluate(()=>window.sentQuestions),2,'desktop and mobile sends must remain clickable');
    await page.locator('[data-testid="stChatInput"]').evaluate(el=>el.remove());
    await page.waitForFunction(()=>document.querySelector('.rr-tour-dock').style.bottom==='');
    // Comparing a source and its editor must never require pausing the guide.
    await page.evaluate(()=>{
      const source=document.createElement('textarea');
      source.setAttribute('aria-label','비교할 원문');source.value='원문 문장 끝까지 읽고 대조합니다.';
      document.querySelector('#approve').before(source);
    });
    await clear(); await marker('approve','원문과 최종본을 대조하세요',7,{presentation:'inline'});
    await mount();
    await page.locator('.rr-tour-dock button').click();
    await title('원문과 최종본을 대조하세요');
    assert.equal(await page.locator('.rr-tour-card').evaluate(e=>getComputedStyle(e).position),'relative');
    assert.equal(await page.locator('.rr-tour-shade:visible').count(),0,'review must not dim or block surrounding text');
    await page.getByRole('textbox',{name:'비교할 원문'}).click();
    assert.equal(await page.locator('.rr-tour-card').isVisible(),true,'source can be read with guide still open');
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(()=>document.activeElement===document.querySelector('#approve button')),true,'inline review must not trap normal reading navigation');
    assert.equal(await page.evaluate(()=>window.approvals),1,'review guidance cannot approve');
    await page.getByRole('button',{name:'직접 승인',exact:true}).click();
    assert.equal(await page.evaluate(()=>window.approvals),2);
    await clear(); await marker('upload','검수 다음 단계',8); await mount();
    await title('검수 다음 단계');
    assert.equal(await page.locator('#rr-tour-root').evaluate(e=>e.parentElement===document.body),true);
    assert.deepEqual(errors,[]);
    console.log('PASS: click progression, reruns, priority, collapsed controls, dialogs, pause/resume, opt-out, no auto-approval, mobile, keyboard and unobscured chat send');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
