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
  let browser;
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
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    page.setDefaultTimeout(30000);
    const errors=[];
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
    await waitTitle(page,'검수 항목을 읽고 판단하세요');
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
    assert.equal(first?.title,'검수 항목을 읽고 판단하세요');
    await page.getByRole('button',{name:'해당 없음',exact:true}).first().click();
    await waitTitle(page,'이 조항의 검수 항목 확인을 마치세요');
    await page.getByText('표시된 검수 항목에 대한 판단을 모두 확인했습니다.',{exact:true}).first().click();
    await waitTitle(page,'원문과 최종본을 직접 대조하세요');
    await page.getByText('원본과 최종본을 직접 대조했고, 이 내용으로 승인·색인하는 데 동의합니다.',{exact:true}).first().click();
    await waitTitle(page,'이 조항의 검수 항목 확인을 마치세요');
    const second=await activeMarker(page);
    assert(second,'expected a next active review marker');
    console.log(JSON.stringify({checkpoint:'after-first-row',first,second}));
    assert.notEqual(second.selector,first.selector,
      `guide still targets first control after confirmation: ${JSON.stringify({first,second})}`);
    assert(second.selector.includes(chunkSuffix(article1)),
      `next marker is not article 1: ${JSON.stringify(second)}`);
    await page.locator('.rr-tour-skip').click();
    assert.equal(await page.locator('.rr-tour-card').isVisible(),false);
    await page.locator(second.selector).locator('label').click();
    assert.equal(await page.locator('.rr-tour-card').isVisible(),false,'pause lost on rerun');
    await page.getByRole('button',{name:'안내 이어서 보기',exact:true}).click();
    const resumed=await eventuallyMarker(page,m=>m?.title==='원문과 최종본을 직접 대조하세요'
      && m.selector.includes(chunkSuffix(article1)),'article 1 comparison after resume');
    await page.locator(resumed.selector).locator('label').click();
    const third=await eventuallyMarker(page,m=>m?.selector.includes(chunkSuffix(article2)),
      'article 2 active review target');
    const after=await snapshot();
    assert(after.documents[0].chunks.every(chunk=>chunk.approval_status!=='approved'));
    assert.equal(after.documents[0].approval_journal_count,0);
    assert.deepEqual(after.documents[0].indexing_jobs,[]);
    assert.deepEqual(after.vector_files,[]);
    assert.equal(await page.locator('#rr-tour-root').count(),1,'rerun leaked tour controller');
    assert.deepEqual(errors,[]);
    assert.equal(await page.locator('[data-testid="stException"]').count(),0);
    console.log(JSON.stringify({status:'PASS',runtime:path.relative(root,runtime).replaceAll('\\','/'),port,articles:articles.map(({chunk_id,article_no})=>({chunk_id,article_no})),
      first,second,resumed,third,journal:after.documents[0].approval_journal_count,vectorFiles:after.vector_files}));
  } finally {
    if(browser) await browser.close();
    server.kill();
  }
}

main().catch(error=>{console.error(error.stack||String(error));process.exitCode=1;});
