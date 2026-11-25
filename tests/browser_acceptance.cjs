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
    const ownerId=(await (await owner.request.get(base+'/api/session')).json()).user.id;
    const dashboard=await admin.context().newPage();
    dashboard.on('pageerror',error=>errors.push(error.message));
    await dashboard.goto(base+'/admin');
    await dashboard.locator('#organizerAdmin summary').click();
    await dashboard.locator('#organizerForm [name=user_id]').fill(String(ownerId));
    await dashboard.locator('#organizerForm [name=name]').fill('Организатор акции');
    await dashboard.locator('#organizerForm [name=reason]').fill('Документы проверены');
    await dashboard.getByRole('button',{name:'Сохранить организатора'}).click();
    await dashboard.locator('#organizerStatus').getByText('Сохранено').waitFor();
    await dashboard.locator('#coordinatorForm [name=organizer_id]').fill(String(ownerId));
    await dashboard.locator('#coordinatorForm [name=user_id]').fill(String(ownerId));
    await dashboard.locator('#coordinatorForm [name=label]').fill('Координатор акции');
    await dashboard.getByRole('button',{name:'Сохранить координатора'}).click();
    await dashboard.locator('#organizerList').getByText(/Координатор акции/).waitFor();
    await dashboard.locator('#launchAdmin summary').click();
    await dashboard.locator('#moduleForm [name=notifications]').and(dashboard.locator(':checked')).waitFor();
    await dashboard.locator('#moduleForm [name=notifications]').uncheck();
    await dashboard.getByRole('button',{name:'Сохранить разделы'}).click();
    await dashboard.locator('#launchStatus').getByText('Сохранено').waitFor();
    assert.equal((await (await owner.request.get(base+'/api/session')).json()).modules.notifications,false);
    await dashboard.close();
    console.log('Browser: organizer verification, coordinator authorization and global switches.');
    await tasks(owner);
    await owner.locator('#privateTasksDialog details:has(#privateTaskForm) > summary').click();
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
      area:'Вход в городской сквер',
      starts_at:now.toISOString().slice(0,16),instructions:'Возьмите перчатки и пакеты'};
    for(const [field,value] of Object.entries(fields))await owner.locator('#goodDeedForm [name='+field+']').fill(value);
    await owner.locator('#goodDeedForm [name=public_site_confirmed]').check();
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
    await owner.locator('#closeGoodDeeds').click();
    await worker.locator('#closeGoodDeeds').click();
    await owner.locator('#createBtn').click();
    const meetingCreated=owner.waitForResponse(r=>r.url().endsWith('/api/meetings')&&r.request().method()==='POST');
    await owner.locator('#saveMeeting').click();
    assert.equal((await meetingCreated).status(),201);
    await worker.locator('#requestNearbyLocation').click();
    await worker.getByRole('button',{name:'Мне интересно',exact:true}).click();
    await owner.locator('nav [data-page=chats]').click();
    await owner.getByRole('button',{name:'Принять',exact:true}).click();
    await owner.getByRole('button',{name:'Открыть встречу',exact:true}).click();
    await owner.locator('#messageInput').fill('Увидимся в кафе');
    await owner.locator('#sendMessage').click();
    await owner.locator('#roomMessages').getByText('Увидимся в кафе').waitFor();
    await worker.locator('nav [data-page=chats]').click();
    await worker.getByRole('button',{name:'Открыть встречу',exact:true}).click();
    await worker.locator('#roomMessages').getByText('Увидимся в кафе').waitFor();
    console.log('Browser: geolocation, meeting creation, application, acceptance and synchronized room.');
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
