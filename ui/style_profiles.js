// Style Profiles Management Logic

let styleProfiles = [];
let currentProfileId = 'default';
Object.defineProperty(window, 'currentProfileId', {
  get: () => currentProfileId,
  set: (val) => { currentProfileId = val; }
});
let activeTab = 'interior';

const SP_BASIC_SLIDERS = [
  { id: 'exposure', label: 'Exposure', min: -100, max: 100, step: 1, default: 0 },
  { id: 'contrast', label: 'Contrast', min: -100, max: 100, step: 1, default: 1 },
  { id: 'highlights', label: 'Highlights', min: -100, max: 100, step: 1, default: 0 },
  { id: 'shadows', label: 'Shadows', min: -100, max: 100, step: 1, default: 0 },
  { id: 'whites', label: 'Whites', min: -100, max: 100, step: 1, default: 0 },
  { id: 'blacks_balance', label: 'Blacks', min: -100, max: 100, step: 1, default: 0 },
  { id: 'white_balance', label: 'Temp', min: -100, max: 100, step: 1, default: 0 },
  { id: 'tint', label: 'Tint', min: -100, max: 100, step: 1, default: 0 },
  { id: 'vibrance', label: 'Vibrance', min: -100, max: 100, step: 1, default: 0 },
  { id: 'saturation', label: 'Saturation', min: -100, max: 100, step: 1, default: 0 },
  { id: 'sharpness', label: 'Sharpness', min: -100, max: 100, step: 1, default: 0 }
];

const SP_COLORS = [
  { id: 0, label: 'Red', color: '#ef4444' },
  { id: 1, label: 'Orange', color: '#f97316' },
  { id: 2, label: 'Yellow', color: '#eab308' },
  { id: 3, label: 'Green', color: '#22c55e' },
  { id: 4, label: 'Aqua', color: '#06b6d4' },
  { id: 5, label: 'Blue', color: '#ec4899' },
  { id: 6, label: 'Purple', color: '#8b5cf6' },
  { id: 7, label: 'Magenta', color: '#ec4899' }
];

let activeColorIdx = 0;

let currentProfileData = {
  interior: getDefaultSPData(),
  exterior: getDefaultSPData()
};

function apiToUiValue(id, apiVal) {
  if (apiVal === undefined) return 0;
  if (id === 'contrast') {
    return Math.round((apiVal - 1) * 200);
  } else if (id === 'white_balance' || id === 'tint') {
    return Math.round(apiVal * 200);
  } else {
    return Math.round(apiVal * 100);
  }
}

function uiToApiValue(id, uiVal) {
  const fVal = parseFloat(uiVal);
  if (id === 'contrast') {
    return parseFloat((1 + fVal / 200).toFixed(2));
  } else if (id === 'white_balance' || id === 'tint') {
    return parseFloat((fVal / 200).toFixed(2));
  } else {
    return parseFloat((fVal / 100).toFixed(2));
  }
}

function getDefaultSPData() {
  let data = {
    distortion: 0,
    vignette: 0,
    vignette_midpoint: 0.5,
    rotate: 0,
    perspective_x: 0,
    perspective_y: 0,
    scale: 1,
    crop_aspect: 0,
    hsl_adjustments: null
  };
  SP_BASIC_SLIDERS.forEach(s => data[s.id] = s.default);
  return data;
}

function initStyleProfilesUI() {
  // Inject basic sliders
  const container = document.getElementById('sp-sliders-container');
  if (!container) return;
  container.innerHTML = '';
  
  SP_BASIC_SLIDERS.forEach(slider => {
    const uiDefault = apiToUiValue(slider.id, slider.default);
    container.innerHTML += `
      <div class="menu-item-group" style="margin-top: 0px;">
        <div class="range-label-row" style="margin-bottom: 8px;">
          <label class="menu-item-label" style="font-size: 13px;">${slider.label}</label>
          <span id="sp-val-${slider.id}" style="font-size: 13px; color: #a1a1aa; font-weight: 500;">${uiDefault}</span>
        </div>
        <input type="range" id="sp-input-${slider.id}" class="menu-item-range" min="${slider.min}" max="${slider.max}" step="${slider.step}" value="${uiDefault}" style="width: 100%;" oninput="handleSPSliderChange('${slider.id}', this.value)">
      </div>
    `;
  });

  // Inject color dots
  const dotsContainer = document.getElementById('sp-color-mixer-dots');
  dotsContainer.innerHTML = '';
  SP_COLORS.forEach((color, idx) => {
    dotsContainer.innerHTML += `
      <button class="btn" id="sp-color-dot-${idx}" style="display: flex; align-items: center; gap: 8px; padding: 6px 12px; background-color: ${idx === 0 ? '#3f3f46' : 'transparent'}; border: 1px solid ${idx === 0 ? '#52525b' : '#3f3f46'}; border-radius: 6px; cursor: pointer;" onclick="switchSPColor(${idx})">
        <div style="width: 12px; height: 12px; border-radius: 50%; background-color: ${color.color}; box-shadow: 0 0 4px rgba(0,0,0,0.5);"></div>
        <span style="font-size: 12px; color: ${idx === 0 ? '#ffffff' : '#a1a1aa'};">${color.label}</span>
      </button>
    `;
  });

  // Inject HSL sliders
  const hslContainer = document.getElementById('sp-color-mixer-sliders');
  ['Hue', 'Saturation', 'Luminance'].forEach((lbl, i) => {
    const key = ['h', 's', 'l'][i];
    hslContainer.innerHTML += `
      <div class="menu-item-group" style="margin-top: 0px;">
        <div class="range-label-row" style="margin-bottom: 8px;">
          <label class="menu-item-label" style="font-size: 13px;">${lbl}</label>
          <span id="sp-val-hsl-${key}" style="font-size: 13px; color: #a1a1aa; font-weight: 500;">0</span>
        </div>
        <input type="range" id="sp-input-hsl-${key}" class="menu-item-range" min="-100" max="100" step="1" value="0" style="width: 100%;" oninput="handleSPHslChange('${key}', this.value)">
      </div>
    `;
  });
}

function handleSPSliderChange(id, value) {
  document.getElementById(`sp-val-${id}`).innerText = value;
  currentProfileData[activeTab][id] = uiToApiValue(id, value);
}

function handleSPHslChange(key, value) {
  document.getElementById(`sp-val-hsl-${key}`).innerText = value;
  if (!currentProfileData[activeTab].hsl_adjustments) {
    currentProfileData[activeTab].hsl_adjustments = { h: Array(8).fill(0), s: Array(8).fill(0), l: Array(8).fill(0) };
  }
  const factor = key === 'h' ? 200 : 100;
  currentProfileData[activeTab].hsl_adjustments[key][activeColorIdx] = parseFloat(value) / factor;
}

function switchSPColor(idx) {
  activeColorIdx = idx;
  SP_COLORS.forEach((c, i) => {
    const dot = document.getElementById(`sp-color-dot-${i}`);
    const span = dot.querySelector('span');
    if (i === idx) {
      dot.style.backgroundColor = '#3f3f46';
      dot.style.borderColor = '#52525b';
      span.style.color = '#ffffff';
    } else {
      dot.style.backgroundColor = 'transparent';
      dot.style.borderColor = '#3f3f46';
      span.style.color = '#a1a1aa';
    }
  });
  updateSPHslUI();
}

function updateSPHslUI() {
  const hsl = currentProfileData[activeTab].hsl_adjustments || { h: Array(8).fill(0), s: Array(8).fill(0), l: Array(8).fill(0) };
  ['h', 's', 'l'].forEach(key => {
    const val = hsl[key][activeColorIdx] || 0;
    const factor = key === 'h' ? 200 : 100;
    const uiVal = Math.round(val * factor);
    document.getElementById(`sp-input-hsl-${key}`).value = uiVal;
    document.getElementById(`sp-val-hsl-${key}`).innerText = uiVal;
  });
}

function updateSPUIFromData() {
  const data = currentProfileData[activeTab];
  SP_BASIC_SLIDERS.forEach(slider => {
    const val = data[slider.id] !== undefined ? data[slider.id] : slider.default;
    const uiVal = apiToUiValue(slider.id, val);
    document.getElementById(`sp-input-${slider.id}`).value = uiVal;
    document.getElementById(`sp-val-${slider.id}`).innerText = uiVal;
  });
  updateSPHslUI();
}

function switchStyleProfileTab(tab) {
  activeTab = tab;
  document.getElementById('sp-tab-interior').style.backgroundColor = tab === 'interior' ? '#ec4899' : 'transparent';
  document.getElementById('sp-tab-interior').style.color = tab === 'interior' ? '#ffffff' : '#a1a1aa';
  
  document.getElementById('sp-tab-exterior').style.backgroundColor = tab === 'exterior' ? '#ec4899' : 'transparent';
  document.getElementById('sp-tab-exterior').style.color = tab === 'exterior' ? '#ffffff' : '#a1a1aa';
  
  updateSPUIFromData();
}

async function loadStyleProfiles() {
  try {
    const res = await window.pywebview.api.autohdr_get_style_profiles();
    if (res && res.ok && res.data && res.data.profiles) {
      styleProfiles = res.data.profiles;
      renderStyleProfileOptions();
    }
  } catch (e) {
    console.error("Error loading style profiles", e);
  }
}

function renderStyleProfileOptions() {
  const sel = document.getElementById('style-profile-select');
  sel.innerHTML = '';
  
  if (styleProfiles.length === 0) {
    sel.innerHTML = '<option value="default">Default</option>';
    currentProfileId = 'default';
  } else {
    let activeProfile = styleProfiles.find(p => p.is_default);
    if (!activeProfile) {
      activeProfile = styleProfiles[0];
    }
    const exists = styleProfiles.some(p => p.id.toString() === currentProfileId);
    if (!exists && currentProfileId !== 'new') {
      currentProfileId = activeProfile.id.toString();
    }
  }

  styleProfiles.forEach(p => {
    const label = p.is_default ? `${p.name} (default)` : p.name;
    sel.innerHTML += `<option value="${p.id}">${label}</option>`;
  });
  sel.innerHTML += '<option value="new">+ New profile</option>';
  sel.value = currentProfileId;
  updateMainUILabel();
  
  if (currentProfileId !== 'default' && currentProfileId !== 'new') {
    const p = styleProfiles.find(x => x.id.toString() === currentProfileId);
    if (p) {
      currentProfileData = {
        interior: Object.assign(getDefaultSPData(), p.interior || {}),
        exterior: Object.assign(getDefaultSPData(), p.exterior || {})
      };
      updateSPUIFromData();
      document.getElementById('sp-btn-rename').style.display = 'block';
      document.getElementById('sp-btn-delete').style.display = 'block';
      document.getElementById('sp-label-default').style.display = 'flex';
      document.getElementById('sp-is-default').checked = true;
    }
  } else {
    document.getElementById('sp-btn-rename').style.display = 'none';
    document.getElementById('sp-btn-delete').style.display = 'none';
    document.getElementById('sp-label-default').style.display = 'none';
  }
}

function updateMainUILabel() {
  const lbl = document.getElementById('wizard-autohdr-profile-label');
  if (!lbl) return;
  
  let selectedProfile = null;
  if (typeof styleProfiles !== 'undefined' && styleProfiles) {
    selectedProfile = styleProfiles.find(p => p.id.toString() === currentProfileId);
  }
  
  if (selectedProfile) {
    lbl.innerText = `Style: ${selectedProfile.name}`;
  } else if (currentProfileId === 'default') {
    lbl.innerText = 'Style: Mặc định';
  } else {
    const defaultProfile = styleProfiles.find(p => p.is_default);
    if (defaultProfile) {
      lbl.innerText = `Style: ${defaultProfile.name}`;
    } else {
      lbl.innerText = 'Style: Mặc định';
    }
  }
}

window.loadStyleProfiles = loadStyleProfiles;
window.updateMainUILabel = updateMainUILabel;

async function handleStyleProfileSelectChange() {
  const sel = document.getElementById('style-profile-select');
  const val = sel.value;
  
  if (val === 'new') {
    const name = await prompt("Nhập tên cho profile mới:");
    if (!name) {
      sel.value = currentProfileId;
      return;
    }
    const payload = {
      name: name,
      interior: getDefaultSPData(),
      exterior: getDefaultSPData()
    };
    
    // Auto-set is_default if creating a new one? Usually no, but we'll see.
    const res = await window.pywebview.api.autohdr_create_style_profile(payload);
    if (res && res.ok) {
      alert("Tạo profile mới thành công!");
      currentProfileId = res.data.id.toString();
      await loadStyleProfiles(); // reload lists and auto-select new profile
    } else {
      alert("Tạo profile thất bại: " + (res.msg || 'Lỗi không xác định'));
      sel.value = currentProfileId;
    }
  } else if (val === 'default') {
    currentProfileId = 'default';
    currentProfileData = { interior: getDefaultSPData(), exterior: getDefaultSPData() };
    updateSPUIFromData();
    updateMainUILabel();
    document.getElementById('sp-btn-rename').style.display = 'none';
    document.getElementById('sp-btn-delete').style.display = 'none';
    document.getElementById('sp-label-default').style.display = 'none';
  } else {
    currentProfileId = val;
    const p = styleProfiles.find(x => x.id.toString() === val);
    if (p) {
      currentProfileData = {
        interior: Object.assign(getDefaultSPData(), p.interior || {}),
        exterior: Object.assign(getDefaultSPData(), p.exterior || {})
      };
      updateSPUIFromData();
      document.getElementById('sp-btn-rename').style.display = 'block';
      document.getElementById('sp-btn-delete').style.display = 'block';
      document.getElementById('sp-label-default').style.display = 'flex';
      document.getElementById('sp-is-default').checked = true;
    }
    updateMainUILabel();
  }
}

async function renameStyleProfile() {
  if (currentProfileId === 'default' || currentProfileId === 'new') return;
  const p = styleProfiles.find(x => x.id.toString() === currentProfileId);
  if (!p) return;
  const newName = await prompt("Nhập tên mới cho profile:", p.name);
  if (!newName || newName === p.name) return;
  const payload = {
    name: newName,
    interior: p.interior || getDefaultSPData(),
    exterior: p.exterior || getDefaultSPData()
  };
  const res = await window.pywebview.api.autohdr_update_style_profile(currentProfileId, payload);
  if (res && res.ok) {
    alert("Đổi tên profile thành công!");
    await loadStyleProfiles();
  } else {
    alert("Đổi tên profile thất bại: " + (res.msg || 'Lỗi không xác định'));
  }
}

async function deleteStyleProfile() {
  if (currentProfileId === 'default' || currentProfileId === 'new') return;
  
  updateMainUILabel();
}

// STYLE SETTINGS LOGIC
let availableModels = [];
let currentStyleSettings = {};

async function loadStyleSettings() {
  try {
    if (availableModels.length === 0) {
      const resModels = await window.pywebview.api.autohdr_get_available_models();
      if (resModels && resModels.ok && resModels.data && resModels.data.models) {
        availableModels = resModels.data.models;
        populateModelsDropdowns();
      }
    }
    
    const resSettings = await window.pywebview.api.autohdr_get_style_settings();
    if (resSettings && resSettings.ok && resSettings.data && resSettings.data.json) {
      currentStyleSettings = resSettings.data.json;
      applyStyleSettingsToUI();
    }
  } catch (e) {
    console.error("Error loading style settings", e);
  }
}

function populateModelsDropdowns() {
  const indoorSel = document.getElementById('setting-indoor-model');
  const outdoorSel = document.getElementById('setting-outdoor-model');
  const twilightSel = document.getElementById('setting-twilight-model');
  
  if (!indoorSel || !outdoorSel || !twilightSel) return;
  
  indoorSel.innerHTML = '';
  outdoorSel.innerHTML = '';
  twilightSel.innerHTML = '';
  
  availableModels.forEach(m => {
    const opt = `<option value="${m.id}" title="${m.description || ''}">${m.name}</option>`;
    
    if (m.family === 'twilight') {
      twilightSel.innerHTML += opt;
    } else if (m.variant === 'indoor') {
      indoorSel.innerHTML += opt;
    } else if (m.variant === 'outdoor') {
      outdoorSel.innerHTML += opt;
    } else {
      outdoorSel.innerHTML += opt;
    }
  });
}

function applyStyleSettingsToUI() {
  const s = currentStyleSettings;
  if (!s) return;
  
  if (s.indoor_model_id !== undefined) document.getElementById('setting-indoor-model').value = s.indoor_model_id;
  if (s.outdoor_model_id !== undefined) document.getElementById('setting-outdoor-model').value = s.outdoor_model_id;
  if (s.default_twilight_model_id !== undefined) document.getElementById('setting-twilight-model').value = s.default_twilight_model_id;
  if (s.tv_screen_replacement !== undefined) document.getElementById('setting-tv-replacement').value = s.tv_screen_replacement;
  
  document.getElementById('setting-fire-fireplace').checked = !!s.add_fire_enabled;
  document.getElementById('setting-declutter').checked = !!s.declutter;
  document.getElementById('setting-deduplication').checked = !!s.deduplication_enabled;
  document.getElementById('setting-walkthrough-reorder').checked = !!s.walkthrough_reorder;
  document.getElementById('setting-perspective').checked = !!s.perspective_correction;
  document.getElementById('setting-grass-replacement').checked = !!s.grass_replacement;
  document.getElementById('setting-sign-removal').checked = !!s.realtor_sign_removal;
  document.getElementById('setting-retain-sky').checked = !!s.retain_original_sky;
}

async function saveStyleSettings() {
  const payload = {
    indoor_model_id: parseInt(document.getElementById('setting-indoor-model').value),
    outdoor_model_id: parseInt(document.getElementById('setting-outdoor-model').value),
    default_twilight_model_id: parseInt(document.getElementById('setting-twilight-model').value),
    tv_screen_replacement: document.getElementById('setting-tv-replacement').value,
    custom_tv_replacement_image: currentStyleSettings.custom_tv_replacement_image || null,
    
    add_fire_enabled: document.getElementById('setting-fire-fireplace').checked,
    declutter: document.getElementById('setting-declutter').checked,
    deduplication_enabled: document.getElementById('setting-deduplication').checked,
    walkthrough_reorder: document.getElementById('setting-walkthrough-reorder').checked,
    walkthrough_reorder_sequence: currentStyleSettings.walkthrough_reorder_sequence || [],
    custom_watermark_url: currentStyleSettings.custom_watermark_url || null,
    custom_filename_enabled: currentStyleSettings.custom_filename_enabled || false,
    file_naming_convention: currentStyleSettings.file_naming_convention || "",
    
    perspective_correction: document.getElementById('setting-perspective').checked,
    grass_replacement: document.getElementById('setting-grass-replacement').checked,
    realtor_sign_removal: document.getElementById('setting-sign-removal').checked,
    retain_original_sky: document.getElementById('setting-retain-sky').checked
  };
  
  const res = await window.pywebview.api.autohdr_update_style_settings(payload);
  if (res && res.ok && res.data && res.data.json && res.data.json.ok) {
    alert("Cập nhật cấu hình model thành công!");
    await loadStyleSettings();
  } else {
    alert("Cập nhật cấu hình thất bại: " + (res.msg || 'Lỗi không xác định'));
  }
}

function resetStyleSettings() {
  currentStyleSettings = {
    tv_screen_replacement: "none",
    custom_tv_replacement_image: null,
    add_fire_enabled: false,
    indoor_model_id: 3,
    outdoor_model_id: 10,
    walkthrough_reorder: false,
    walkthrough_reorder_sequence: [],
    deduplication_enabled: false,
    custom_watermark_url: null,
    custom_filename_enabled: false,
    file_naming_convention: "",
    perspective_correction: true,
    grass_replacement: false,
    declutter: false,
    realtor_sign_removal: false,
    default_twilight_model_id: 7,
    retain_original_sky: false
  };
  applyStyleSettingsToUI();
}

function openStyleProfilesModal() {
  const modal = document.getElementById('style-profiles-modal');
  if (modal) {
    modal.style.display = 'flex';
    if (typeof loadStyleSettings === 'function') {
      loadStyleSettings();
    }
  }
}

function closeStyleProfilesModal() {
  const modal = document.getElementById('style-profiles-modal');
  if (modal) {
    modal.style.display = 'none';
  }
}

window.openStyleProfilesModal = openStyleProfilesModal;
window.closeStyleProfilesModal = closeStyleProfilesModal;

// Initialize on DOM load
window.addEventListener('DOMContentLoaded', () => {
  initStyleProfilesUI();
});
