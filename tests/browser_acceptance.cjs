const assert=require('node:assert/strict');
const {chromium}=require('playwright');
(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE,args:['--no-sandbox']});
  const base='http://127.0.0.1:'+(process.env.ACCEPTANCE_PORT||'8095'), errors=[];
  async function pageFor(user){
    const context=await browser.newContext({viewport:{width:390,height:844},isMobile:true,hasTouch:true,timezoneId:'UTC',
      geolocation:{latitude:53.9023,longitude:27.5619},permissions:['geolocation']});
    assert.equal((await context.request.post(base+'/auth/test',{data:{user}})).status(),200);
    const page=await context.newPage();
    page.on('pageerror',error=>errors.push(error.message));
    await page.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
    await page.goto(base+'/',{waitUntil:'domcontentloaded'});
    await page.locator('#skipOnboarding').click();
    await page.locator('#accountState').getByText('Профиль',{exact:true}).waitFor();
    return page;
  }
  async function tasks(page, reopen=false){
    if(reopen)await page.locator('[data-close-dialog=privateTasksDialog]').click();
    else await page.locator('#accountState').click();
    await page.locator('#openPrivateTasks').click();
    await page.locator('#privateTasksDialog').waitFor();
  }
  async function deeds(page, reopen=false){
    if(reopen)await page.locator('#closeGoodDeeds').click();
    else await page.locator('nav [data-page=home]').click();
    await page.locator('#openGoodDeeds').click();
    await page.locator('#goodDeedsDialog').waitFor();
  }
  try{
    const [owner, worker, admin]=await Promise.all(['991','992','993'].map(pageFor));
    await tasks(owner);
    await owner.locator('#privateTasksDialog summary').first().click();
    for(const [field,value] of Object.entries({title:'Проверочная редактура',description:'Безопасное дистанционное оформление текста',budget:35})){
      await owner.locator('#privateTaskForm [name='+field+']').fill(String(value));
    }
    await owner.getByRole('button',{name:'Отправить на модерацию'}).click();
    await owner.getByText('На проверке',{exact:true}).waitFor();
    await tasks(admin);
    await admin.getByRole('button',{name:'Одобрить',exact:true}).click();
    await admin.getByText('Одобрено',{exact:true}).waitFor();
    await tasks(worker);
    await worker.getByRole('button',{name:'Откликнуться',exact:true}).click();
    await tasks(owner,true);
    await owner.getByRole('button',{name:/Назначить/}).click();
    await owner.getByText('Назначено',{exact:true}).waitFor();
    await tasks(worker,true);
    await worker.getByRole('button',{name:'Отправить результат'}).click();
    await worker.locator('#taskResultForm textarea').fill('Исправления готовы');
    await worker.getByRole('button',{name:'Передать результат'}).click();
    await worker.getByText('Результат отправлен',{exact:true}).waitFor();
    await tasks(owner,true);
    await owner.getByRole('button',{name:'Принять результат'}).click();
    await owner.getByText('Принято',{exact:true}).waitFor();
    for(const page of [owner, worker, admin])await page.locator('[data-close-dialog=privateTasksDialog]').click();
    console.log('Browser: task creation → independent approval → application → assignment → delivery → acceptance.');

    await deeds(owner);
    await owner.locator('#newGoodDeed summary').click();
    const now=new Date(Date.now()+120*60000);
    const fields={title:'Уборка городского сквера',description:'Убираем мусор на общедоступной территории',
      organizer_name:'Организатор акции',coordinator_name:'Координатор акции',area:'Вход в городской сквер',
      starts_at:now.toISOString().slice(0,16),instructions:'Возьмите перчатки и пакеты'};
    for(const [field,value] of Object.entries(fields))await owner.locator('#goodDeedForm [name='+field+']').fill(value);
    await owner.getByRole('button',{name:'Отправить на проверку'}).click();
    await owner.getByText('На проверке',{exact:true}).waitFor();
    await deeds(admin);
    await admin.getByRole('button',{name:'Опубликовать',exact:true}).click();
    await admin.getByText('Открыта',{exact:true}).waitFor();
    await deeds(worker);
    await worker.getByRole('button',{name:'Участвовать',exact:true}).click();
    await worker.getByRole('button',{name:'Отменить запись',exact:true}).waitFor();
    assert.equal((await admin.request.post(base+'/__clock/good-deed')).status(),200);
    await deeds(owner,true);
    const completion=owner.locator('#goodDeedsDialog form[data-complete]');
    await completion.locator('[name=result_summary]').fill('Сквер приведён в порядок');
    await completion.locator('[name=participant]').first().check();
    await completion.locator('[name=thanks]').fill('Спасибо за помощь');
    await completion.getByRole('button',{name:'Подтвердить результат и участие'}).click();
    await owner.getByText('Завершена',{exact:true}).waitFor();
    await deeds(worker,true);
    await worker.getByText('Спасибо за помощь',{exact:false}).waitFor();
    const publicContext=await browser.newContext({viewport:{width:1280,height:800}});
    const publicPage=await publicContext.newPage();
    publicPage.on('pageerror',error=>errors.push(error.message));
    await publicPage.goto(base+'/about?lang=en');
    await publicPage.getByRole('heading',{name:'People nearby. Things to do together.'}).waitFor();
    assert(await publicPage.locator('img').evaluate(img=>img.complete&&img.naturalWidth>0));
    await publicPage.setViewportSize({width:768,height:1024});
    assert(await publicPage.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await publicPage.setViewportSize({width:390,height:844});
    assert(await publicPage.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await publicPage.goto(base+'/',{waitUntil:'domcontentloaded'});
    await publicPage.waitForFunction(()=>Boolean(navigator.serviceWorker.controller));
    await publicContext.setOffline(true);
    await publicPage.goto(base+'/?offline-check=1',{waitUntil:'domcontentloaded'});
    await publicPage.getByRole('heading',{name:'Нет соединения'}).waitFor();
    assert.deepEqual(errors,[]);
    console.log('Browser: deed creation → moderation → participation → completion → gratitude in the participant profile.');
    console.log('Browser: public English page, QR, desktop/tablet/mobile and offline PWA fallback.');
  }finally{await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
