(() => {
  'use strict';

  let pendingPublicImage = '';
  let pendingRealPhoto = '';
  let existingPublicImage = false;
  let existingRealPhoto = false;
  let activitySummary = {};

  const imageMarkup = (src, fallback) => src
    ? `<img class="avatar-image" src="${escapeHtml(src)}" alt="">`
    : escapeHtml(fallback);

  function installProfileFields() {
    if ($('profileMediaV2')) return;
    const terms = $('profileTerms')?.closest('label');
    if (!terms) return;

    const visibility = $('selfieVisibility');
    if (visibility) {
      visibility.style.display = 'none';
      const previous = visibility.previousElementSibling;
      if (previous?.tagName === 'LABEL') previous.style.display = 'none';
      visibility.insertAdjacentHTML('afterend', '<div class="form-note"><b>Проверочное селфи закрыто.</b> Оно подтверждает, что аккаунтом пользуется живой человек, и никогда не показывается другим участникам.</div>');
    }

    const block = document.createElement('div');
    block.id = 'profileMediaV2';
    block.innerHTML = `
      <label>Публичное изображение</label>
      <div id="publicImageKind" class="options">
        <button type="button" class="option pick" data-kind="neutral">Нейтральное</button>
        <button type="button" class="option" data-kind="avatar">Аватар</button>
        <button type="button" class="option" data-kind="real">Настоящее фото</button>
      </div>
      <div class="media-upload-row">
        <img id="publicImagePreview" class="media-preview" src="/demo-user.svg" alt="Публичное изображение">
        <div><input id="publicImageInput" type="file" accept="image/*"><div class="meta">Это изображение видно в карточке. Можно использовать аватар или нейтральную картинку.</div></div>
      </div>
      <label>Отдельное настоящее фото для встречи</label>
      <div class="media-upload-row">
        <img id="realPhotoPreview" class="media-preview" src="/demo-user.svg" alt="Настоящее фото">
        <div><input id="realPhotoInput" type="file" accept="image/*"><div class="meta">Открывается одновременно обеим сторонам только после взаимного согласия. Это не проверочное селфи.</div></div>
      </div>`;
    terms.parentElement.insertBefore(block, terms);

    $('publicImageKind').querySelectorAll('.option').forEach(button => {
      button.onclick = () => selectOne($('publicImageKind'), button);
    });
    $('publicImageInput').onchange = async () => {
      try {
        pendingPublicImage = await compressSelfie($('publicImageInput').files[0]);
        $('publicImagePreview').src = pendingPublicImage;
      } catch (error) { showError(error); }
    };
    $('realPhotoInput').onchange = async () => {
      try {
        pendingRealPhoto = await compressSelfie($('realPhotoInput').files[0]);
        $('realPhotoPreview').src = pendingRealPhoto;
      } catch (error) { showError(error); }
    };
  }

  async function openProfileFormV2() {
    try {
      installProfileFields();
      const data = await api('/api/profile');
      pendingSelfie = '';
      pendingPublicImage = '';
      pendingRealPhoto = '';
      existingSelfiePresent = Boolean(data.profile.selfie_present);
      existingPublicImage = Boolean(data.profile.public_image_preview);
      existingRealPhoto = Boolean(data.profile.real_photo_present);
      clearProfileError();
      $('profileNameInput').value = data.profile.name || serverUser?.name || '';
      $('profileAgeInput').value = data.profile.age || '';
      $('profileAbout').value = data.profile.about || '';
      $('selfiePreview').src = data.profile.selfie_preview || '/demo-user.svg';
      $('publicImagePreview').src = data.profile.public_image_preview || '/demo-user.svg';
      $('realPhotoPreview').src = data.profile.real_photo_preview || '/demo-user.svg';
      document.querySelectorAll('#genderOptions .option').forEach(b => b.classList.toggle('pick', b.dataset.gender === data.profile.gender));
      document.querySelectorAll('#publicImageKind .option').forEach(b => b.classList.toggle('pick', b.dataset.kind === (data.profile.public_image_kind || 'neutral')));
      $('profileTerms').checked = Boolean(data.profile.completed);
      $('cancelProfile').style.display = 'block';
      if (!$('profileDialog').open) $('profileDialog').showModal();
    } catch (error) { showError(error); }
  }

  async function saveProfileFormV2() {
    clearProfileError();
    const name = $('profileNameInput').value.trim();
    const age = Number($('profileAgeInput').value);
    const about = $('profileAbout').value.trim();
    const gender = document.querySelector('#genderOptions .pick')?.dataset.gender;
    const publicKind = document.querySelector('#publicImageKind .pick')?.dataset.kind || 'neutral';
    if (name.length < 2) return setProfileError('Укажите имя — не короче двух букв.', $('profileNameInput'));
    if (!Number.isInteger(age) || age < 18 || age > 100) return setProfileError('Укажите возраст от 18 до 100 лет.', $('profileAgeInput'));
    if (!gender) return setProfileError('Выберите пол: мужчина или женщина.');
    if (about.length < 20) return setProfileError('В поле «О себе» нужно не меньше 20 символов.', $('profileAbout'));
    if (!pendingSelfie && !existingSelfiePresent) return setProfileError('Добавьте свежее проверочное селфи.', $('profileSelfie'));
    if (publicKind !== 'neutral' && !pendingPublicImage && !existingPublicImage) return setProfileError('Добавьте публичное изображение или выберите нейтральный вариант.', $('publicImageInput'));
    if (!$('profileTerms').checked) return setProfileError('Подтвердите совершеннолетие и правила безопасности.', $('profileTerms'));

    try {
      $('saveProfile').disabled = true;
      $('saveProfile').textContent = 'Сохраняем…';
      const data = await api('/api/profile', {
        method: 'POST',
        body: JSON.stringify({
          name, age, gender, about,
          selfie: pendingSelfie,
          public_image_kind: publicKind,
          public_image: pendingPublicImage,
          real_photo: pendingRealPhoto,
          terms_accepted: true,
        }),
      });
      profileCompleted = true;
      existingSelfiePresent = true;
      existingPublicImage = Boolean(data.profile.public_image_preview);
      existingRealPhoto = Boolean(data.profile.real_photo_present);
      serverUser = { ...(serverUser || {}), name: data.profile.name };
      $('profileDialog').close();
      updateAccountUI();
      const action = pendingAction;
      pendingAction = null;
      if (action) action(); else switchPage('home');
      showToast('Профиль сохранён. Проверочное селфи остаётся закрытым.');
    } catch (error) { showError(error); }
    finally {
      $('saveProfile').disabled = false;
      $('saveProfile').textContent = 'Сохранить';
    }
  }

  const legacyUpdateAccountUI = updateAccountUI;
  updateAccountUI = function updateAccountUIV2() {
    legacyUpdateAccountUI();
    if (isRegistered && profileCompleted) {
      $('profileBadge').textContent = '🛡 Селфи-проверка пройдена';
      $('profileDesc').textContent = 'Аккаунт подтверждён. Имя и возраст указаны пользователем.';
    }
  };

  loadFeed = async function loadFeedV2(silent = false) {
    const point = currentLocation || { latitude: 53.9023, longitude: 27.5619 };
    try {
      if (demoMode) {
        meetings = demoItems().map(item => ({ ...item, distance_band: item.distance_km < .7 ? '300–700 м' : '0,7–1,5 км', zone_radius_m: 500, approximate_zone: true }));
        activitySummary = {};
      } else {
        const data = await api(`/api/feed?lat=${point.latitude}&lon=${point.longitude}&radius=${currentLocation ? radius : 12}&category=${selected}&time=${timeFilter}`);
        meetings = data.items;
        activitySummary = data.activity_summary || {};
      }
      drawCards();
      drawCards($('mapCards'));
      drawMapMarkers();
    } catch (error) { if (!silent) showError(error); }
  };

  drawCards = function drawCardsV2(el = $('cards')) {
    if (!meetings.length) {
      const count = Object.values(activitySummary).reduce((sum, value) => sum + Number(value || 0), 0);
      if (!isRegistered && count) {
        el.innerHTML = `<div class="empty">Сейчас в Минске есть ${count} активных целей и встреч.<br><br>Войдите, чтобы увидеть персональные карточки.</div>`;
      } else {
        el.innerHTML = '<div class="empty">По выбранному времени пока никого нет.<br><br>Создайте встречу или посмотрите «Добрые дела рядом».</div>';
      }
      return;
    }
    el.innerHTML = meetings.map(m => {
      const badge = m.kind === 'person' ? 'Открыт сейчас' : m.format === 'group' ? 'Компания' : 'Один на один';
      const button = m.kind === 'person'
        ? `<button class="interest" data-presence="${m.id}" ${m.interested ? 'disabled' : ''}>${m.interested ? 'Отправлено ✓' : 'Мне интересно'}</button>`
        : m.kind === 'meeting' && !m.mine
          ? `<button class="interest" data-meeting="${m.id}" ${m.interested ? 'disabled' : ''}>${m.interested ? 'Отправлено ✓' : 'Мне интересно'}</button>` : '';
      const when = m.time_mode === 'hour' ? (m.starts_in_minutes ? `Примерно через ${m.starts_in_minutes} мин` : 'В течение часа') : 'Прямо сейчас';
      const gender = m.gender === 'male' ? 'мужчина' : m.gender === 'female' ? 'женщина' : '';
      const trust = m.profile_verified ? '<span class="badge">🛡 Селфи-проверка пройдена</span>' : '';
      const avatar = imageMarkup(m.public_image, m.icon);
      return `<article class="card"><div class="avatar">${avatar}</div><div class="card-main"><div class="card-top"><strong>${escapeHtml(m.name)}</strong><span class="badge">${badge}</span>${trust}</div><div class="desc">${escapeHtml(m.description)}</div>${m.about ? `<div class="meta">${escapeHtml(m.about)}</div>` : ''}<div class="meta">${[m.age ? m.age + ' лет' : '', gender, when, m.distance_band || 'примерная зона'].filter(Boolean).join(' · ')}</div></div>${button}</article>`;
    }).join('');
    el.querySelectorAll('[data-meeting]').forEach(button => button.onclick = () => {
      if (demoMode) return demoInterest(Number(button.dataset.meeting));
      requireRegistration(async () => {
        try { await api(`/api/meetings/${button.dataset.meeting}/interest`, { method: 'POST', body: '{}' }); await loadFeed(); }
        catch (error) { showError(error); }
      });
    });
    el.querySelectorAll('[data-presence]').forEach(button => button.onclick = () => requireRegistration(async () => {
      try {
        await api(`/api/presences/${button.dataset.presence}/interest`, { method: 'POST', body: '{}' });
        showToast('Отклик отправлен. Ответ появится в разделе «Чаты».');
        await loadFeed();
      } catch (error) { showError(error); }
    }));
  };

  drawMapMarkers = function drawMapMarkersV2() {
    if (!map || !window.L) return;
    mapMarkers.forEach(marker => map.removeLayer(marker));
    mapMarkers = meetings.filter(item => !item.mine && Number.isFinite(Number(item.latitude)) && Number.isFinite(Number(item.longitude))).map(item => {
      const circle = L.circle([item.latitude, item.longitude], {
        radius: item.zone_radius_m || 500,
        color: '#198754', fillColor: '#2eaa61', fillOpacity: .14, weight: 2,
      }).addTo(map);
      circle.bindPopup(`<b>Приблизительная зона</b><br>${escapeHtml(item.icon)} ${escapeHtml(item.description)}<br>${escapeHtml(item.distance_band || '')}<br>Точное положение человека не показывается.`);
      return circle;
    });
  };

  const legacyDrawRoom = drawRoom;
  drawRoom = function drawRoomV2(room) {
    room.participants = (room.participants || []).map(p => ({ photo_visibility: 'mutual', ...p }));
    legacyDrawRoom(room);
    activeRoom = room;
    if (activeRoom?.meeting) {
      delete activeRoom.meeting.latitude;
      delete activeRoom.meeting.longitude;
    }
    const photoHeading = [...$('roomDialog').querySelectorAll('h2')].find(h => h.textContent.includes('Фото участников'));
    if (photoHeading) photoHeading.textContent = 'Настоящие фото — только взаимно';
    $('roomPhotos').innerHTML = $('roomPhotos').innerHTML
      .replaceAll('Селфи', 'Настоящее фото')
      .replaceAll('Открыть фото', 'Предложить обмен фото');
    if ($('useMeetingPoint')) $('useMeetingPoint').style.display = 'none';
    $('placeInput').placeholder = 'Публичное место: кафе, парк, стойка информации';

    const confirmation = document.createElement('article');
    confirmation.className = 'card confirmation-card';
    const confirmed = Boolean(room.meeting?.my_confirmed);
    const allConfirmed = Boolean(room.meeting?.all_confirmed);
    confirmation.innerHTML = `<div class="avatar">${allConfirmed ? '✓' : '🕒'}</div><div class="card-main"><strong>${allConfirmed ? 'Встреча подтверждена обеими сторонами' : 'Всё в силе?'}</strong><div class="desc">${allConfirmed ? `Место: ${escapeHtml(room.meeting.confirmed_place || 'выбранное публичное место')}` : `${room.meeting?.confirmation_count || 0} из ${room.meeting?.member_count || room.participants.length} подтвердили встречу`}</div></div><button id="confirmMeetingV2" class="interest">${confirmed ? 'Отменить подтверждение' : 'Подтвердить'}</button>`;
    $('roomSafety').prepend(confirmation);
    $('confirmMeetingV2').onclick = () => roomAction(`/api/v2/meetings/${activeRoom.meeting.id}/confirm`);
  };

  function installGoodDeeds() {
    if ($('goodDeedsBlock')) return;
    const block = document.createElement('section');
    block.id = 'goodDeedsBlock';
    block.innerHTML = `<div class="section-head"><h2>Добрые дела рядом</h2><button id="createGoodDeed" class="chip create">＋ Предложить акцию</button></div><p class="good-deeds-intro">Публичные групповые дела с организатором, координатором, местом и понятной задачей.</p><div id="goodDeedsList" class="cards"><div class="empty">Загружаем…</div></div>`;
    $('home').appendChild(block);

    const dialog = document.createElement('dialog');
    dialog.id = 'goodDeedDialog';
    dialog.innerHTML = `<form class="profile-form" onsubmit="return false"><button id="closeGoodDeed" type="button" class="profile-float-close">×</button><h3>Предложить доброе дело</h3><p>До проверки организатора акция будет иметь статус «На согласовании».</p><label>Название</label><input id="deedTitle" type="text" maxlength="120" placeholder="Например: помочь приюту собрать корм"><label>Что делаем</label><textarea id="deedDescription" maxlength="500"></textarea><label>Организатор</label><input id="deedOrganizer" type="text" maxlength="120"><label>Координатор на месте</label><input id="deedCoordinator" type="text" maxlength="120"><label>Публичное место или район</label><input id="deedArea" type="text" maxlength="120"><label>Дата и время</label><input id="deedStarts" type="datetime-local"><label>Количество участников</label><input id="deedCapacity" type="number" min="2" max="100" value="10"><label>Что взять и важные условия</label><textarea id="deedInstructions" maxlength="500"></textarea><div class="actions"><button id="cancelGoodDeed" class="secondary" type="button">Отмена</button><button id="saveGoodDeed" class="primary" type="button">Отправить</button></div></form>`;
    document.body.appendChild(dialog);
    $('createGoodDeed').onclick = () => requireRegistration(() => dialog.showModal());
    $('closeGoodDeed').onclick = $('cancelGoodDeed').onclick = () => dialog.close();
    $('saveGoodDeed').onclick = createGoodDeedV2;
  }

  async function loadGoodDeeds() {
    try {
      const data = await api('/api/v2/good-deeds');
      const list = $('goodDeedsList');
      if (!data.items.length) {
        list.innerHTML = '<div class="empty">Пока нет опубликованных акций.<br><br>Первая проверенная акция появится здесь.</div>';
        return;
      }
      list.innerHTML = data.items.map(item => {
        const date = new Date(item.starts_at).toLocaleString('ru-BY', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
        const status = item.status === 'completed' ? 'Завершено' : item.organizer_verified ? 'Организатор проверен' : 'На согласовании';
        const action = item.status === 'active' ? `<button class="interest" data-deed="${item.id}">${item.my_status ? 'Участвую ✓' : 'Участвовать'}</button>` : '';
        return `<article class="card good-deed-card"><div class="avatar">🤝</div><div class="card-main"><div class="card-top"><strong>${escapeHtml(item.title)}</strong><span class="badge">${status}</span></div><div class="desc">${escapeHtml(item.description)}</div><div class="meta">${escapeHtml(item.organizer_name)} · координатор ${escapeHtml(item.coordinator_name)}</div><div class="meta">${date} · ${escapeHtml(item.area)} · ${item.participants}/${item.capacity} участников</div>${item.result_summary ? `<div class="good-result">Результат: ${escapeHtml(item.result_summary)}</div>` : ''}</div>${action}</article>`;
      }).join('');
      list.querySelectorAll('[data-deed]').forEach(button => button.onclick = () => requireRegistration(async () => {
        try { await api(`/api/v2/good-deeds/${button.dataset.deed}/join`, { method: 'POST', body: '{}' }); await loadGoodDeeds(); }
        catch (error) { showError(error); }
      }));
    } catch (error) { $('goodDeedsList').innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`; }
  }

  async function createGoodDeedV2() {
    const starts = $('deedStarts').value;
    const payload = {
      title: $('deedTitle').value.trim(),
      description: $('deedDescription').value.trim(),
      organizer_name: $('deedOrganizer').value.trim(),
      coordinator_name: $('deedCoordinator').value.trim(),
      area: $('deedArea').value.trim(),
      starts_at: starts ? new Date(starts).toISOString() : '',
      capacity: Number($('deedCapacity').value || 10),
      duration_minutes: 120,
      instructions: $('deedInstructions').value.trim(),
    };
    try {
      const data = await api('/api/v2/good-deeds', { method: 'POST', body: JSON.stringify(payload) });
      $('goodDeedDialog').close();
      showToast(data.pending ? 'Акция отправлена на проверку организатора.' : 'Доброе дело опубликовано.');
      await loadGoodDeeds();
    } catch (error) { showError(error); }
  }

  function installGoodTrace() {
    if ($('goodTraceCard')) return;
    const privacyHeading = [...$('profile').querySelectorAll('h2')].find(h => h.textContent === 'Приватность');
    const card = document.createElement('div');
    card.id = 'goodTraceCard';
    card.innerHTML = `<h2>След добра</h2><div class="card"><div class="avatar">✦</div><div class="card-main"><strong id="goodTraceTitle">Подтверждённых дел пока нет</strong><div id="goodTraceDesc" class="desc">Здесь появятся подтверждённые акции и часы участия.</div></div></div>`;
    if (privacyHeading) $('profile').insertBefore(card, privacyHeading); else $('profile').appendChild(card);
  }

  async function loadGoodTrace() {
    if (!isRegistered) return;
    try {
      const data = await api('/api/v2/good-trace');
      $('goodTraceTitle').textContent = `${data.confirmed_deeds} добрых дел · ${data.hours} ч`;
      $('goodTraceDesc').textContent = data.directions.length ? data.directions.join(' · ') : 'Подтверждённые организатором участия появятся здесь.';
    } catch (error) { console.warn(error); }
  }

  function updateHelpText() {
    const details = $('helpDialog')?.querySelectorAll('details');
    if (details?.[2]) details[2].innerHTML = '<summary>Фото и геолокация</summary><p><b>Проверочное селфи обязательно и всегда закрыто.</b> Публично можно поставить настоящее фото, аватар или нейтральную картинку. Отдельное настоящее фото открывается одновременно обеим сторонам только после взаимного согласия.</p><p><b>Точная геолокация скрыта.</b> На карте показывается приблизительная зона. Точным бывает только согласованное публичное место встречи.</p>';
    const newDetails = document.createElement('details');
    newDetails.innerHTML = '<summary>Помощь и добрые дела</summary><p>Главный формат — публичные групповые акции с организатором и координатором. Помощь дома незнакомым людям, доступ к деньгам, документам, паролям и банковским приложениям в первой версии запрещены.</p>';
    $('helpDialog')?.querySelector('.help-sheet')?.insertBefore(newDetails, details?.[3] || null);
  }

  installProfileFields();
  installGoodDeeds();
  installGoodTrace();
  updateHelpText();
  $('editProfile').onclick = openProfileFormV2;
  $('saveProfile').onclick = saveProfileFormV2;
  $('profileBadge').title = 'Проверено наличие живого человека. Имя и возраст не сверялись с документом.';
  document.addEventListener('click', event => {
    const nav = event.target.closest('#nav button');
    if (nav?.dataset.page === 'profile') loadGoodTrace();
    if (nav?.dataset.page === 'home') loadGoodDeeds();
  });
  updateAccountUI();
  loadFeed(true);
  loadGoodDeeds();
})();
