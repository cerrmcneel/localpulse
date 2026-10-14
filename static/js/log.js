import { getJSON, postForm, postJSON, fmt, round, toast, esc, checkHealth, prettyDate, shiftDay, todayISO, inferMealType, getFoodIcon } from './api.js';

const $ = (id) => document.getElementById(id);

const MEAL_ICON = {
  breakfast: '☕', lunch: '🍜', dinner: '🍽️',
  snack: '🍎', other: '🍽️',
};

/** Working copy of the meal under review. Edits mutate this, never the DOM. */
let draft = { name: 'Meal', items: [], pendingImage: null, model: null, raw: null, source: 'manual' };
let previewURL = null;

// Date state and initialization (supports logging for yesterday or any past day)
const urlParams = new URLSearchParams(window.location.search);
const initialDay = urlParams.get('day') || todayISO();
let selectedMealDay = initialDay;

function setMealDay(d) {
  selectedMealDay = d || todayISO();
  if ($('meal-date')) $('meal-date').value = selectedMealDay;
  if ($('review-meal-date')) $('review-meal-date').value = selectedMealDay;

  const label = prettyDate(selectedMealDay);
  const isPast = selectedMealDay < todayISO();
  const badgeText = isPast ? `${label} (${selectedMealDay})` : label;
  if ($('meal-date-badge')) {
    $('meal-date-badge').textContent = badgeText;
    $('meal-date-badge').style.color = isPast ? 'var(--accent)' : 'var(--muted)';
    $('meal-date-badge').style.fontWeight = isPast ? '600' : 'normal';
  }
  if ($('review-date-badge')) {
    $('review-date-badge').textContent = badgeText;
    $('review-date-badge').style.color = isPast ? 'var(--accent)' : 'var(--muted)';
    $('review-date-badge').style.fontWeight = isPast ? '600' : 'normal';
  }
  if ($('today')) {
    $('today').textContent = isPast ? `Logging for ${label} (${selectedMealDay})` : prettyDate(todayISO());
    $('today').style.color = isPast ? 'var(--accent)' : '';
  }
  const targetDayLabel = selectedMealDay === todayISO() ? 'Today' : prettyDate(selectedMealDay);
  if ($('repeat-target-date-label')) $('repeat-target-date-label').textContent = targetDayLabel;
  updateRepeatButtonsLabel(targetDayLabel);
}

const todayStr = todayISO();
const yesterdayStr = shiftDay(todayStr, -1);
if ($('meal-date')) $('meal-date').max = todayStr;
if ($('review-meal-date')) $('review-meal-date').max = todayStr;
setMealDay(initialDay);

$('meal-date')?.addEventListener('change', (e) => setMealDay(e.target.value));
$('review-meal-date')?.addEventListener('change', (e) => setMealDay(e.target.value));
$('btn-date-today')?.addEventListener('click', () => setMealDay(todayStr));
$('btn-date-yesterday')?.addEventListener('click', () => setMealDay(yesterdayStr));
$('btn-review-date-today')?.addEventListener('click', () => setMealDay(todayStr));
$('btn-review-date-yesterday')?.addEventListener('click', () => setMealDay(yesterdayStr));

function show(step) {
  const isCapture = step === 'step-capture';
  $('step-capture')?.classList.toggle('hidden', !isCapture);
  $('step-past-meals')?.classList.toggle('hidden', !isCapture);
  $('step-loading')?.classList.toggle('hidden', step !== 'step-loading');
  $('step-review')?.classList.toggle('hidden', step !== 'step-review');
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

// --- photo and text intake ---
let selectedFile = null;

$('btn-camera')?.addEventListener('click', () => $('file').click());
$('btn-library')?.addEventListener('click', () => $('file-lib').click());

for (const id of ['file', 'file-lib']) {
  $(id)?.addEventListener('change', (e) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (file) {
      selectedFile = file;
      if (previewURL) URL.revokeObjectURL(previewURL);
      previewURL = URL.createObjectURL(file);
      const thumb = $('intake-photo-thumb');
      const fallback = $('intake-photo-fallback');
      if (thumb) {
        thumb.style.display = 'block';
        thumb.src = previewURL;
      }
      if (fallback) fallback.classList.add('hidden');
      $('intake-photo-box')?.classList.remove('hidden');
      $('photo-btns-row')?.classList.add('hidden');
    }
  });
}

$('intake-photo-thumb')?.addEventListener('error', () => {
  const thumb = $('intake-photo-thumb');
  const fallback = $('intake-photo-fallback');
  if (thumb && fallback && selectedFile) {
    thumb.style.display = 'none';
    fallback.classList.remove('hidden');
    const nameEl = $('intake-fallback-name');
    const infoEl = $('intake-fallback-info');
    if (nameEl) nameEl.textContent = selectedFile.name || 'Photo selected';
    if (infoEl) {
      const mb = (selectedFile.size / (1024 * 1024)).toFixed(1);
      infoEl.textContent = `${mb} MB · Ready for AI macro estimation (auto-converted to JPEG on upload)`;
    }
  }
});

$('btn-remove-photo')?.addEventListener('click', () => {
  selectedFile = null;
  if (previewURL) URL.revokeObjectURL(previewURL);
  previewURL = null;
  const thumb = $('intake-photo-thumb');
  const fallback = $('intake-photo-fallback');
  if (thumb) {
    thumb.src = '';
    thumb.style.display = 'block';
  }
  if (fallback) fallback.classList.add('hidden');
  $('intake-photo-box')?.classList.add('hidden');
  $('photo-btns-row')?.classList.remove('hidden');
});

$('btn-manual')?.addEventListener('click', () => {
  draft = { name: 'Meal', items: [blankItem()], pendingImage: null, model: null, raw: null, source: 'manual' };
  $('preview').classList.add('hidden');
  $('model-notes').classList.add('hidden');
  $('meal-name').value = '';
  if ($('meal-type')) $('meal-type').value = inferMealType();
  renderItems();
  show('step-review');
});

$('btn-analyze-ai')?.addEventListener('click', async () => {
  const text = $('meal-description').value.trim();
  if (!selectedFile && !text) {
    toast('Please provide a photo, a description, or both.', true);
    return;
  }
  await runAnalysis(selectedFile, text);
});

async function runAnalysis(file, text) {
  show('step-loading');
  if (file && text) {
    $('loading-text').textContent = 'Combining photo & notes with AI…';
  } else if (file) {
    $('loading-text').textContent = 'Analysing meal photo with AI…';
  } else {
    $('loading-text').textContent = 'Estimating macros from description…';
  }

  const form = new FormData();
  if (file) form.append('image', file, file.name || 'meal.jpg');
  if (text) form.append('text', text);

  const started = Date.now();
  const tick = setInterval(() => {
    $('loading-sub').textContent = `${Math.round((Date.now() - started) / 1000)}s elapsed`;
  }, 1000);

  try {
    const result = await postForm('/api/analyze', form);
    draft = {
      name: result.dish || 'Meal',
      items: result.items.map((i) => ({ ...i })),
      pendingImage: result.pending_image,
      model: result.model,
      raw: result.raw,
      source: result.source || (file ? 'photo' : 'text'),
    };
    $('meal-name').value = draft.name;
    if ($('meal-type')) $('meal-type').value = inferMealType();

    if (result.pending_image) {
      $('preview').src = `/media/${result.pending_image}`;
      $('preview').classList.remove('hidden');
    } else if (file && previewURL) {
      $('preview').src = previewURL;
      $('preview').classList.remove('hidden');
    } else {
      $('preview').classList.add('hidden');
    }

    const notes = $('model-notes');
    if (result.notes) {
      notes.className = 'banner';
      notes.innerHTML = `<b>AI Estimate:</b> ${esc(result.notes)}`;
      notes.classList.remove('hidden');
    } else {
      notes.classList.add('hidden');
    }

    renderItems();
    show('step-review');
  } catch (err) {
    toast(err.message, true);
    show('step-capture');
  } finally {
    clearInterval(tick);
    $('loading-sub').textContent = 'A cold model load can take a minute the first time.';
  }
}

// --- editable items ---
const blankItem = () => ({
  name: '', grams: 0, calories: 0, protein_g: 0, carbs_g: 0, fat_g: 0,
  confidence: 'high', basis: '',
});

const FIELDS = [
  ['grams', 'g'], ['calories', 'kcal'], ['protein_g', 'P'], ['carbs_g', 'C'], ['fat_g', 'F'],
];

const CONF_LABELS = {
  high: 'AI Conf: High',
  medium: 'AI Conf: Med',
  low: 'AI Conf: Low',
};

const CONF_DESCRIPTIONS = {
  high: 'High AI Confidence: Food is clearly visible with a reliable portion estimate.',
  medium: 'Medium AI Confidence: Standard recognizable dish, but portion size is approximate.',
  low: 'Low AI Confidence: Ingredients are mixed, covered in sauce, or obscured. Please check and adjust grams.',
};

function renderItems() {
  const isPhoto = draft.source === 'photo';
  $('confidence-explainer')?.classList.toggle('hidden', !isPhoto);
  $('items-source-badge')?.classList.toggle('hidden', !isPhoto);

  $('items').innerHTML = draft.items.map((item, idx) => `
    <div class="item" data-idx="${idx}">
      <div class="row1">
        <span class="food-icon-badge" data-badge-idx="${idx}" title="Component visual">${getFoodIcon(item.name)}</span>
        <input data-field="name" value="${esc(item.name)}" placeholder="Item name" aria-label="Item name">
        ${isPhoto && item.confidence
          ? `<span class="chip ${esc(item.confidence)}" data-conf="${esc(item.confidence)}" role="button" tabindex="0" title="${CONF_DESCRIPTIONS[item.confidence] || 'AI Confidence'}. Tap for details.">${CONF_LABELS[item.confidence] || esc(item.confidence)}</span>`
          : ''}
        <button class="del" data-remove="${idx}" aria-label="Remove item">&times;</button>
      </div>
      <div class="grid">
        ${FIELDS.map(([field, label]) => `
          <div>
            <label>${label}</label>
            <input type="number" inputmode="decimal" step="any" min="0"
                   data-field="${field}" value="${round(item[field], 1)}" aria-label="${label}">
          </div>`).join('')}
      </div>
      ${item.basis ? `<div class="basis">${esc(item.basis)}</div>` : ''}
    </div>`).join('');

  $('items').querySelectorAll('input').forEach((input) => {
    input.addEventListener('input', onEdit);
  });
  $('items').querySelectorAll('[data-remove]').forEach((btn) => {
    btn.addEventListener('click', () => {
      draft.items.splice(Number(btn.dataset.remove), 1);
      if (!draft.items.length) draft.items.push(blankItem());
      renderItems();
    });
  });
  $('items').querySelectorAll('.chip[data-conf]').forEach((chip) => {
    const showInfo = () => {
      const level = chip.dataset.conf;
      toast(CONF_DESCRIPTIONS[level] || `AI Visual Certainty: ${level}`);
    };
    chip.addEventListener('click', showInfo);
    chip.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        showInfo();
      }
    });
  });
  updateTotals();
}

function onEdit(e) {
  const idx = Number(e.target.closest('.item').dataset.idx);
  const field = e.target.dataset.field;
  const item = draft.items[idx];
  if (!item) return;

  if (field === 'name') {
    item.name = e.target.value;
    const badge = e.target.closest('.item')?.querySelector(`.food-icon-badge[data-badge-idx="${idx}"]`);
    if (badge) badge.textContent = getFoodIcon(e.target.value);
    return; // no totals impact, and re-rendering would steal focus
  }

  const value = Math.max(0, Number(e.target.value) || 0);
  item[field] = value;

  // Editing a macro re-derives calories, so a corrected portion stays coherent.
  // Editing calories directly is respected as-is -- the user may know the label value.
  if (field !== 'calories') {
    item.calories = round(item.protein_g * 4 + item.carbs_g * 4 + item.fat_g * 9, 1);
    const calInput = e.target.closest('.item').querySelector('[data-field="calories"]');
    if (calInput && document.activeElement !== calInput) calInput.value = item.calories;
  }
  updateTotals();
}

function totals() {
  return draft.items.reduce((acc, i) => ({
    calories: acc.calories + (Number(i.calories) || 0),
    protein_g: acc.protein_g + (Number(i.protein_g) || 0),
    carbs_g: acc.carbs_g + (Number(i.carbs_g) || 0),
    fat_g: acc.fat_g + (Number(i.fat_g) || 0),
  }), { calories: 0, protein_g: 0, carbs_g: 0, fat_g: 0 });
}

function updateTotals() {
  const t = totals();
  $('t-kcal').textContent = fmt(t.calories);
  $('t-pro').textContent = fmt(t.protein_g);
  $('t-car').textContent = fmt(t.carbs_g);
  $('t-fat').textContent = fmt(t.fat_g);
}

$('btn-add-item').addEventListener('click', () => {
  draft.items.push(blankItem());
  renderItems();
  $('items').lastElementChild?.querySelector('input')?.focus();
});

// --- save ---
$('btn-save').addEventListener('click', async () => {
  const items = draft.items
    .filter((i) => i.name.trim() && (i.calories > 0 || i.protein_g > 0 || i.carbs_g > 0 || i.fat_g > 0))
    .map((i) => ({
      name: i.name.trim(),
      grams: round(i.grams, 1),
      calories: round(i.calories, 1),
      protein_g: round(i.protein_g, 1),
      carbs_g: round(i.carbs_g, 1),
      fat_g: round(i.fat_g, 1),
      confidence: ['low', 'medium', 'high'].includes(i.confidence) ? i.confidence : 'medium',
    }));

  if (!items.length) {
    toast('Add at least one item with a name and some nutrition.', true);
    return;
  }

  const btn = $('btn-save');
  btn.disabled = true;
  btn.textContent = 'Saving…';
  try {
    await postJSON('/api/meals', {
      name: $('meal-name').value.trim() || 'Meal',
      meal_type: $('meal-type').value,
      source: draft.source,
      items,
      pending_image: draft.pendingImage,
      model: draft.model,
      raw_json: draft.raw,
      day: selectedMealDay,
    });
    const dayLabel = prettyDate(selectedMealDay);
    toast(selectedMealDay === todayISO() ? 'Meal saved' : `Meal saved for ${dayLabel}`);
    setTimeout(() => {
      location.href = selectedMealDay === todayISO() ? '/' : `/?day=${encodeURIComponent(selectedMealDay)}`;
    }, 600);
  } catch (err) {
    toast(err.message, true);
    btn.disabled = false;
    btn.textContent = 'Save meal';
  }
});

$('btn-cancel').addEventListener('click', () => {
  if (!confirm('Discard this meal?')) return;
  if (previewURL) { URL.revokeObjectURL(previewURL); previewURL = null; }
  draft = { name: 'Meal', items: [], pendingImage: null, model: null, raw: null, source: 'manual' };
  show('step-capture');
});

/**
 * Pick up a photo shared into the app from the phone's camera or gallery.
 * The service worker parked the file in a cache and redirected here, so the
 * whole flow is: shoot in the normal camera app -> share -> macros. No browser.
 */
async function consumeSharedImage() {
  if (new URLSearchParams(location.search).get('share') !== '1') return false;
  // Drop the query string so a reload does not look like a second share.
  history.replaceState(null, '', '/log');
  try {
    const cache = await caches.open('shared-image');
    const res = await cache.match('/__shared-image');
    if (!res) return false;
    await cache.delete('/__shared-image');
    const blob = await res.blob();
    if (!blob.size) return false;
    const name = decodeURIComponent(res.headers.get('X-Filename') || 'shared.jpg');
    await analyze(new File([blob], name, { type: blob.type || 'image/jpeg' }));
    return true;
  } catch {
    return false;
  }
}

// --- Past Meals & Searchable Database ---
let loadedPastMeals = [];
let currentFilter = 'all';
let currentSearchQuery = '';
let searchDebounceTimer = null;

async function fetchPastMeals(q = '') {
  currentSearchQuery = q.trim();
  const listEl = $('past-meals-list');
  if (!listEl) return;

  const sort = currentFilter === 'frequent' ? 'frequent' : 'recent';
  const typeParam = ['breakfast', 'lunch', 'dinner', 'snack'].includes(currentFilter) ? `&meal_type=${currentFilter}` : '';

  try {
    const url = `/api/meals/search?q=${encodeURIComponent(currentSearchQuery)}&sort=${sort}${typeParam}`;
    const data = await getJSON(url);
    loadedPastMeals = data.meals || [];
    renderPastMeals(loadedPastMeals);
    if ($('past-meals-badge')) {
      const count = data.total || loadedPastMeals.length;
      $('past-meals-badge').textContent = `${count} ${count === 1 ? 'meal' : 'meals'}`;
    }
  } catch (err) {
    listEl.innerHTML = `<div class="past-empty-state"><span class="past-empty-icon">⚠️</span>Could not load past meals: ${esc(err.message)}</div>`;
  }
}

function updateRepeatButtonsLabel(label) {
  const listEl = $('past-meals-list');
  if (!listEl) return;
  listEl.querySelectorAll('[data-repeat-id]').forEach(btn => {
    btn.title = `Repeat this meal for ${label}`;
  });
}

function renderPastMeals(meals) {
  const listEl = $('past-meals-list');
  if (!listEl) return;

  let displayList = [...meals];
  if (currentFilter === 'frequent') {
    displayList = displayList.filter(m => (m.times_logged || 1) > 1);
    if (!displayList.length) {
      displayList = [...meals].sort((a, b) => (b.times_logged || 1) - (a.times_logged || 1));
    }
  }

  if (!displayList.length) {
    if (currentSearchQuery) {
      listEl.innerHTML = `
        <div class="past-empty-state">
          <span class="past-empty-icon">🔍</span>
          No past meals found matching "<strong>${esc(currentSearchQuery)}</strong>".<br>
          <button type="button" class="btn" id="btn-reset-search" style="margin-top:10px;font-size:12px;min-height:32px">Clear search</button>
        </div>`;
      $('btn-reset-search')?.addEventListener('click', () => {
        if ($('past-meals-search')) $('past-meals-search').value = '';
        $('btn-clear-search')?.classList.add('hidden');
        fetchPastMeals('');
      });
    } else {
      listEl.innerHTML = `
        <div class="past-empty-state">
          <span class="past-empty-icon">🍽️</span>
          No past meals logged yet.<br>
          <small class="muted">Log your meals above with photos or descriptions, and they'll appear here for easy 1-click repeat!</small>
        </div>`;
    }
    return;
  }

  const targetDayLabel = selectedMealDay === todayISO() ? 'Today' : prettyDate(selectedMealDay);

  listEl.innerHTML = displayList.map(m => {
    const thumb = m.image_url
      ? `<img src="${esc(m.image_url)}" alt="" loading="lazy">`
      : `<span class="thumb-none" aria-hidden="true">${esc(MEAL_ICON[m.meal_type] || '🍽️')}</span>`;

    const itemsSummary = m.items && m.items.length
      ? m.items.map(it => `${getFoodIcon(it.name)} ${it.name}${it.grams ? ` (${round(it.grams, 0)}g)` : ''}`).join(', ')
      : 'No items recorded';

    const lastDayStr = m.last_day ? prettyDate(m.last_day) : (m.day ? prettyDate(m.day) : '');
    const frequentBadge = (m.times_logged && m.times_logged > 1)
      ? `<span class="past-meal-frequent-badge" title="Logged ${m.times_logged} times">&#9733; ${m.times_logged}x</span>`
      : '';

    return `
      <div class="past-meal-card" data-id="${m.id}">
        <div class="past-meal-thumb-wrap">
          ${thumb}
        </div>
        <div class="past-meal-body">
          <div class="past-meal-header">
            <span class="past-meal-title" title="${esc(m.name)}">${esc(m.name)}</span>
            ${frequentBadge}
          </div>
          <div class="past-meal-meta">
            <span class="past-meal-date">${esc(lastDayStr)}</span>
            <span class="past-meal-dot">&middot;</span>
            <span class="past-meal-kcal"><strong>${fmt(m.totals?.calories || 0)}</strong> kcal</span>
            <span class="past-meal-macros">(${fmt(m.totals?.protein_g || 0)}P &middot; ${fmt(m.totals?.carbs_g || 0)}C &middot; ${fmt(m.totals?.fat_g || 0)}F)</span>
          </div>
          <div class="past-meal-items-preview" title="${esc(itemsSummary)}">
            ${esc(itemsSummary)}
          </div>
        </div>
        <div class="past-meal-actions">
          <button type="button" class="btn btn-repeat" data-repeat-id="${m.id}" title="Repeat this meal for ${targetDayLabel}">
            &#x21bb; Repeat
          </button>
          <button type="button" class="btn btn-tweak" data-tweak-id="${m.id}" title="Edit portions before saving">
            &#x270e; Tweak
          </button>
        </div>
      </div>
    `;
  }).join('');

  listEl.querySelectorAll('[data-repeat-id]').forEach(btn => {
    btn.addEventListener('click', () => repeatPastMeal(Number(btn.dataset.repeatId), btn));
  });

  listEl.querySelectorAll('[data-tweak-id]').forEach(btn => {
    btn.addEventListener('click', () => tweakPastMeal(Number(btn.dataset.tweakId)));
  });
}

async function repeatPastMeal(mealId, btnEl) {
  const meal = loadedPastMeals.find(m => m.id === mealId);
  const targetDay = selectedMealDay || todayISO();
  const dayName = targetDay === todayISO() ? 'Today' : prettyDate(targetDay);

  if (btnEl) {
    btnEl.disabled = true;
    btnEl.textContent = 'Logging…';
  }

  try {
    await postJSON(`/api/meals/${mealId}/duplicate?day=${encodeURIComponent(targetDay)}`, {});
    const mealTitle = meal ? `"${meal.name}"` : 'Meal';
    toast(`✓ ${mealTitle} logged for ${dayName}!`);
    setTimeout(() => {
      location.href = targetDay === todayISO() ? '/' : `/?day=${encodeURIComponent(targetDay)}`;
    }, 550);
  } catch (err) {
    toast(err.message, true);
    if (btnEl) {
      btnEl.disabled = false;
      btnEl.innerHTML = '&#x21bb; Repeat';
    }
  }
}

function tweakPastMeal(mealId) {
  const meal = loadedPastMeals.find(m => m.id === mealId);
  if (!meal) return;

  draft = {
    name: meal.name || 'Meal',
    items: (meal.items || []).map(it => ({
      name: it.name,
      grams: round(it.grams, 1),
      calories: round(it.calories, 1),
      protein_g: round(it.protein_g, 1),
      carbs_g: round(it.carbs_g, 1),
      fat_g: round(it.fat_g, 1),
      confidence: it.confidence || 'high',
      basis: it.basis || '',
    })),
    pendingImage: null,
    model: meal.model || null,
    raw: null,
    source: 'manual',
  };

  $('meal-name').value = draft.name;
  if ($('meal-type')) $('meal-type').value = meal.meal_type || inferMealType();

  if (meal.image_url) {
    $('preview').src = meal.image_url;
    $('preview').classList.remove('hidden');
  } else {
    $('preview').classList.add('hidden');
  }
  $('model-notes')?.classList.add('hidden');

  renderItems();
  show('step-review');
  const dayName = selectedMealDay === todayISO() ? 'Today' : prettyDate(selectedMealDay);
  toast(`Editing "${meal.name}" for ${dayName}`);
}

// Search and filter listeners
const pastSearchInput = $('past-meals-search');
const pastClearBtn = $('btn-clear-search');

pastSearchInput?.addEventListener('input', (e) => {
  const val = e.target.value;
  pastClearBtn?.classList.toggle('hidden', !val);
  clearTimeout(searchDebounceTimer);
  searchDebounceTimer = setTimeout(() => {
    fetchPastMeals(val);
  }, 250);
});

pastClearBtn?.addEventListener('click', () => {
  if (pastSearchInput) pastSearchInput.value = '';
  pastClearBtn.classList.add('hidden');
  fetchPastMeals('');
  pastSearchInput?.focus();
});

document.querySelectorAll('.past-filter-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.past-filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentFilter = btn.dataset.filter || 'all';
    fetchPastMeals(pastSearchInput?.value || '');
  });
});

checkHealth();
consumeSharedImage();
fetchPastMeals('');
