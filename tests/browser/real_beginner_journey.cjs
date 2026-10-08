// Headless browser assertion against the real Streamlit app, with isolated synthetic data.
// Run from repository root: node tests/browser/real_beginner_journey.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '../..');
const fixture = path.join(__dirname, 'real_beginner_fixture.py');
const python = fs.existsSync(path.join(root,'.venv','Scripts','python.exe'))
  ? path.join(root,'.venv','Scripts','python.exe') : 'python';
fs.mkdirSync(path.join(root,'tmp'),{recursive:true});
const runtime = fs.mkdtempSync(path.join(root,'tmp','real-beginner-'));
const environment = {...process.env, RR_REAL_BEGINNER_ROOT:runtime, PYTHONUTF8:'1'};

async function freePort() {
  const server = net.createServer();
  await new Promise((resolve,reject)=>server.once('error',reject).listen(0,'127.0.0.1',resolve));
  const port=server.address().port;
  await new Promise(resolve=>server.close(resolve));
  return port;
}

async function snapshot() {
  const output=await new Promise((resolve,reject)=>{
    const child=spawn(python,[fixture,'--snapshot'],
      {cwd:root,env:environment,shell:false,windowsHide:true,stdio:['ignore','pipe','pipe']});
    let stdout='',stderr='',bytes=0,settled=false;
    const finish=(error,value)=>{
      if(settled) return;
      settled=true;
      clearTimeout(timer);
      if(error) reject(error); else resolve(value);
    };
    const timer=setTimeout(()=>{
      child.kill();
      finish(new Error('snapshot timed out after 30 seconds'));
    },30000);
    const collect=(chunk,isError)=>{
      bytes+=chunk.length;
      if(bytes>256*1024) {
        child.kill();
        finish(new Error('snapshot output exceeded 256 KiB'));
        return;
      }
      if(isError) stderr+=chunk.toString('utf8'); else stdout+=chunk.toString('utf8');
    };
    child.stdout.on('data',chunk=>collect(chunk,false));
    child.stderr.on('data',chunk=>collect(chunk,true));
    child.once('error',error=>finish(error));
    child.once('close',code=>finish(code===0 ? null : new Error(`snapshot exited ${code}: ${stderr}`),stdout));
  });
  return JSON.parse(output);
}

async function activeMarker(page) {
  return page.evaluate(() => {
    const visible=el=>el && el.getClientRects().length>0 && getComputedStyle(el).visibility!=='hidden';
    const markers=[...document.querySelectorAll('[data-rr-tour]')].map(element=>{
      try {return {element,...JSON.parse(element.dataset.rrTour),
        current:element.dataset.rrTourCurrent==='true',
        priority:Number(element.dataset.rrTourPriority)||0};} catch {return null;}
    }).filter(Boolean).sort((a,b)=>b.priority-a.priority||Number(b.current)-Number(a.current));
    for (const marker of markers) {
      for (const selector of marker.selectors||[]) {
        let candidates=[];
        try {candidates=[...document.querySelectorAll(selector)];} catch {continue;}
        const target=candidates.find(visible);
        if (target) return {title:marker.title,step:marker.step,substep:marker.substep,
          selector,targetText:(target.innerText||target.textContent||'').trim().slice(0,120),
          targetClass:target.className};
      }
    }
    return null;
  });
}

async function waitTitle(page,title) {
  await page.waitForFunction(value=>document.querySelector('#rr-tour-title')?.textContent===value
    && !document.querySelector('.rr-tour-card')?.hidden,title);
}

async function eventuallyMarker(page,predicate,description) {
  let last=null;
  for(let attempt=0;attempt<75;attempt++) {
    last=await activeMarker(page);
    if(predicate(last)) return last;
    await page.waitForTimeout(200);
  }
  throw new Error(`marker did not reach ${description}: ${JSON.stringify(last)}`);
}

const chunkSuffix=chunk=>chunk.chunk_id.split('_').slice(-3).join('_');
// Streamlit replaces Korean characters in widget keys; the fixture's source-node suffix is stable.
const markerChunk=(marker,document)=>{
  const matches=document.chunks.filter(chunk=>marker?.selector.includes(chunkSuffix(chunk)));
  assert(matches.length<=1,'synthetic chunk suffix does not uniquely identify a review row');
  return matches[0];
};

async function resolveFocusedWarnings(page,initial) {
  let marker=initial;
  const decisions=[];
  for(let attempt=0;attempt<10 && marker?.title==='주의할 점을 보고 버튼을 골라 주세요';attempt++) {
    const previous=marker;
    await page.locator(previous.selector).getByRole('button',{name:'이 문제는 없어요',exact:true}).click();
    decisions.push(previous.selector);
    marker=await eventuallyMarker(page,m=>m &&
      (m.selector!==previous.selector || m.title!==previous.title),'next unresolved decision or confirmation');
  }
  assert.equal(marker?.title,'내용이 맞으면 다음으로 가요','unresolved review decisions remain');
  return {marker,decisions};
}

async function nextFocusedChunk(page,previousChunk,document) {
  return eventuallyMarker(page,m=>{
    const chunk=markerChunk(m,document);
    return chunk && chunk.chunk_id!==previousChunk.chunk_id;
  },'next synthetic review row');
}

async function main() {
  const port=await freePort();
  let serverLog='';
  let serverError=null;
  const server=spawn(python,['-m','streamlit','run',fixture,'--server.address','127.0.0.1',
    '--server.port',String(port),'--server.headless','true','--browser.gatherUsageStats','false'],
    {cwd:root,env:environment,shell:false,windowsHide:true,stdio:['ignore','pipe','pipe']});
  server.once('error',error=>{serverError=error;});
  for (const pipe of [server.stdout,server.stderr]) pipe.on('data',bytes=>{
    serverLog=(serverLog+bytes.toString('utf8')).slice(-10000);
  });
  let browser, page;
  const errors=[];
  try {
    const url=`http://127.0.0.1:${port}`;
    for(let attempt=0;attempt<80;attempt++) {
      if(serverError) throw serverError;
      if(server.exitCode!==null) throw new Error(`Streamlit exited ${server.exitCode}: ${serverLog}`);
      try {const response=await fetch(`${url}/_stcore/health`);if(response.ok) break;} catch {}
      await new Promise(resolve=>setTimeout(resolve,250));
      if(attempt===79) throw new Error(`Streamlit did not become ready: ${serverLog}`);
    }
    browser=await chromium.launch({headless:true});
    page=await browser.newPage({viewport:{width:1440,height:1000}});
    page.setDefaultTimeout(30000);
    page.on('pageerror',error=>errors.push(String(error)));
    await page.goto(url);
    await page.getByRole('button',{name:'초보자 안내 시작',exact:true}).click();
    await waitTitle(page,'처음이라면, 한 단계씩 같이 해요');
    await page.locator('.rr-tour-primary').click();
    await page.locator('.rr-tour-primary').click();
    const institution=page.getByRole('textbox',{name:'기관명',exact:true});
    await institution.click();
    await institution.fill('공개 합성 브라우저 기관');
    await page.getByRole('button',{name:'기관 생성',exact:true}).click();
    await waitTitle(page,'먼저 규정 파일을 선택하세요');
    await page.locator('input[type=file]').setInputFiles(path.join(runtime,'synthetic_beginner_regulation.docx'));
    await waitTitle(page,'자동 인식한 규정 정보를 확인하세요');
    await page.locator('div[class*="st-key-beginner_guide_preprocess_info_confirmed"] label').click();
    await waitTitle(page,'파일 내용을 정리해 볼까요?');
    await page.getByRole('button',{name:'전처리 시작',exact:true}).click();
    await waitTitle(page,'전처리 결과를 확인하세요');
    await page.getByRole('button',{name:'③ 검수하고 승인으로 이동',exact:true}).click();
    await waitTitle(page,'주의할 점을 보고 버튼을 골라 주세요');
    const before=await snapshot();
    assert.equal(before.documents.length,1,'expected one synthetic document');
    const document=before.documents[0];
    const articles=document.chunks.filter(chunk=>chunk.article_no);
    assert(new Set(articles.map(chunk=>chunk.article_no)).size>=2,
      `fixture did not produce two distinct article chunks: ${JSON.stringify(articles)}`);
    const article1=articles.find(chunk=>chunk.article_no==='제1조');
    const article2=articles.find(chunk=>chunk.article_no==='제2조');
    assert(article1 && article2,'expected public fixture articles 1 and 2');
    assert(document.chunks.every(chunk=>chunk.approval_status!=='approved'));
    assert.equal(document.approval_journal_count,0);
    assert.deepEqual(document.indexing_jobs,[]);
    assert.deepEqual(before.vector_files,[]);
    const first=await activeMarker(page);
    assert.equal(first?.title,'주의할 점을 보고 버튼을 골라 주세요');
    assert.equal(await page.getByRole('button',{name:'내용이 맞아요 · 다음',exact:true}).count(),0,
      'review confirmation appeared before the required decisions');
    assert(!(await page.locator('body').innerText()).includes('를 AI 검수 결과대로 한 번에 승인하기'),
      'bulk shortcut claimed AI execution for the fixture with AI review disabled');
    const firstChunk=markerChunk(first,document);
    assert(firstChunk,'first review marker did not identify a synthetic chunk');
    const resolvedFirst=await resolveFocusedWarnings(page,first);
    const decisions=resolvedFirst.decisions;
    assert(decisions.length>0,'expected a real review decision');
    await page.locator(resolvedFirst.marker.selector).getByRole('button',{name:'내용이 맞아요 · 다음',exact:true}).click();
    const second=await nextFocusedChunk(page,firstChunk,document);
    const secondChunk=markerChunk(second,document);
    const resolvedSecond=await resolveFocusedWarnings(page,second);
    await page.locator('.rr-tour-skip').click();
    assert.equal(await page.locator('.rr-tour-card').isVisible(),false);
    await page.locator(resolvedSecond.marker.selector).getByRole('button',{name:'내용이 맞아요 · 다음',exact:true}).click();
    const third=await nextFocusedChunk(page,secondChunk,document);
    assert.equal(await page.locator('.rr-tour-card').isVisible(),false,'pause lost on rerun');
    const resume=page.getByRole('button',{name:'안내 이어서 보기',exact:true});
    if(!await resume.isVisible()) await page.getByText('설정·다른 단계 보기',{exact:true}).click();
    await resume.click();
    await waitTitle(page,third.title);
    const resumed=await eventuallyMarker(page,m=>m?.selector===third.selector,'same review target after resume');
    const reviewedChunkIds=[firstChunk.chunk_id,secondChunk.chunk_id];
    let current=resumed;
    // Warnings and preamble rows may precede articles; confirm each actual row without assuming its position.
    for(let row=0;row<document.chunks.length;row++) {
      if(reviewedChunkIds.includes(article1.chunk_id) && reviewedChunkIds.includes(article2.chunk_id)) break;
      const chunk=markerChunk(current,document);
      assert(chunk && !reviewedChunkIds.includes(chunk.chunk_id),'review returned to an already confirmed row');
      const resolved=await resolveFocusedWarnings(page,current);
      await page.locator(resolved.marker.selector).getByRole('button',{name:'내용이 맞아요 · 다음',exact:true}).click();
      reviewedChunkIds.push(chunk.chunk_id);
      if(!reviewedChunkIds.includes(article1.chunk_id) || !reviewedChunkIds.includes(article2.chunk_id))
        current=await nextFocusedChunk(page,chunk,document);
    }
    assert(reviewedChunkIds.includes(article1.chunk_id) && reviewedChunkIds.includes(article2.chunk_id),
      'did not finish the two distinct article review rows');
    const after=await snapshot();
    assert(after.documents[0].chunks.every(chunk=>chunk.approval_status!=='approved'));
    assert.equal(after.documents[0].approval_journal_count,0);
    assert.deepEqual(after.documents[0].indexing_jobs,[]);
    assert.deepEqual(after.vector_files,[]);
    assert.equal(await page.locator('#rr-tour-root').count(),1,'rerun leaked tour controller');
    assert.deepEqual(errors,[]);
    assert.equal(await page.locator('[data-testid="stException"]').count(),0);
    console.log(JSON.stringify({status:'PASS',runtime:path.relative(root,runtime).replaceAll('\\','/'),port,articles:articles.map(({chunk_id,article_no})=>({chunk_id,article_no})),
      first,decisions,second,resumed,third,reviewedChunkIds,journal:after.documents[0].approval_journal_count,vectorFiles:after.vector_files}));
  } catch (error) {
    const diagnostic={error:String(error),serverLog,errors,
      currentTitle:page ? await page.locator('#rr-tour-title').textContent().catch(()=>null) : null,
      marker:page ? await activeMarker(page).catch(()=>null) : null,
      body:page ? (await page.locator('body').innerText().catch(()=>'' )).slice(-18000) : ''};
    fs.writeFileSync(path.join(runtime,'failure.json'),JSON.stringify(diagnostic,null,2));
    if(page) await page.screenshot({path:path.join(runtime,'failure.png'),fullPage:true}).catch(()=>{});
    console.error('Synthetic browser failure artifacts:',path.relative(root,runtime).replaceAll('\\','/'));
    throw error;
  } finally {
    if(browser) await browser.close();
    server.kill();
  }
}

main().catch(error=>{console.error(error.stack||String(error));process.exitCode=1;});
