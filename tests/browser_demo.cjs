const assert = require('node:assert/strict');
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE,args:['--no-sandbox']});
  const context = await browser.newContext({viewport:{width:390,height:844},isMobile:true,hasTouch:true});
  const page = await context.newPage();
  const base = 'http://127.0.0.1:8081';
  await page.route('**/*', route => route.request().url().startsWith(base+'/') ? route.continue() : route.abort());
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const choose = async role => {
    await page.goto(base+'/demo');
    await page.locator('[name=role][value='+role+']').click();
    await page.waitForURL(base+'/');
    if(await page.locator('#onboardingDialog').evaluate(el => el.open)) {
      await page.locator('#skipOnboarding').click();
    }
  };

  try {
    await choose('guest');
    await page.locator('#openPrivateTasks').click();
    await page.locator('#taskSignIn').waitFor();
    await choose('anna');
    await page.locator('#openPrivateTasks').click();
    await page.getByText('Доступ к поручениям выдаёт администратор.').waitFor();
    await choose('organizer');
    await page.locator('#openGoodDeeds').click();
    await page.locator('#newGoodDeed summary').waitFor();
    await choose('coordinator');
    await page.locator('#openGoodDeeds').click();
    await page.locator('#goodDeedsDialog').waitFor();
    await choose('new');
    const state = await (await context.request.get(base+'/api/session')).json();
    assert.equal(state.profile_completed,false);
    await choose('customer');
    await page.locator('#openPrivateTasks').click();
    await page.locator('#taskShowCreate').click();
    const form = page.locator('#privateTaskForm');
    await form.locator('[name=title]').fill('Демонстрационная правка');
    await form.locator('[name=description]').fill('Проверить опечатки в открытой заметке');
    await form.locator('[name=budget]').fill('30');
    await form.getByRole('button',{name:'Отправить на модерацию'}).click();
    await page.getByText('На проверке',{exact:true}).waitFor();
    await choose('moderator');
    await page.locator('#openPrivateTasks').click();
    await page.getByRole('button',{name:'Одобрить',exact:true}).click();
    await page.getByText('Одобрено',{exact:true}).waitFor();
    await choose('worker');
    await page.locator('#openPrivateTasks').click();
    await page.locator('#taskShowAvailable').click();
    await page.getByRole('button',{name:'Откликнуться',exact:true}).click();
    await page.getByRole('button',{name:'Отозвать отклик',exact:true}).waitFor();
    await choose('admin');
    await page.goto(base+'/admin');
    await page.locator('#launchAdmin summary').click();
    await page.locator('#taskAccessForm').waitFor();
    assert.deepEqual(errors,[]);
    console.log('Demo: guest, participant, organizer, coordinator, incomplete registration, customer, moderator, worker and administrator.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode=1; });
