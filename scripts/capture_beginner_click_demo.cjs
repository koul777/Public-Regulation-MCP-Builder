/* Record actual localhost controls with a public synthetic document.
 * Prerequisite: tests/browser/beginner_demo_app.py running on port 8766.
 * Requires optional Playwright + Chromium; no production dependency.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');

(async () => {
  const out = path.resolve('tmp/beginner-click-recording');
  fs.mkdirSync(out, {recursive:true});
  const browser = await chromium.launch({headless:true});
  const context = await browser.newContext({viewport:{width:1440,height:1000},
    recordVideo:{dir:out,size:{width:1440,height:1000}}});
  const page = await context.newPage();
  page.setDefaultTimeout(30000);
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  const video = page.video();
  const hold = () => page.waitForTimeout(2200);
  const guide = title => page.waitForFunction(text =>
    document.querySelector('#rr-tour-title')?.textContent === text
    && !document.querySelector('.rr-tour-card')?.hidden, title);
  const click = async locator => {
    await locator.scrollIntoViewIfNeeded();
    const box = await locator.boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, {steps:16});
    await page.waitForTimeout(450);
    await locator.click();
  };
  try {
    await page.goto('http://127.0.0.1:8766');
    // The pointer is a recording aid, not shipped application UI.
    await page.evaluate(() => {
      const pointer = document.createElement('div');
      pointer.style.cssText='position:fixed;width:22px;height:22px;border:3px solid #f59e0b;border-radius:50%;background:#f59e0b33;pointer-events:none;z-index:2147483647;transform:translate(-50%,-50%);left:-50px';
      document.body.append(pointer);
      document.addEventListener('mousemove',e=>{pointer.style.left=e.clientX+'px';pointer.style.top=e.clientY+'px';});
    });
    await hold();
    await click(page.getByRole('button',{name:'초보자 안내 시작',exact:true}));
    await guide('처음이라면, 한 단계씩 같이 해요'); await hold();
    await click(page.locator('.rr-tour-primary')); await hold();
    await click(page.locator('.rr-tour-primary')); await hold();
    const institution = page.getByRole('textbox',{name:'기관명',exact:true});
    await click(institution);
    await institution.pressSequentially('공개 합성 시연 기관',{delay:110});
    await institution.press('Tab');
    await guide('입력한 기관을 생성하세요'); await hold();
    await click(page.getByRole('button',{name:'기관 생성',exact:true}));
    await guide('먼저 규정 파일을 선택하세요'); await hold();
    // Playwright supplies the file through the same native uploader input.
    await page.locator('input[type=file]').setInputFiles(path.resolve('tmp/beginner-guide-sample.docx'));
    await guide('자동 인식한 규정 정보를 확인하세요'); await hold();
    await page.screenshot({path:'docs/assets/beginner-click-guide.png'});
    await click(page.locator('div[class*="st-key-beginner_guide_preprocess_info_confirmed"] label'));
    await guide('선택한 파일의 전처리를 시작하세요'); await hold();
    await click(page.getByRole('button',{name:'전처리 시작',exact:true}));
    await guide('전처리 결과를 확인하세요'); await hold();
    await click(page.getByRole('button',{name:'③ 검수하고 승인으로 이동',exact:true}));
    await guide('검수 항목을 읽고 판단하세요'); await hold();
    await click(page.getByRole('button',{name:'해당 없음',exact:true}).first());
    await guide('이 조항의 검수 항목 확인을 마치세요'); await hold();
    await click(page.getByText('표시된 검수 항목에 대한 판단을 모두 확인했습니다.',{exact:true}).first());
    await guide('원문과 최종본을 직접 대조하세요'); await hold();
    await click(page.getByText('원본과 최종본을 직접 대조했고, 이 내용으로 승인·색인하는 데 동의합니다.',{exact:true}).first());
    await guide('이 조항의 검수 항목 확인을 마치세요'); await hold();
    await click(page.locator('.rr-tour-skip')); await hold();
    await click(page.getByRole('button',{name:'안내 이어서 보기',exact:true}));
    await guide('이 조항의 검수 항목 확인을 마치세요'); await hold();
    assert.deepEqual(errors, []);
    assert.equal(await page.locator('[data-testid="stException"]').count(), 0);
    console.log('PASS: actual upload, preprocessing, per-clause review, pause and resume');
  } finally {
    await context.close();
    await video.saveAs(path.join(out,'beginner-click-guide.webm'));
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode=1;});
