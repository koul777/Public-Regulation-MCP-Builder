// Synthetic UI exercise; no external deployment is made by this test.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');
const root = path.resolve(__dirname,'../..');
const artifact = fs.mkdtempSync(path.join(root,'tmp','vercel-ux-'));
const python = path.join(root,'.venv','Scripts','python.exe');

async function main() {
  const listener=net.createServer();
  await new Promise(resolve=>listener.listen(0,'127.0.0.1',resolve));
  const port=listener.address().port;
  await new Promise(resolve=>listener.close(resolve));
  const server=spawn(python,['-m','streamlit','run','tests/browser/vercel_deployment_fixture.py',
    '--server.address','127.0.0.1','--server.port',String(port),'--server.headless','true',
    '--browser.gatherUsageStats','false'],{cwd:root,shell:false,windowsHide:true,
    env:{...process.env,RR_DEPLOYMENT_FIXTURE_ROOT:artifact,PYTHONUTF8:'1'},stdio:['ignore','pipe','pipe']});
  let serverLog='';
  server.stdout.on('data',data=>serverLog+=data.toString());
  server.stderr.on('data',data=>serverLog+=data.toString());
  let browser;
  try {
    for(let count=0;count<100;count++) {
      if(await fetch(`http://127.0.0.1:${port}/_stcore/health`).then(r=>r.ok).catch(()=>false)) break;
      await new Promise(resolve=>setTimeout(resolve,200));
    }
    browser=await chromium.launch({headless:true});
    const page=await browser.newPage({viewport:{width:1280,height:900},reducedMotion:'reduce'});
    page.setDefaultTimeout(60000);
    const errors=[];
    page.on('pageerror',error=>errors.push(String(error)));
    const measurements=[];
    for(const scenario of ['first','ready','complete','failed']) {
      await page.goto(`http://127.0.0.1:${port}/?scenario=${scenario}`);
      await page.getByText('Vercel에 배포하고 AI에 연결하기',{exact:true}).waitFor();
      const finalControl=scenario==='first' ? 'Vercel 계정 연결' : scenario==='ready'
        ? 'Vercel에 배포하고 연결 검사' : scenario==='complete' ? 'ChatGPT 열기' : '연결 다시 검사';
      await page.getByRole(scenario==='complete' ? 'link' : 'button',{name:finalControl,exact:true}).waitFor();
      await page.getByRole('button',{name:'Stop',exact:true}).waitFor({state:'hidden'});
      await page.getByText('합성 테스트 화면 · 실제 Vercel 업로드 없음',{exact:true}).scrollIntoViewIfNeeded();
      assert.equal(await page.locator('[data-testid="stException"]').count(),0);
      await page.screenshot({path:path.join(artifact,`${scenario}.png`),fullPage:true});
      measurements.push({scenario,...await page.evaluate(()=>({
        height:document.querySelector('[data-testid="stMainBlockContainer"]').scrollHeight,
        buttons:[...document.querySelectorAll('button')].filter(b=>b.getClientRects().length).map(b=>b.innerText),
        checkboxes:[...document.querySelectorAll('input[type=checkbox]')].filter(b=>b.getClientRects().length).length,
        body:document.querySelector('[data-testid="stMainBlockContainer"]').innerText,
      }))});
    }
    // First use: connect, edit without polling stealing focus, consent, then deploy exactly once.
    await page.goto(`http://127.0.0.1:${port}/?scenario=first`);
    await page.getByRole('button',{name:'Vercel 계정 연결',exact:true}).click();
    const name=page.getByRole('textbox',{name:'새 프로젝트 이름',exact:true});
    await name.waitFor();
    await name.fill('bad name');
    await name.press('Enter');
    const deploy=page.getByRole('button',{name:'Vercel에 배포하고 연결 검사',exact:true});
    await page.getByText('영문 소문자·숫자로 시작하는 이름을 입력하세요.',{exact:false}).waitFor();
    assert(await deploy.isDisabled());
    await name.fill('regulation-demo');
    await new Promise(resolve=>setTimeout(resolve,1500));
    assert.equal(await name.inputValue(),'regulation-demo','idle rerun lost typed project');
    assert(await name.evaluate(el=>el===document.activeElement),'idle rerun stole input focus');
    await name.press('Enter');
    await page.getByText('영문 소문자·숫자로 시작하는 이름을 입력하세요.',{exact:false}).waitFor({state:'hidden'});
    const consent=page.getByRole('checkbox',{name:'이 규정을 인터넷에 공개해도 됩니다.',exact:true});
    await consent.focus();
    await page.keyboard.press('Space');
    assert(await consent.isChecked(),'keyboard space did not select public consent');
    await deploy.click();
    await page.getByText('합성 배포 검사 중',{exact:false}).waitFor();
    assert.equal(await deploy.count(),0,'duplicate publish action remained while busy');
    await page.getByText('배포·검색 검사 완료 — 다음은 AI 앱에 주소 등록입니다.',{exact:true}).waitFor();
    assert((await page.locator('code').allTextContents()).includes('https://regulation-demo.vercel.app/mcp'));
    const calls=()=>fs.readFileSync(path.join(artifact,'deployment_calls.jsonl'),'utf8').trim().split('\n').map(JSON.parse);
    assert.equal(calls().length,1);
    assert.equal(calls()[0].public_approved,true);
    assert.equal(calls()[0].replace_existing,false);
    // Verification recovery must not trigger another upload.
    await page.goto(`http://127.0.0.1:${port}/?scenario=failed`);
    await page.getByRole('button',{name:'연결 다시 검사',exact:true}).click();
    await page.getByText('배포·검색 검사 완료 — 다음은 AI 앱에 주소 등록입니다.',{exact:true}).waitFor();
    assert.equal(calls().length,1);
    // A changed bundle recalls its destination, but never old success or public consent.
    await page.goto(`http://127.0.0.1:${port}/?scenario=changed`);
    await page.getByText('새 묶음은 아직 배포하지 않았습니다.',{exact:true}).waitFor();
    assert(await page.getByRole('radio',{name:'기존 주소 업데이트',exact:true}).isChecked());
    assert(await deploy.isDisabled());
    assert.equal(await page.getByRole('checkbox',{checked:true}).count(),0);
    assert.equal(await page.locator('code').count(),0);
    await page.setViewportSize({width:390,height:844});
    await page.getByText('합성 테스트 화면 · 실제 Vercel 업로드 없음',{exact:true}).scrollIntoViewIfNeeded();
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1),'mobile horizontal overflow');
    await page.screenshot({path:path.join(artifact,'changed-mobile.png'),fullPage:true});
    assert.equal(await page.locator('[data-testid="stException"]').count(),0);
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(artifact,'measurements.json'),JSON.stringify(measurements,null,2));
    console.log(JSON.stringify({status:'PASS',artifact:path.relative(root,artifact),measurements:measurements.map(({body,...rest})=>rest)}));
  } catch(error) {
    fs.writeFileSync(path.join(artifact,'server.log'),serverLog);
    console.error('Failure artifacts:',artifact);
    throw error;
  } finally {
    if(browser) await browser.close();
    server.kill();
  }
}
main().catch(error=>{console.error(error.stack);process.exitCode=1;});
