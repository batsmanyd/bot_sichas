const assert = require('node:assert/strict');
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE,args:['--no-sandbox']});
  const context = await browser.newContext({viewport:{width:390,height:844},isMobile:true,hasTouch:true,
    geolocation:{latitude:53.9023,longitude:27.5619},permissions:['geolocation']});
  const page = await context.newPage();
  const base = 'http://127.0.0.1:8081';
  await page.route('**/*', route => route.request().url().startsWith(base+'/') ? route.continue() : route.abort());
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const choose = async role => {
    await page.goto(base+'/demo');
    await page.locator('[name=role][value='+role+']').click();
    await page.waitForURL(base+'/');
    if(await page.evaluate(() => localStorage.getItem('onboardingDoneV2') !== '1')) {
      await page.locator('#skipOnboarding').click();
    }
  };

  try {
    await choose('guest');
    await page.locator('#openPrivateTasks').click();
    await page.locator('#taskSignIn').waitFor();
    await choose('anna');
    const cookieNames = (await context.cookies()).map(cookie => cookie.name);
    assert(cookieNames.includes('sichas_demo_session'));
    assert(!cookieNames.includes('session'));
    await page.locator('#openPrivateTasks').click();
    await page.getByText('Доступ к поручениям выдаёт администратор.').waitFor();
    await page.locator('#taskBack').click();
    await page.locator('nav [data-page=profile]').click();
    await page.locator('#telegramSettings').waitFor();
    const preference = page.waitForResponse(response => response.url().endsWith('/api/settings/telegram') && response.request().method()==='POST');
    await page.locator('#telegramOptIn').check();
    assert.equal((await preference).status(),200);
    await page.locator('nav [data-page=home]').click();
    const opened = page.waitForResponse(response => response.url().endsWith('/api/presence') && response.request().method()==='POST');
    await page.locator('#openBtn').click();
    assert((await opened).ok());
    await page.goto(base+'/demo');
    await page.locator('[name=advance]').click();
    await page.getByText(/Вы открыты к общению уже час/).waitFor();
    await page.getByRole('link',{name:'Вернуться в приложение'}).click();
    const closed = page.waitForResponse(response => response.url().endsWith('/api/presence') && response.request().method()==='DELETE');
    await page.locator('#openBtn.on').click();
    assert((await closed).ok());
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
    console.log('Demo: all local roles, isolated session, optional reminder and manual status shutdown.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode=1; });
