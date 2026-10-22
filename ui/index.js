/* --- FRONTEND CONTROL FOR CLEAN AUTOFOTELLO --- */

let currentTab = "upload";
let connectionPollInterval = null;
let isRunning = false;
let isUploadRunning = false;
let isDownloadRunning = false;
let isMergeRunning = false;
let currentService = "autohdr";
let selectedInputFiles = [];
let selectedInputBaseDir = "";
let selectedUpscaleFiles = [];
let selectedDteFiles = [];
let detailMergeFolders = {
  original: "",
  ai: "",
  output: "",
};
let wizardParentDir = "";
let wizardOutputTarget = null;
let wizardContext = "upload";
let localUpscaleStatus = null;
let dropboxPickerState = null;

let currentListingPhotos = [];
let selectedPhotoIds = new Set();
let currentPhotoshootId = null;
let currentPhotoshootUserId = null;
let currentListingId = null;
let currentListingService = null;

let authState = null;
let isInitialAuthCheck = true;
let isInitialServiceSet = false;
let isConnectionStatusChecking = false;
const expiredLoginPromptShown = {
  fotello: false,
  autohdr: false,
  autoenhance: false,
};
const missingLoginPromptShown = {
  fotello: false,
  autohdr: false,
  autoenhance: false,
};
const serviceLoginState = {
  fotello: false,
  autohdr: false,
  autoenhance: false,
};

let isAuthChecking = false;

async function verifyAuthLoop() {
  if (!window.pywebview || isAuthChecking) return;
  isAuthChecking = true;
  try {
    const auth = await window.pywebview.api.check_auth_status();
    if (auth) {
      applyAuthStatus(auth);
      isInitialAuthCheck = false;
    }
  } catch (e) {
    console.warn("Auth check failed:", e);
  }
  isAuthChecking = false;
}

async function loadAppVersion() {
  const versionEl = document.getElementById("app-version");
  if (versionEl) {
    versionEl.style.display = "none";
    versionEl.textContent = "";
  }
}

function applyAuthStatus(auth) {
  authState = auth;

  if (!isInitialServiceSet) {
    isInitialServiceSet = true;
    const defaultSvc = determineDefaultService(auth);
    switchService(defaultSvc);
  }

  maybeOfferFirstTour(auth);

  // Update check email button visibility based on admin status
  const btnCheckEmail = document.getElementById("btn-check-email");
  if (btnCheckEmail) {
    const isAdmin = authState && authState.is_admin;
    btnCheckEmail.style.display = (currentService === "autohdr" && isAdmin) ? "inline-block" : "none";
  }
}

function startUpscaleTour() {
  if (typeof introJs !== 'undefined') {
    const intro = introJs();
    intro.setOptions({
      nextLabel: 'Tiếp tục',
      prevLabel: 'Quay lại',
      doneLabel: 'Hoàn tất',
      showProgress: true,
      showBullets: true,
      exitOnOverlayClick: false,
      tooltipClass: 'custom-intro-tooltip',
      steps: [
        {
          element: document.querySelector('#tab-upscale'),
          title: 'Upscale ảnh',
          intro: 'Đây là tính năng Upscale / Làm nét ảnh bằng công nghệ siêu AI (Real-ESRGAN).',
          position: 'right'
        },
        {
          element: document.querySelector('.upscale-dropzone'),
          title: 'Khu vực tải ảnh',
          intro: 'Nhấp chuột hoặc Kéo thả ảnh (hỗ trợ JPG, PNG, WebP) vào đây để bắt đầu làm nét. Ngay sau khi chọn ảnh, hệ thống sẽ mở bảng Thiết lập dự án.',
          position: 'left'
        },
        {
          element: document.querySelector('#btn-start-upscale'),
          title: 'Chọn ảnh từ máy tính',
          intro: 'Hoặc bấm nút này để duyệt tìm ảnh trên máy tính và cấu hình dự án.',
          position: 'top'
        },
        {
          element: document.querySelector('.console-box'),
          title: 'Trạng thái Jobs (Console)',
          intro: 'Tiến độ và log chi tiết quá trình làm nét (tải lên Cloud hoặc xử lý Local) sẽ được hiển thị đầy đủ tại đây.',
          position: 'top'
        }
      ]
    });
    intro.start();
  }
}
window.startUpscaleTour = startUpscaleTour;

function toggleAdvancedSettings() {
  const content = document.getElementById("advanced-settings-content");
  const icon = document.getElementById("advanced-toggle-icon");
  if (!content || !icon) return;

  const isHidden = content.classList.contains("hidden");
  if (isHidden) {
    content.classList.remove("hidden");
    icon.textContent = "▲ Thu gọn";
  } else {
    content.classList.add("hidden");
    icon.textContent = "▼ Mở rộng";
  }
}

function toggleFotelloAdvancedSettings() {
  const content = document.getElementById("fotello-advanced-settings-content");
  const icon = document.getElementById("fotello-advanced-toggle-icon");
  if (!content || !icon) return;

  const isHidden = content.classList.contains("hidden");
  if (isHidden) {
    content.classList.remove("hidden");
    icon.textContent = "▲ Thu gọn";
  } else {
    content.classList.add("hidden");
    icon.textContent = "▼ Mở rộng";
  }
}

function toggleServiceDropdown(event) {
  if (event) event.stopPropagation();
  const dropdown = document.getElementById("service-dropdown");
  if (dropdown) {
    dropdown.classList.toggle("open");
  }
}

async function selectDropdownService(service) {
  const dropdown = document.getElementById("service-dropdown");
  if (dropdown) {
    dropdown.classList.remove("open");
  }
  await switchService(service);
}

function determineDefaultService() {
  const serviceIds = ["autohdr", "autoenhance", "fotello"];

  const lastSvc = localStorage.getItem("last_selected_service");
  if (lastSvc && serviceIds.includes(lastSvc)) {
    return lastSvc;
  }

  const counts = {
    fotello: parseInt(localStorage.getItem("use_count_fotello") || "0", 10),
    autohdr: parseInt(localStorage.getItem("use_count_autohdr") || "0", 10),
    autoenhance: parseInt(localStorage.getItem("use_count_autoenhance") || "0", 10)
  };

  let maxService = "autohdr";
  let maxCount = -1;
  for (const svc of serviceIds) {
    if (counts[svc] > maxCount) {
      maxCount = counts[svc];
      maxService = svc;
    }
  }

  return maxService;
}

// Close dropdown when clicking outside
document.addEventListener("click", function(event) {
  const dropdown = document.getElementById("service-dropdown");
  if (dropdown && !dropdown.contains(event.target)) {
    dropdown.classList.remove("open");
  }
});

async function switchService(service) {
  currentService = service;

  // Track service selection and usage
  localStorage.setItem("last_selected_service", service);
  let count = parseInt(localStorage.getItem(`use_count_${service}`) || "0", 10);
  localStorage.setItem(`use_count_${service}`, (count + 1).toString());

  // Toggle active styling on dropdown items
  document
    .querySelectorAll(".service-dropdown-item")
    .forEach((el) => el.classList.remove("active"));
  
  const activeItem = document.getElementById(`svc-${service}`);
  if (activeItem) {
    activeItem.classList.add("active");
  }

  // Update check email button visibility
  const btnCheckEmail = document.getElementById("btn-check-email");
  if (btnCheckEmail) {
    const isAdmin = authState && authState.is_admin;
    btnCheckEmail.style.display = (service === "autohdr" && isAdmin) ? "inline-block" : "none";
  }

  // Update active trigger text and logo
  const activeLogo = document.getElementById("dropdown-active-logo");
  const activeName = document.getElementById("dropdown-active-name");
  if (activeLogo && activeName) {
    activeName.textContent = service === "fotello" ? "Fotello" : (service === "autoenhance" ? "Autoenhance" : "AutoHDR");

    if (service === "fotello") {
      activeLogo.src = "assets/fotelo-logo.webp";
    } else if (service === "autohdr") {
      activeLogo.src = "assets/autohdr-logo.webp";
    } else if (service === "autoenhance") {
      activeLogo.src = "assets/autoenhance-logo.svg";
    }
  }

  // Update header title dynamically
  const appTitle = document.getElementById("app-title");
  if (appTitle) {
    appTitle.textContent = "T&T";
  }

  // Keep the app logo consistent across services
  const appLogo = document.getElementById("app-logo");
  if (appLogo) {
    appLogo.src = "assets/autofotello-app-logo.png";
  }

  const uploadLogo = document.getElementById("upload-service-logo");
  if (uploadLogo) {
    uploadLogo.src = service === "fotello" ? "assets/fotelo-logo.webp" : (service === "autoenhance" ? "assets/autoenhance-logo.svg" : "assets/autohdr-logo.webp");
  }
  const downloadLogo = document.getElementById("download-service-logo");
  if (downloadLogo) {
    downloadLogo.src = service === "fotello" ? "assets/fotelo-logo.webp" : (service === "autoenhance" ? "assets/autoenhance-logo.svg" : "assets/autohdr-logo.webp");
  }

  // Update header buttons text based on service
  resetConnectButton();

  // Instantly re-verify and update status badge
  checkConnectionStatus();

  // Re-verify service lock overlay
  if (authState) {
    applyAuthStatus(authState);
  }

  // Toggle visible fields in Wizard Modal
  const wizFotelloPrefs = document.getElementById("wizard-fotello-prefs");
  const wizAutohdrPrefs = document.getElementById("wizard-autohdr-prefs");
  const topBtn = document.getElementById("btn-top-manage-profiles");
  if (wizFotelloPrefs && wizAutohdrPrefs) {
    if (service === "fotello") {
      wizFotelloPrefs.classList.remove("hidden");
      wizAutohdrPrefs.classList.add("hidden");
      if (topBtn) topBtn.style.display = "none";
    } else if (service === "autohdr") {
      wizFotelloPrefs.classList.add("hidden");
      wizAutohdrPrefs.classList.remove("hidden");
      if (topBtn) topBtn.style.display = "flex";
    } else {
      wizFotelloPrefs.classList.add("hidden");
      wizAutohdrPrefs.classList.add("hidden");
      if (topBtn) topBtn.style.display = "none";
    }
  }
  updateWizardModelLabel();

  // If in download tab, reload listings for the selected service
  if (currentService === service && currentTab === "download") {
    refreshListings();
  }
}

// ═══ WEBVIEW BRIDGE INITIALIZATION ═══

async function waitForPyWebView(maxWait = 5000) {
  if (window.pywebview && window.pywebview.api) return true;
  return new Promise((resolve) => {
    let resolved = false;
    window.addEventListener("pywebviewready", () => {
      if (!resolved) {
        resolved = true;
        resolve(true);
      }
    });
    const start = Date.now();
    const poll = setInterval(() => {
      if (window.pywebview && window.pywebview.api) {
        clearInterval(poll);
        if (!resolved) {
          resolved = true;
          resolve(true);
        }
      } else if (Date.now() - start > maxWait) {
        clearInterval(poll);
        if (!resolved) {
          resolved = true;
          resolve(false);
        }
      }
    }, 100);
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  const ready = await waitForPyWebView();
  if (ready) {
    try {
      await loadPersistentGuideState();
      // 0. Show local app version in header as soon as the webview bridge is ready.
      await loadAppVersion();

      // 1. Check authorization status and start verification loop
      await verifyAuthLoop();

      // 1.2 Check software updates
      checkAppUpdate();
      refreshLocalUpscaleStatus();

      // 2. Try auto reconnect (Fotello & AutoHDR)
      addLog("Đang khôi phục đăng nhập đã lưu...", "info");
      const res = await window.pywebview.api.fotello_reconnect();
      if (res.ok) {
        addLog("Đã khôi phục phiên đăng nhập Fotello trước đó!", "success");
      }

      const resAuto = await window.pywebview.api.autohdr_reconnect();
      if (resAuto.ok) {
        addLog("Đã khôi phục phiên đăng nhập AutoHDR trước đó!", "success");
      }

      const resEnhance = await window.pywebview.api.autoenhance_reconnect();
      if (resEnhance && resEnhance.ok) {
        addLog("Đã khôi phục phiên đăng nhập Autoenhance trước đó!", "success");
      }

      // Load style profiles on startup
      if (typeof loadStyleProfiles === "function") {
        try {
          await loadStyleProfiles();
        } catch (err) {}
      }

      // 3. Start polling connection status
      checkConnectionStatus();
      connectionPollInterval = setInterval(checkConnectionStatus, 10000);
    } catch (e) {
      console.warn("Init error:", e);
    }
  } else {
    addLog(
      "⚠ PyWebView chưa sẵn sàng. Bạn đang mở trực tiếp bằng trình duyệt?",
      "warn",
    );
  }
});

// ═══ EXPOSED BRIDGE FUNCTIONS (Called by Python) ═══

window.clearLogs = function () {
  const tabsContainer = document.getElementById("log-tabs");
  const outputsContainer = document.getElementById("console-outputs");
  if (!tabsContainer || !outputsContainer) return;

  // Remove all non-global tabs and outputs
  Array.from(tabsContainer.children).forEach((child) => {
    if (child.id !== "tab-global") child.remove();
  });
  Array.from(outputsContainer.children).forEach((child) => {
    if (child.id !== "console-output-global") child.remove();
    else child.innerHTML = "";
  });

  switchLogTab("global");
};

window.clearCompletedSessions = async function () {
  if (!window.pywebview || !window.pywebview.api) return;
  try {
    const activeIds = await window.pywebview.api.get_active_session_ids() || [];
    const tabsContainer = document.getElementById("log-tabs");
    const outputsContainer = document.getElementById("console-outputs");
    if (!tabsContainer || !outputsContainer) return;

    let removedCount = 0;
    Array.from(tabsContainer.children).forEach((child) => {
      if (child.id === "tab-global") return;
      const sessionId = child.id.replace("tab-", "");
      if (!activeIds.includes(sessionId)) {
        child.remove();
        const outputDiv = document.getElementById(`console-output-${sessionId}`);
        if (outputDiv) outputDiv.remove();
        delete _sessionProgress[sessionId];
        removedCount++;
      }
    });

    if (removedCount > 0) {
      const activeTab = document.querySelector(".log-tab.active");
      if (!activeTab) {
        switchLogTab("global");
      }
      addLog(`🧹 Đã xóa ${removedCount} luồng đã hoàn thành.`, "info");
    } else {
      addLog("ℹ Không có luồng đã hoàn thành nào để xóa.", "info");
    }
  } catch (e) {
    console.error("Error clearing completed sessions:", e);
  }
};

window.copyActiveLog = async function () {
  const activeOutput = document.querySelector(".console-output.active");
  const text = activeOutput ? activeOutput.innerText.trim() : "";
  const btn = document.getElementById("btn-copy-log");
  const oldText = btn ? btn.textContent : "";

  if (!text) {
    addLog("⚠ Log hiện tại đang trống.", "warn");
    return;
  }

  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.setAttribute("readonly", "");
      textarea.style.position = "fixed";
      textarea.style.left = "-9999px";
      textarea.style.top = "0";
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand("copy");
      textarea.remove();
    }

    if (btn) {
      btn.textContent = "Đã copy";
      setTimeout(() => {
        btn.textContent = oldText || "Copy log";
      }, 1200);
    }
  } catch (err) {
    console.error("Failed to copy log:", err);
    addLog(`⚠ Không copy được log: ${err}`, "warn");
  }
};

window.showFolderBtn = function (sessionId, folderPath) {
  if (_closedLogSessions.has(sessionId)) return;

  const tabBtn = document.getElementById(`tab-${sessionId}`);
  if (!tabBtn) return;
  if (tabBtn.querySelector(".tab-folder-btn")) return; // already added
  const btn = document.createElement("button");
  btn.className = "tab-folder-btn";
  btn.title = folderPath;
  btn.textContent = "📁";
  btn.style =
    "margin-left:6px; background:none; border:none; cursor:pointer; font-size:14px; padding:0 2px; opacity:0.8;";
  btn.onclick = function (e) {
    e.stopPropagation();
    if (window.pywebview) window.pywebview.api.open_folder(folderPath);
  };
  tabBtn.appendChild(btn);
};

window.switchLogTab = function (sessionId) {
  const tabs = document.querySelectorAll(".log-tab");
  const outputs = document.querySelectorAll(".console-output");

  tabs.forEach((t) => t.classList.remove("active"));
  outputs.forEach((o) => o.classList.remove("active"));

  const targetTab = document.getElementById(`tab-${sessionId}`);
  const targetOutput = document.getElementById(`console-output-${sessionId}`);

  if (targetTab) targetTab.classList.add("active");
  if (targetOutput) {
    targetOutput.classList.add("active");
    targetOutput.scrollTop = targetOutput.scrollHeight;
  }

  // Restore progress bar to this session's last known state
  const p = _sessionProgress[sessionId];
  if (p) {
    document.getElementById("progress-pct").textContent = `${p.pct}%`;
    document.getElementById("progress-bar-fill").style.width = `${p.pct}%`;
    document.getElementById("progress-status-text").textContent =
      p.statusText || `Đang xử lý: ${p.current}/${p.total} ảnh`;
    document.getElementById("progress-container").classList.remove("hidden");
  } else {
    document.getElementById("progress-pct").textContent = "";
    document.getElementById("progress-bar-fill").style.width = "0%";
    document.getElementById("progress-status-text").textContent =
      "Đang chạy...";
    if (!isRunning)
      document.getElementById("progress-container").classList.add("hidden");
  }
};

window.addLog = function (
  msg,
  type = "info",
  sessionId = "global",
  sessionName = null,
) {
  if (_closedLogSessions.has(sessionId)) return;

  let output = document.getElementById(`console-output-${sessionId}`);

  // Create tab if it doesn't exist
  if (!output) {
    const tabsContainer = document.getElementById("log-tabs");
    const outputsContainer = document.getElementById("console-outputs");
    if (!tabsContainer || !outputsContainer) return;

    // Create Tab Button
    const tabBtn = document.createElement("button");
    tabBtn.className = "log-tab";
    tabBtn.id = `tab-${sessionId}`;
    tabBtn.onclick = () => switchLogTab(sessionId);

    let logoHtml = "";
    let appPrefix = "";
    const lowerName = (sessionName || "").toLowerCase();
    const lowerId = (sessionId || "").toLowerCase();
    
    if (lowerName.includes("autoenhance") || lowerId.includes("_ae_") || lowerId.startsWith("dl_ae") || lowerId.startsWith("up_ae")) {
      logoHtml = `<img src="assets/autoenhance-logo.svg" style="width: 14px; height: 14px; margin-right: 6px; vertical-align: middle; border-radius: 2px; object-fit: contain;">`;
      if (!lowerName.includes("autoenhance")) appPrefix = `<span style="font-weight: 500; font-size: 11px; margin-right: 4px; opacity: 0.8; color: var(--text-muted)">Autoenhance:</span>`;
    } else if (lowerName.includes("autohdr") || lowerId.includes("_a_") || lowerId.startsWith("dl_a") || lowerId.startsWith("up_a") || lowerId.startsWith("dl_sel_")) {
      logoHtml = `<img src="assets/autohdr-logo.webp" style="width: 14px; height: 14px; margin-right: 6px; vertical-align: middle; border-radius: 2px; object-fit: contain;">`;
      if (!lowerName.includes("autohdr")) appPrefix = `<span style="font-weight: 500; font-size: 11px; margin-right: 4px; opacity: 0.8; color: var(--text-muted)">AutoHDR:</span>`;
    } else if (lowerName.includes("fotello") || lowerId.includes("_f_") || lowerId.startsWith("dl_f") || lowerId.startsWith("up_f") || lowerId.startsWith("analyze_f")) {
      logoHtml = `<img src="assets/fotelo-logo.webp" style="width: 14px; height: 14px; margin-right: 6px; vertical-align: middle; border-radius: 2px; object-fit: contain;">`;
      if (!lowerName.includes("fotello")) appPrefix = `<span style="font-weight: 500; font-size: 11px; margin-right: 4px; opacity: 0.8; color: var(--text-muted)">Fotello:</span>`;
    }

    const titleSpan = document.createElement("span");
    titleSpan.className = "tab-title";
    titleSpan.innerHTML = logoHtml + appPrefix + (sessionName || sessionId);
    titleSpan.style.display = "flex";
    titleSpan.style.alignItems = "center";
    tabBtn.appendChild(titleSpan);

    if (sessionId !== "global") {
      const closeBtn = document.createElement("span");
      closeBtn.className = "close-btn";
      closeBtn.textContent = "×";
      closeBtn.title = "Dừng và xóa luồng này";
      closeBtn.onclick = (e) => {
        e.stopPropagation();
        closeLogSession(sessionId);
      };
      tabBtn.appendChild(closeBtn);
    }

    tabsContainer.appendChild(tabBtn);

    // Create Output Div
    output = document.createElement("div");
    output.className = "console-output";
    output.id = `console-output-${sessionId}`;
    outputsContainer.appendChild(output);

    if (sessionId !== "global") {
      switchLogTab(sessionId);
    }
  }

  const line = document.createElement("div");
  line.className = "log-line " + type;

  const now = new Date();
  const ts = now.toLocaleTimeString("vi-VN", { hour12: false });
  line.textContent = `[${ts}] ${msg}`;

  output.appendChild(line);

  // Auto-scroll if it's currently visible
  if (output.classList.contains("active")) {
    output.scrollTop = output.scrollHeight;
  }
};

function playSuccessChime() {
  try {
    const AudioContext = window.AudioContext || window.webkitAudioContext;
    if (!AudioContext) return;
    const ctx = new AudioContext();

    // Note 1: C5 (523.25 Hz)
    const osc1 = ctx.createOscillator();
    const gain1 = ctx.createGain();
    osc1.connect(gain1);
    gain1.connect(ctx.destination);
    osc1.type = "sine";
    osc1.frequency.setValueAtTime(523.25, ctx.currentTime);
    gain1.gain.setValueAtTime(0.2, ctx.currentTime);
    gain1.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.15);

    // Note 2: E5 (659.25 Hz) after 0.12s
    const osc2 = ctx.createOscillator();
    const gain2 = ctx.createGain();
    osc2.connect(gain2);
    gain2.connect(ctx.destination);
    osc2.type = "sine";
    osc2.frequency.setValueAtTime(659.25, ctx.currentTime + 0.12);
    gain2.gain.setValueAtTime(0.2, ctx.currentTime + 0.12);
    gain2.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.4);

    osc1.start(ctx.currentTime);
    osc1.stop(ctx.currentTime + 0.15);
    osc2.start(ctx.currentTime + 0.12);
    osc2.stop(ctx.currentTime + 0.4);
  } catch (e) {
    console.error("Failed to play success sound:", e);
  }
}

function showLoginSuccessModal(service = currentService, customMsg = null) {
  const serviceName = serviceDisplayName(service);
  const title = document.getElementById("login-success-title");
  const message = document.getElementById("login-success-message");
  const modal = document.getElementById("login-success-modal");

  if (title) title.textContent = `Đăng nhập ${serviceName} thành công`;
  if (message) {
    message.textContent = customMsg || `${serviceName} đã sẵn sàng để upload, xử lý và tải ảnh.`;
  }

  playSuccessChime();

  if (!modal) {
    window.alert(`Đăng nhập ${serviceName} thành công\n${customMsg || ''}`);
    return;
  }

  modal.classList.remove("hidden");
  setTimeout(() => {
    modal.classList.add("hidden");
  }, 2500);
}

function closeLoginSuccessModal() {
  const modal = document.getElementById("login-success-modal");
  if (modal) modal.classList.add("hidden");
}

window.setStatus = function (state, text, type = "upload", sessionName = null) {
  const wasRunning = type === "upload" ? isUploadRunning : (type === "merge" ? isMergeRunning : isDownloadRunning);

  if (type === "upload") {
    if (state === "running") {
      isUploadRunning = true;
      document.getElementById("btn-stop-upload").classList.remove("hidden");
      const btnStopUpscale = document.getElementById("btn-stop-upscale");
      if (btnStopUpscale) btnStopUpscale.classList.remove("hidden");
      const btnStartUpscale = document.getElementById("btn-start-upscale");
      if (btnStartUpscale) btnStartUpscale.disabled = true;
      document.getElementById("progress-container").classList.remove("hidden");
    } else {
      isUploadRunning = false;
      document.getElementById("btn-stop-upload").classList.add("hidden");
      const btnStopUpscale = document.getElementById("btn-stop-upscale");
      if (btnStopUpscale) btnStopUpscale.classList.add("hidden");
      const btnStartUpscale = document.getElementById("btn-start-upscale");
      if (btnStartUpscale) btnStartUpscale.disabled = false;
      if (!isDownloadRunning && !isMergeRunning) {
        document.getElementById("progress-container").classList.add("hidden");
      }
    }
  } else if (type === "download") {
    if (state === "running") {
      isDownloadRunning = true;
      document.getElementById("btn-stop-download").classList.remove("hidden");
      document.getElementById("progress-container").classList.remove("hidden");
    } else {
      isDownloadRunning = false;
      document.getElementById("btn-stop-download").classList.add("hidden");
      if (!isUploadRunning && !isMergeRunning) {
        document.getElementById("progress-container").classList.add("hidden");
      }
    }
  } else if (type === "merge") {
    const btnStopMerge = document.getElementById("btn-stop-merge");
    const btnStartMerge = document.getElementById("btn-start-merge");
    if (state === "running") {
      isMergeRunning = true;
      if (btnStopMerge) btnStopMerge.classList.remove("hidden");
      if (btnStartMerge) btnStartMerge.disabled = false;
      document.getElementById("progress-container").classList.remove("hidden");
    } else {
      isMergeRunning = false;
      if (btnStopMerge) btnStopMerge.classList.add("hidden");
      if (btnStartMerge) btnStartMerge.disabled = false;
      if (!isUploadRunning && !isDownloadRunning) {
        document.getElementById("progress-container").classList.add("hidden");
      }
    }
  }

  // Detect if a running task has successfully finished
  if (wasRunning && state === "idle" && text === "Hoàn tất") {
    playSuccessChime();
    if (
      window.pywebview &&
      window.pywebview.api &&
      window.pywebview.api.notify_user
    ) {
      const taskLabel =
        type === "upload" ? "Xử lý HDR thành công" : (type === "merge" ? "Merge chi tiết hoàn tất" : "Tải ảnh hoàn tất");
      const detail = sessionName ? `${sessionName}` : "Dự án ảnh của bạn";
      window.pywebview.api.notify_user(taskLabel, `Đã hoàn tất: ${detail}`);
    }
  }

  isRunning = isUploadRunning || isDownloadRunning || isMergeRunning;
  document.getElementById("progress-status-text").textContent = text;
};

const _sessionProgress = {};
const _closedLogSessions = new Set();

window.closeLogSession = async function (sessionId) {
  if (!sessionId || sessionId === "global") return;
  if (!await confirm("Bạn có chắc chắn muốn đóng tab này và hủy tác vụ không?")) return;

  _closedLogSessions.add(sessionId);

  const tab = document.getElementById(`tab-${sessionId}`);
  const output = document.getElementById(`console-output-${sessionId}`);
  const wasActive = tab && tab.classList.contains("active");

  if (tab) tab.remove();
  if (output) output.remove();
  delete _sessionProgress[sessionId];

  if (wasActive) {
    switchLogTab("global");
  }

  if (
    window.pywebview &&
    window.pywebview.api &&
    window.pywebview.api.stop_session
  ) {
    try {
      const res = await window.pywebview.api.stop_session(sessionId);
      if (!res || !res.ok) {
        _closedLogSessions.delete(sessionId);
      }
    } catch (e) {
      _closedLogSessions.delete(sessionId);
      console.warn(`Failed to stop session ${sessionId}:`, e);
    }
  } else {
    _closedLogSessions.delete(sessionId);
  }
};

window.updateProgress = function (current, total, pct, sessionId = "global") {
  if (_closedLogSessions.has(sessionId)) return;
  const previous = _sessionProgress[sessionId] || {};
  _sessionProgress[sessionId] = {
    ...previous,
    current,
    total,
    pct,
  };

  if (sessionId.startsWith("engine_")) {
    const textEl = document.getElementById("local-engine-progress-text");
    const pctEl = document.getElementById("local-engine-progress-pct");
    const fillEl = document.getElementById("local-engine-progress-fill");
    if (textEl) textEl.textContent = `Đang tải... ${current}/${total}`;
    if (pctEl) pctEl.textContent = `${pct}%`;
    if (fillEl) fillEl.style.width = `${pct}%`;
    return;
  }

  // Update badge on the tab button
  const tabBtn = document.getElementById(`tab-${sessionId}`);
  if (tabBtn) {
    let badge = tabBtn.querySelector(".tab-progress-badge");
    if (!badge) {
      badge = document.createElement("span");
      badge.className = "tab-progress-badge";
      badge.style =
        "margin-left:6px; font-size:11px; color:#f9a8d4; font-weight:600;";
      tabBtn.appendChild(badge);
    }
    if (current >= total && total > 0) {
      badge.textContent = "✓";
      setTimeout(() => {
        badge.textContent = "";
      }, 4000);
    } else {
      badge.textContent = `${current}/${total}`;
    }
  }

  // Only update the global progress bar if this session's tab is active
  const activeOutput = document.querySelector(".console-output.active");
  const activeSessionId = activeOutput
    ? activeOutput.id.replace("console-output-", "")
    : "global";
  if (activeSessionId !== sessionId) return;

  document.getElementById("progress-pct").textContent = `${pct}%`;
  document.getElementById("progress-bar-fill").style.width = `${pct}%`;
  if (!_sessionProgress[sessionId].statusText) {
    document.getElementById("progress-status-text").textContent =
      `Đang xử lý: ${current}/${total} ảnh`;
  }

  if (current >= total && total > 0) {
    setTimeout(() => {
      if (!isRunning) {
        document.getElementById("progress-container").classList.add("hidden");
      }
    }, 4000);
  }
};

window.updateSessionStatus = function (text, sessionId = "global") {
  if (_closedLogSessions.has(sessionId)) return;
  const previous = _sessionProgress[sessionId] || {
    current: 0,
    total: 0,
    pct: 0,
  };
  _sessionProgress[sessionId] = {
    ...previous,
    statusText: text,
  };

  const activeOutput = document.querySelector(".console-output.active");
  const activeSessionId = activeOutput
    ? activeOutput.id.replace("console-output-", "")
    : "global";
  if (activeSessionId === sessionId) {
    document.getElementById("progress-status-text").textContent = text;
  }
};

// ═══ CORE ACTION FUNCTIONS ═══

function serviceDisplayName(service = currentService) {
  return service === "fotello" ? "Fotello" : (service === "autoenhance" ? "Autoenhance" : "AutoHDR");
}

async function isServiceLoggedIn(service = currentService) {
  const status = await getServiceLoginStatus(service);
  return Boolean(status && status.connected);
}

async function getServiceLoginStatus(service = currentService) {
  if (!window.pywebview || !window.pywebview.api) return false;
  try {
    return (
      service === "fotello"
        ? await window.pywebview.api.fotello_status()
        : (service === "autoenhance"
          ? await window.pywebview.api.autoenhance_status()
          : await window.pywebview.api.autohdr_status())
    );
  } catch (e) {
    console.error("Login status check failed:", e);
    return {
      connected: Boolean(serviceLoginState[service]),
      auth_expired: false,
      transient_error: true,
      msg: String(e),
    };
  }
}

function statusHasSavedLogin(service, status) {
  if (!status) return false;
  if (service === "fotello") return Boolean(status.has_saved_token);
  if (service === "autoenhance") return Boolean(status.has_saved_cookie);
  return Boolean(status.has_saved_cookie);
}

function statusAuthExpired(service, status) {
  if (!status || status.connected) return false;
  if (service === "autohdr" || service === "autoenhance") return status.auth_expired === true;
  return statusHasSavedLogin(service, status);
}

async function promptExpiredLoginIfNeeded(service, status) {
  if (
    !status ||
    status.connected ||
    !statusAuthExpired(service, status) ||
    !statusHasSavedLogin(service, status)
  ) {
    return;
  }
  if (expiredLoginPromptShown[service]) return;

  expiredLoginPromptShown[service] = true;
  const serviceName = serviceDisplayName(service);
  const msg =
    status.msg ||
    `Phiên đăng nhập ${serviceName} đã hết hạn. Vui lòng đăng nhập lại.`;
  addLog(`⚠ ${msg}`, "warn");
}

async function promptMissingLoginIfNeeded(service, actionText = "sử dụng tính năng này") {
  const status = await getServiceLoginStatus(service);
  if (status && status.connected) {
    missingLoginPromptShown[service] = false;
    return true;
  }

  if (statusAuthExpired(service, status) && statusHasSavedLogin(service, status)) {
    const serviceName = serviceDisplayName(service);
    const msg =
      status.msg ||
      `Phiên đăng nhập ${serviceName} đã hết hạn. Vui lòng đăng nhập lại.`;
    addLog(`⚠ ${msg}`, "warn");
    return false;
  }

  const serviceName = serviceDisplayName(service);
  addLog(`⚠ Cần đăng nhập ${serviceName} để ${actionText}.`, "warn");
  return false;
}

async function requireServiceLogin(actionText = "sử dụng tính năng này") {
  const service = currentService;
  const status = await getServiceLoginStatus(service);
  if (status && status.connected) {
    missingLoginPromptShown[service] = false;
    return true;
  }

  return promptMissingLoginIfNeeded(service, actionText);
}

async function pyConnect(service = currentService) {
  if (!window.pywebview) return;
  const btn = document.getElementById("btn-connect");
  const serviceName = serviceDisplayName(service);
  if (btn) {
    btn.textContent = `Đang đăng nhập ${serviceName}...`;
  }

  try {
    addLog(`Đang mở Chrome để đăng nhập ${serviceName}...`, "info");
    const res = await window.pywebview.api.start_connection_flow(service);
    if (!res.ok) {
      addLog(
        `Lỗi: ${res.msg || `Không thể bắt đầu đăng nhập ${serviceName}.`}`,
        "error",
      );
      resetConnectButton();
    }
  } catch (e) {
    addLog(`Lỗi: ${e}`, "error");
    resetConnectButton();
  }
}

async function pyLogout(service = currentService) {
  if (!window.pywebview || !window.pywebview.api) return;
  const btn = document.getElementById("btn-connect");
  const serviceName = serviceDisplayName(service);
  if (btn) {
    btn.disabled = true;
    btn.textContent = `Đang đăng xuất ${serviceName}...`;
    btn.onmouseenter = null;
    btn.onmouseleave = null;
  }

  try {
    const res =
      service === "fotello"
        ? await window.pywebview.api.fotello_logout()
        : (service === "autoenhance"
          ? await window.pywebview.api.autoenhance_logout()
          : await window.pywebview.api.autohdr_logout());
    if (res && res.ok) {
      serviceLoginState[service] = false;
      addLog(`Đã đăng xuất ${serviceName}.`, "success");
    } else {
      addLog(
        `⚠ Không đăng xuất được ${serviceName}: ${res?.msg || "Lỗi không xác định"}`,
        "warn",
      );
    }
  } catch (e) {
    addLog(`Lỗi đăng xuất ${serviceName}: ${e}`, "error");
  } finally {
    resetConnectButton();
  }
}

async function toggleServiceLogin() {
  const service = currentService;
  if (serviceLoginState[service]) {
    await pyLogout(service);
  } else {
    await pyConnect(service);
  }
}

function resetConnectButton() {
  const btn = document.getElementById("btn-connect");
  if (!btn) return;
  btn.disabled = false;
  checkConnectionStatus();
}

window.onConnectionSuccess = async function (service = currentService) {
  serviceLoginState[service] = true;
  resetConnectButton();
  
  const status = await getServiceLoginStatus(service);
  const customMsg = status && status.msg ? status.msg : null;
  
  showLoginSuccessModal(service, customMsg);
  checkConnectionStatus();
  if (currentTab === "download") {
    refreshListings();
  }
};

window.onConnectionFailed = function (msg) {
  resetConnectButton();
};

async function checkConnectionStatus() {
  if (!window.pywebview || isConnectionStatusChecking) return;
  isConnectionStatusChecking = true;
  try {
    const btn = document.getElementById("btn-connect");
    const service = currentService;
    const status = await getServiceLoginStatus(service);
    if (service !== currentService) return;
    const isConnected = Boolean(status && status.connected);
    serviceLoginState[service] = isConnected;

    if (isConnected) {
      expiredLoginPromptShown[service] = false;
      missingLoginPromptShown[service] = false;
      if (btn && !btn.disabled) {
        let displayEmail = null;
        if (status && status.msg) {
          const emailMatch = status.msg.match(/OK:\s*(.*?)\s*\(id:/);
          if (emailMatch && emailMatch[1] && emailMatch[1] !== "Unknown") {
            displayEmail = emailMatch[1];
          }
        }
        
        if (displayEmail) {
          btn.dataset.defaultText = displayEmail;
          btn.dataset.hoverText = `Đăng xuất ${serviceDisplayName(service)}`;
          btn.textContent = displayEmail;
          btn.onmouseenter = function() { this.textContent = this.dataset.hoverText; };
          btn.onmouseleave = function() { this.textContent = this.dataset.defaultText; };
        } else {
          btn.textContent = `Đăng xuất ${serviceDisplayName(service)}`;
          btn.onmouseenter = null;
          btn.onmouseleave = null;
        }
      }
      
      const creditDisplay = document.getElementById("autohdr-credit-display");
      const creditText = document.getElementById("autohdr-credit-text");
      if (service === "autohdr" && status.user_id) {
        syncFreeCreditsGiftStatus(status);
        
        // Parse and display credits if available in status msg
        if (creditDisplay && creditText && status.msg) {
          const match = status.msg.match(/-\s*([\d,]+)\s*credit/i);
          if (match) {
            creditText.innerHTML = `<b>${match[1]}</b> photos`;
            creditDisplay.style.display = "flex";
          } else {
            creditDisplay.style.display = "none";
          }
        }
      } else if (creditDisplay) {
        creditDisplay.style.display = "none";
      }
    } else {
      if (btn && !btn.disabled) {
        btn.textContent = `Đăng nhập ${serviceDisplayName()}`;
        btn.onmouseenter = null;
        btn.onmouseleave = null;
      }
      
      const creditDisplay = document.getElementById("autohdr-credit-display");
      if (creditDisplay) creditDisplay.style.display = "none";
      if (service === "autohdr") currentLoggedUid = null;
      
      // Only log warning of expired session, do not automatically delete stored credentials
      if (statusAuthExpired(service, status) && statusHasSavedLogin(service, status)) {
        addLog(`⚠️ Phiên đăng nhập ${serviceDisplayName()} đã hết hạn. Vui lòng đăng nhập lại.`, "warn");
      } else if (status?.transient_error) {
        console.warn(status.msg || `Tạm thời chưa kiểm tra được ${serviceDisplayName()}.`);
      }
    }
  } catch (e) {
    console.error("Status check failed:", e);
  } finally {
    isConnectionStatusChecking = false;
  }
}

// ═══ TABS & UI NAVIGATION ═══

function switchTab(tab) {
  if (tab !== "upload" && tab !== "download") {
    tab = "upload";
  }
  currentTab = tab;

  // Update navbar active state
  document
    .querySelectorAll(".nav-item")
    .forEach((el) => el.classList.remove("active"));
  const tabEl = document.getElementById(`tab-${tab}`);
  if (tabEl) tabEl.classList.add("active");

  // Show active panel
  document
    .querySelectorAll(".content-tab-panel")
    .forEach((el) => el.classList.remove("active"));
  const secEl = document.getElementById(`sec-${tab}`);
  if (secEl) secEl.classList.add("active");

  if (tab === "download") {
    refreshListings();
  }

  if (tab === "upscale") {
    const seen = readSeenFeatureGuides();
    if (!seen[UPSCALE_MAIN_GUIDE_KEY]) {
      setTimeout(() => {
        if (currentTab === "upscale" && !readSeenFeatureGuides()[UPSCALE_MAIN_GUIDE_KEY]) {
          startUpscaleTour();
          markFeatureGuideSeen(UPSCALE_MAIN_GUIDE_KEY);
        }
      }, 500);
    }
  }

  if (authState) {
    applyAuthStatus(authState);
  }
}

function getSupportedUpscaleFiles(files) {
  const allowed = new Set(["jpg", "jpeg", "png", "webp"]);
  return (files || []).filter((file) => {
    if (isDropboxSpec(file)) return true;
    const ext = String(file || "").split(".").pop().toLowerCase();
    return allowed.has(ext);
  });
}

function updateUpscaleSummary() {
  const summary = document.getElementById("upscale-selection-summary");
  if (!summary) return;

  if (!selectedUpscaleFiles.length) {
    summary.textContent = "Chưa chọn ảnh.";
    return;
  }

  const first = selectedUpscaleFiles[0];
  if (isDropboxSpec(first) && Array.isArray(first.paths)) {
    const firstName = pathBasename(first.paths[0]);
    const more = first.paths.length > 1 ? ` (+${first.paths.length - 1})` : "";
    summary.textContent = `Dropbox: ${first.paths.length} ảnh đã chọn: ${firstName}${more}`;
    return;
  }

  const firstName = pathBasename(first);
  const more = selectedUpscaleFiles.length > 1 ? ` (+${selectedUpscaleFiles.length - 1})` : "";
  summary.textContent = `${selectedUpscaleFiles.length} ảnh đã chọn: ${firstName}${more}`;
}

function describeLocalGpu(status) {
  const gpu = status?.capability?.gpu || {};
  const platform = status?.capability?.platform || "unknown";
  const gpuName = gpu.name || (platform === "macos" ? "Apple GPU" : "GPU chưa xác định");
  const vram = gpu.vram_mb ? `, ${Math.round(gpu.vram_mb / 1024)}GB VRAM` : "";
  return `${platform.toUpperCase()} - ${gpuName}${vram}`;
}

function updateUpscaleProviderUI() {
  const provider = document.getElementById("upscale-provider")?.value || "runpod";
  const panel = document.getElementById("local-engine-panel");
  if (panel) {
    if (provider !== "local") {
      panel.classList.add("hidden");
    } else {
      const recommended = localUpscaleStatus?.recommended_engine;
      const installed = recommended && (localUpscaleStatus?.installed_engines || []).some((item) => item.id === recommended.id);
      if (installed) {
        panel.classList.add("hidden");
      } else {
        panel.classList.remove("hidden");
      }
    }
  }
  const scaleSelect = document.getElementById("wizard-upscale-scale");
  const item4x = document.getElementById("item-upscale-4x");
  if (scaleSelect) {
    if (provider === "runpod") {
      scaleSelect.value = "upscale_2x";
      if (typeof selectUpscaleScale === "function") {
        selectUpscaleScale("upscale_2x", "x2 (Gấp đôi)");
      }
      Array.from(scaleSelect.options).forEach(opt => {
        if (opt.value === "upscale_4x") opt.disabled = true;
      });
      if (item4x) {
        item4x.classList.add("disabled");
        item4x.style.opacity = "0.4";
        item4x.style.pointerEvents = "none";
      }
    } else {
      Array.from(scaleSelect.options).forEach(opt => opt.disabled = false);
      if (item4x) {
        item4x.classList.remove("disabled");
        item4x.style.opacity = "1";
        item4x.style.pointerEvents = "auto";
      }
    }
  }

  renderLocalUpscaleStatus();
  updateWizardModelLabel();
}

function renderLocalUpscaleStatus() {
  const statusEl = document.getElementById("local-engine-status");
  const installBtn = document.getElementById("btn-install-local-engine");
  if (!statusEl || !installBtn) return;

  if (!localUpscaleStatus) {
    statusEl.textContent = "Đang kiểm tra máy...";
    installBtn.disabled = true;
    return;
  }

  const recommended = localUpscaleStatus.recommended_engine;
  if (!localUpscaleStatus.capability?.supports_local) {
    statusEl.textContent = "Máy này chưa hỗ trợ Local Upscale. Vui lòng dùng Cloud.";
    installBtn.disabled = true;
    return;
  }
  if (!recommended) {
    statusEl.textContent = `${describeLocalGpu(localUpscaleStatus)}. Chưa có engine phù hợp trên server.`;
    installBtn.disabled = true;
    return;
  }

  const sizeMb = recommended.size_bytes ? Math.round(recommended.size_bytes / (1024 * 1024)) : null;
  const installed = (localUpscaleStatus.installed_engines || []).some((item) => item.id === recommended.id);
  statusEl.textContent = installed
    ? `${describeLocalGpu(localUpscaleStatus)}. Local sẵn sàng: ${recommended.name}.`
    : `${describeLocalGpu(localUpscaleStatus)}. Đề xuất: ${recommended.name}${sizeMb ? ` (~${sizeMb}MB)` : ""}.`;
  installBtn.disabled = installed;
  installBtn.textContent = installed ? "Engine local đã tải" : "Tải engine local";
}

async function refreshLocalUpscaleStatus() {
  if (!window.pywebview?.api?.upscale_local_status) return;
  try {
    const status = await window.pywebview.api.upscale_local_status();
    if (status?.ok !== false) {
      localUpscaleStatus = status;
    }
  } catch (e) {
    localUpscaleStatus = null;
  }
  renderLocalUpscaleStatus();
}

async function installLocalUpscaleEngine() {
  if (!window.pywebview?.api?.upscale_install_local_engine) return;
  const engineId = localUpscaleStatus?.recommended_engine?.id || null;
  if (!engineId) {
    addLog("⚠ Chưa có engine local phù hợp để tải.", "warn");
    return;
  }
  const btn = document.getElementById("btn-install-local-engine");
  if (btn) btn.disabled = true;
  try {
    addLog(`▶ Đang tải engine local: ${localUpscaleStatus.recommended_engine.name}...`, "info");
    
    if (btn) btn.style.display = "none";
    const progContainer = document.getElementById("local-engine-progress-container");
    if (progContainer) progContainer.classList.remove("hidden");
    
    const res = await window.pywebview.api.upscale_install_local_engine(engineId);
    if (!res?.ok) {
      addLog(`❌ Không thể tải engine local: ${res?.msg || "Lỗi không xác định"}`, "error");
      if (btn) btn.disabled = false;
    } else {
      let attempts = 0;
      const poll = setInterval(async () => {
        attempts += 1;
        await refreshLocalUpscaleStatus();
        const recommendedId = localUpscaleStatus?.recommended_engine?.id;
        const installed = (localUpscaleStatus?.installed_engines || []).some((item) => item.id === recommendedId);
        if (installed || attempts >= 360) {
          clearInterval(poll);
        }
      }, 5000);
    }
  } catch (e) {
    addLog(`❌ Lỗi tải engine local: ${e}`, "error");
    if (btn) btn.disabled = false;
  }
}

function upscaleDefaultProjectName(files) {
  const first = Array.isArray(files) && files.length ? files[0] : "";
  if (isDropboxSpec(first) && Array.isArray(first.paths) && first.paths.length > 0) {
    const baseName = pathWithoutExtension(first.paths[0]);
    if (baseName) return sanitizeFolderName(baseName);
  }
  const baseName = pathWithoutExtension(first);
  if (baseName) return sanitizeFolderName(baseName);
  return "Upscale_" + new Date().toISOString().slice(0, 10).replace(/-/g, "");
}

function openUpscaleWizard(files) {
  const supported = getSupportedUpscaleFiles(files);
  if (!supported.length) {
    if (files && files.length) {
      addLog("⚠ Upscale hiện chỉ nhận JPG, PNG hoặc WebP.", "warn");
    }
    return;
  }

  selectedUpscaleFiles = supported;
  if (supported.length !== files.length) {
    addLog("⚠ Một số file không phải JPG/PNG/WebP đã được bỏ qua.", "warn");
  }
  updateUpscaleSummary();
  const baseDir = pathDirname(supported[0]);
  openWizardModal(upscaleDefaultProjectName(supported), baseDir, "upscale");
}

async function browseUpscaleFiles() {
  if (!window.pywebview || !window.pywebview.api.browse_files) return;
  try {
    const files = await window.pywebview.api.browse_files("upscale");
    if (!files || !files.length) return;
    openUpscaleWizard(files);
  } catch (e) {
    addLog(`Lỗi chọn ảnh upscale: ${e}`, "error");
  }
}

async function startRunpodUpscale() {
  if (!window.pywebview || !window.pywebview.api.runpod_upscale) return;

  const files = getSupportedUpscaleFiles(selectedUpscaleFiles);

  if (!files.length) {
    addLog("⚠ Vui lòng chọn ít nhất một ảnh JPG, PNG hoặc WebP để upscale.", "warn");
    return;
  }
  openUpscaleWizard(files);
}

async function runUpscaleToProject(outputDir) {
  if (!window.pywebview || !window.pywebview.api.runpod_upscale) return;

  const files = getSupportedUpscaleFiles(selectedUpscaleFiles);
  const provider = document.getElementById("upscale-provider")?.value === "local" ? "local" : (document.getElementById("upscale-provider")?.value === "free" ? "free" : "runpod");
  
  const scaleSelect = document.getElementById("wizard-upscale-scale");
  const mode = scaleSelect ? scaleSelect.value : "upscale_2x";
  
  const strengthInput = document.getElementById("wizard-upscale-strength");
  const strength = strengthInput ? parseFloat(strengthInput.value) : 0.75;
  const keepSizeCheck = document.getElementById("wizard-upscale-keep-size");
  const keepOriginalSize = keepSizeCheck ? keepSizeCheck.checked : true;
  
  const blendOriginalCheck = document.getElementById("wizard-upscale-blend-original");
  const blendOriginal = blendOriginalCheck ? blendOriginalCheck.checked : true;
  
  let modeLabel = "Làm nét giữ size";
  if (!keepOriginalSize) {
    modeLabel = mode === "upscale_4x" ? "Upscale x4" : "Upscale x2";
  }

  if (!files.length || !outputDir) return;
  if (provider === "local") {
    await refreshLocalUpscaleStatus();
    const recommendedId = localUpscaleStatus?.recommended_engine?.id;
    const installed = (localUpscaleStatus?.installed_engines || []).some((item) => item.id === recommendedId);
    if (!installed) {
      addLog("⚠ Vui lòng tải engine local trước khi chạy Local Upscale.", "warn");
      updateUpscaleProviderUI();
      return;
    }
  }
  const btn = document.getElementById("btn-start-upscale");
  if (btn) btn.disabled = true;
  try {
    addLog(`▶ Đang thêm ${files.length} ảnh vào hàng đợi ${modeLabel} (${provider === "local" ? "Local" : (provider === "free" ? "Miễn phí" : "RunPod")})...`, "info");
    
    let res;
    if (provider === "free") {
        res = await window.pywebview.api.start_free_upscale(files, outputDir);
    } else {
        res = await window.pywebview.api.runpod_upscale(files, outputDir, mode, strength, provider, keepOriginalSize, blendOriginal, null);
    }
    
    if (!res || !res.ok) {
      addLog(`❌ Không thể bắt đầu upscale: ${res?.msg || "Lỗi không xác định"}`, "error");
    }
  } catch (e) {
    addLog(`❌ Lỗi khởi chạy upscale: ${e}`, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

function updateMergeFolderUI() {
  const original = document.getElementById("merge-original-path");
  const ai = document.getElementById("merge-ai-path");
  const output = document.getElementById("merge-output-path");
  if (original) original.textContent = formatMergeInputLabel(detailMergeFolders.original);
  if (ai) ai.textContent = formatMergeInputLabel(detailMergeFolders.ai);
  if (output) output.textContent = formatStorageTarget(detailMergeFolders.output);
}

function formatMergeInputLabel(value) {
  if (isDropboxSpec(value)) return formatStorageTarget(value);
  if (value && typeof value === "object" && value.provider === "dropbox_link") {
    return formatStorageTarget(value);
  }
  if (Array.isArray(value)) {
    if (!value.length) return "Chưa chọn thư mục";
    if (Array.isArray(value[0])) {
      return `Đã chọn ${value.length} nhóm bracket (Đã kiểm duyệt)`;
    }
    const firstName = pathBasename(value[0]);
    const more = value.length > 1 ? ` (+${value.length - 1})` : "";
    return `${value.length} ảnh đã chọn: ${firstName}${more}`;
  }
  return value || "Chưa chọn thư mục";
}

function getSupportedMergeFiles(paths) {
  const supported = [
    ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif",
    ".3fr", ".ari", ".arw", ".bay", ".cr2", ".cr3", ".crw", ".dcr", ".dng",
    ".erf", ".fff", ".gpr", ".iiq", ".k25", ".kdc", ".mdc", ".mef", ".mos",
    ".mrw", ".nef", ".nrw", ".orf", ".pef", ".raf", ".raw", ".rw2", ".rwl",
    ".sr2", ".srf", ".srw", ".x3f",
  ];
  return (Array.isArray(paths) ? paths : [])
    .filter((path) => isDropboxSpec(path) || supported.some((ext) => String(path || "").toLowerCase().endsWith(ext)));
}

async function handleMergeDroppedPaths(kind, paths) {
  if (!["original", "ai"].includes(kind)) return;
  const files = getSupportedMergeFiles(paths);
  if (!files.length) {
    addLog("⚠ Merge chỉ nhận ảnh JPG, PNG, WebP, HEIC hoặc RAW hỗ trợ khi kéo thả.", "warn");
    return;
  }

  const signature = `merge:${kind}\n${files.join('\n')}`;
  const now = Date.now();
  if (signature === lastDropSignature && now - lastDropHandledAt < 1000) return;
  lastDropSignature = signature;
  lastDropHandledAt = now;

  detailMergeFolders[kind] = files;
  updateMergeFolderUI();
  addLog(`Đã nhận ${files.length} ảnh cho ${kind === "original" ? "ảnh gốc" : "ảnh AI"}.`, "success");
  if (kind === "original") {
    openMergeBracketReview();
  }
}

function updateMergeStrengthLabel() {
  const input = document.getElementById("merge-strength");
  const label = document.getElementById("merge-strength-label");
  const value = input ? Number(input.value || 0.38) : 0.38;
  let text = "Tự nhiên";
  if (value <= 0.28) text = "Mềm";
  if (value >= 0.43) text = "Rõ nét";
  if (label) label.textContent = text;
}

function setMergePreset(preset) {
  const presets = {
    soft: { value: 0.28, label: "Mềm" },
    natural: { value: 0.40, label: "Tự nhiên" },
    crisp: { value: 0.58, label: "Rõ nét" },
  };
  const selected = presets[preset] || presets.natural;
  const input = document.getElementById("merge-strength");
  const label = document.getElementById("merge-strength-label");
  if (input) input.value = String(selected.value);
  if (label) label.textContent = selected.label;
  document.querySelectorAll("[data-merge-preset]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.mergePreset === preset);
  });
}

async function browseMergeFolder(kind) {
  if (!window.pywebview?.api?.browse_folder) return;
  try {
    const folder = await window.pywebview.api.browse_folder();
    if (!folder || !["original", "ai", "output"].includes(kind)) return;
    detailMergeFolders[kind] = folder;
    updateMergeFolderUI();
    if (kind === "original") {
      openMergeBracketReview();
    }
  } catch (e) {
    addLog(`Lỗi chọn thư mục merge: ${e}`, "error");
  }
}

async function browseDropboxInput(kind) {
  const labels = {
    upload: "Chọn file hoặc thư mục ảnh trên Dropbox",
    upscale: "Chọn file hoặc thư mục ảnh upscale trên Dropbox",
    "merge-original": "Chọn thư mục ảnh gốc trên Dropbox",
    "merge-ai": "Chọn thư mục ảnh AI trên Dropbox",
    "merge-output": "Chọn thư mục Dropbox để lưu kết quả merge",
  };
  const spec = await requestDropboxPath(labels[kind] || "Chọn Dropbox", "/");
  if (!spec) return;

  if (kind === "upload") {
    const service = currentService;
    if (!serviceLoginState[service]) {
      addLog(`⚠️ Bạn chưa đăng nhập ${serviceDisplayName(service)}. Đang tự động mở trình duyệt đăng nhập...`, "warn");
      toggleServiceLogin();
      return;
    }
    selectedInputFiles = spec;
    selectedInputBaseDir = pathDirname(spec);
    openWizardModal(dropboxDefaultProjectName(spec, "DuAn_Dropbox"), selectedInputBaseDir);
    return;
  }

  if (kind === "upscale") {
    selectedUpscaleFiles = [spec];
    openUpscaleWizard(selectedUpscaleFiles);
    return;
  }

  if (kind === "merge-original") {
    detailMergeFolders.original = spec;
    updateMergeFolderUI();
    addLog("Đã chọn ảnh gốc từ Dropbox.", "success");
    openMergeBracketReview();
    return;
  }

  if (kind === "merge-ai") {
    detailMergeFolders.ai = spec;
    updateMergeFolderUI();
    addLog("Đã chọn ảnh AI từ Dropbox.", "success");
    return;
  }

  if (kind === "merge-output") {
    detailMergeFolders.output = spec;
    updateMergeFolderUI();
    addLog("Đã chọn thư mục lưu Dropbox.", "success");
  }
}


function getDetailMergeConfig() {
  const strengthInput = document.getElementById("merge-strength");
  return {
    strength: strengthInput ? Number(strengthInput.value || 0.38) : 0.38,
    denoise: document.getElementById("merge-denoise")?.checked !== false,
    align: document.getElementById("merge-align")?.checked !== false,
    restore: document.getElementById("merge-restore")?.checked !== false,
    whiteBalance: document.getElementById("merge-white-balance")?.checked !== false,
    detailMode: document.getElementById("merge-detail-mode")?.value || "normal",
    presetLabel: document.getElementById("merge-strength-label")?.textContent || "",
    highlight: Number(document.getElementById("merge-highlight")?.value || 0),
    shadow: Number(document.getElementById("merge-shadow")?.value || 0),
  };
}

async function runDetailMergeWithOriginal(originalSource) {
  if (!window.pywebview?.api?.detail_merge) return;
  if (originalSource) {
    detailMergeFolders.original = originalSource;
  }
  const { ai, output } = detailMergeFolders;
  const original = detailMergeFolders.original;
  
  // Always update UI so the user sees "Đã chọn X nhóm bracket" after they close the modal.
  updateMergeFolderUI();

  if (!original || !ai || !output) {
    addLog("⚠ Vui lòng chọn đủ thư mục ảnh gốc, ảnh AI và thư mục lưu để bắt đầu gộp.", "warn");
    return;
  }
  const { strength, denoise, align, restore, detailMode, whiteBalance, presetLabel, highlight, shadow } = getDetailMergeConfig();
  const btn = document.getElementById("btn-start-merge");
  if (btn) btn.disabled = true;
  let started = false;
  try {
    const label = presetLabel || `${Math.round(strength * 100)}%`;
    const sourceLabel = Array.isArray(original) && Array.isArray(original[0]) ? ` với ${original.length} nhóm bracket` : "";
    addLog(`▶ Đang merge chi tiết${sourceLabel} ở mức ${label.toLowerCase()} (Highlight: ${highlight > 0 ? '+' : ''}${highlight}, Shadow: ${shadow > 0 ? '+' : ''}${shadow})...`, "info");
    const res = await window.pywebview.api.detail_merge(original, ai, output, strength, denoise, align, restore, detailMode, whiteBalance, highlight, shadow);
    if (!res?.ok) {
      addLog(`❌ Không thể bắt đầu merge: ${res?.msg || "Lỗi không xác định"}`, "error");
      if (btn) btn.disabled = false;
    } else {
      started = true;
      if (btn) btn.disabled = false;
    }
  } catch (e) {
    addLog(`❌ Lỗi khởi chạy merge: ${e}`, "error");
    if (btn) btn.disabled = false;
  } finally {
    if (btn && !started && !isMergeRunning) btn.disabled = false;
  }
}

async function startDetailMerge() {
  return runDetailMergeWithOriginal();
}

async function openMergeBracketReview() {
  if (!window.pywebview?.api?.detail_merge_analyze_brackets) return;
  const { original, ai, output } = detailMergeFolders;
  if (!original) {
    addLog("⚠ Vui lòng chọn thư mục hoặc kéo thả ảnh gốc trước.", "warn");
    return;
  }

  const sizeSelect = document.getElementById("bracket-group-size");
  const modeSelect = document.getElementById("bracket-modal-mode");
  const bracketSize = sizeSelect ? parseInt(sizeSelect.value || "3", 10) : 3;
  const mode = syncFotelloBracketMode(modeSelect?.value || "order_fixed");
  try {
    addLog("🔎 Đang phân tích bracket ảnh gốc cho Merge...", "info");
    const res = await window.pywebview.api.detail_merge_analyze_brackets(original, bracketSize, mode);
    if (!res?.ok) {
      addLog(`❌ Không thể phân tích bracket merge: ${res?.msg || "Lỗi không xác định"}`, "error");
      return;
    }
    openBracketModal(
      res.brackets,
      original,
      output,
      { mergeAi: ai },
      mode,
      bracketViewMode,
      bracketSize,
      "merge",
    );
  } catch (e) {
    addLog(`❌ Lỗi phân tích bracket merge: ${e}`, "error");
  }
}

async function browseFolder(inputId) {
  if (!window.pywebview) return;
  try {
    const path = await window.pywebview.api.browse_folder();
    if (path) {
      if (inputId === "inp-inputdir") {
        selectedInputFiles = [];
        selectedInputBaseDir = path;
      }
      document.getElementById(inputId).value = path;
    }
  } catch (e) {
    addLog(`Lỗi chọn thư mục: ${e}`, "error");
  }
}

function isDropboxLinkSpec(value) {
  return !!value && typeof value === "object" && value.provider === "dropbox_link";
}

function pathBasename(path) {
  if (isDropboxSpec(path)) {
    return pathBasename(path.path || "");
  }
  if (isDropboxLinkSpec(path)) {
    const url = path.url || "";
    try {
      const urlObj = new URL(url);
      const pathname = urlObj.pathname;
      const parts = pathname.split("/").filter(Boolean);
      return parts.pop() || "DropboxLink";
    } catch(e) {
      return "DropboxLink";
    }
  }
  return (
    String(path || "")
      .split(/[\\/]/)
      .filter(Boolean)
      .pop() || ""
  );
}

function pathDirname(path) {
  if (isDropboxSpec(path)) {
    if (Array.isArray(path.paths) && path.paths.length > 0) {
      const dirname = pathDirname(path.paths[0]);
      return makeDropboxSpec(dirname || "/");
    }
    const dirname = pathDirname(path.path || "");
    return makeDropboxSpec(dirname || "/");
  }
  if (isDropboxLinkSpec(path)) {
    return path;
  }
  const normalized = String(path || "").replace(/[\\/]+$/, "");
  const idx = Math.max(
    normalized.lastIndexOf("/"),
    normalized.lastIndexOf("\\"),
  );
  return idx > 0 ? normalized.slice(0, idx) : normalized;
}

function pathWithoutExtension(name) {
  const base = pathBasename(name);
  const idx = base.lastIndexOf(".");
  return idx > 0 ? base.slice(0, idx) : base;
}

function isDropboxSpec(value) {
  return !!value && typeof value === "object" && value.provider === "dropbox";
}

function makeDropboxSpec(path, extra = {}) {
  let normalized = String(path || "").trim();
  if (!normalized) normalized = "/";
  if (!normalized.startsWith("/")) normalized = "/" + normalized;
  return { provider: "dropbox", path: normalized, ...extra };
}

function formatStorageTarget(value, fallback = "Chưa chọn thư mục") {
  if (isDropboxSpec(value)) {
    if (Array.isArray(value.paths)) {
      return `Dropbox: Chọn ${value.paths.length} ảnh`;
    }
    return `Dropbox: ${value.path || "/"}`;
  }
  if (value && typeof value === "object" && value.provider === "dropbox_link") {
    const url = value.url || "";
    const shortUrl = url.length > 40 ? url.slice(0, 37) + "..." : url;
    return `Link Dropbox: ${shortUrl}`;
  }
  if (Array.isArray(value)) {
    if (!value.length) return fallback;
    const firstName = pathBasename(value[0]);
    const more = value.length > 1 ? ` (+${value.length - 1})` : "";
    return `${value.length} ảnh đã chọn: ${firstName}${more}`;
  }
  return value || fallback;
}

async function ensureDropboxConfigured() {
  if (!window.pywebview?.api?.storage_status) return false;
  try {
    const status = await window.pywebview.api.storage_status();
    if (status?.dropbox?.configured) return true;
    return startDropboxOAuth(status?.dropbox || {});
  } catch (e) {}

  return startDropboxOAuth({});
}

async function startDropboxOAuth(dropboxStatus = {}) {
  if (!window.pywebview?.api?.storage_start_dropbox_oauth) return false;

  return new Promise(async (resolve) => {
    let settled = false;
    const finish = (ok, msg = "") => {
      if (settled) return;
      settled = true;
      window.onDropboxOAuthComplete = null;
      if (ok) addLog("Dropbox đã kết nối.", "success");
      if (!ok && msg) addLog(`❌ Dropbox OAuth lỗi: ${msg}`, "error");
      resolve(!!ok);
    };

    window.onDropboxOAuthComplete = finish;

    try {
      addLog("Đang mở trình duyệt đăng nhập Dropbox qua Supabase...", "info");
      const res = await window.pywebview.api.storage_start_dropbox_oauth(null, null);
      if (!res?.ok) {
        finish(false, res?.msg || "Không khởi chạy được OAuth.");
        return;
      }
      if (res.redirect_uri) {
        addLog(`Nếu Dropbox báo redirect URI chưa hợp lệ, hãy thêm URI này trong Dropbox App Console: ${res.redirect_uri}`, "info");
      }
      setTimeout(() => finish(false, "Hết thời gian chờ đăng nhập Dropbox."), 180000);
    } catch (e) {
      finish(false, String(e));
    }
  });
}

async function requestDropboxPath(label, defaultPath = "/") {
  const ready = await ensureDropboxConfigured();
  if (!ready) return null;
  const mode = /lưu|save|kết quả|cha/i.test(label || "") ? "folder" : "any";
  return openDropboxPicker(label, defaultPath || "/", mode);
}

function normalizeDropboxPath(path) {
  let value = String(path || "/").trim();
  if (!value) value = "/";
  if (!value.startsWith("/")) value = "/" + value;
  return value.replace(/\/+/g, "/");
}

function dropboxParentPath(path) {
  const normalized = normalizeDropboxPath(path).replace(/\/+$/, "");
  if (!normalized || normalized === "/") return "/";
  const idx = normalized.lastIndexOf("/");
  return idx <= 0 ? "/" : normalized.slice(0, idx);
}

function formatDropboxSize(size) {
  const n = Number(size || 0);
  if (!Number.isFinite(n) || n <= 0) return "";
  if (n >= 1024 * 1024 * 1024) return `${(n / (1024 * 1024 * 1024)).toFixed(1)} GB`;
  if (n >= 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  if (n >= 1024) return `${Math.round(n / 1024)} KB`;
  return `${n} B`;
}

async function openDropboxPicker(title, startPath = "/", mode = "any") {
  const modal = document.getElementById("dropbox-picker-modal");
  const titleEl = document.getElementById("dropbox-picker-title");
  if (!modal || !window.pywebview?.api?.storage_list_dropbox_folder) return null;

  const linkField = document.getElementById("dropbox-picker-link-field");
  if (linkField) linkField.value = "";

  return new Promise(async (resolve) => {
    dropboxPickerState = {
      mode,
      currentPath: normalizeDropboxPath(startPath),
      resolve,
      selectedPaths: new Set(),
    };
    if (titleEl) titleEl.textContent = title || "Chọn Dropbox";
    modal.classList.remove("hidden");
    await loadDropboxPickerPath(dropboxPickerState.currentPath, true);
  });
}

function closeDropboxPicker(value = null) {
  const modal = document.getElementById("dropbox-picker-modal");
  if (modal) modal.classList.add("hidden");
  if (dropboxPickerState?.resolve) {
    dropboxPickerState.resolve(value);
  }
  dropboxPickerState = null;
}

window.submitDropboxLinkFromPicker = function () {
  const field = document.getElementById("dropbox-picker-link-field");
  if (!field) return;
  const url = field.value.trim();
  if (!url) return;
  if (!url.toLowerCase().includes("dropbox.com")) {
    alert("Đường dẫn không hợp lệ. Vui lòng nhập link chia sẻ từ Dropbox.");
    return;
  }
  
  field.value = "";
  const spec = { provider: "dropbox_link", url: url };
  closeDropboxPicker(spec);
};

async function logoutDropboxFromPicker() {
  if (!window.pywebview?.api?.storage_clear_dropbox_token) return;
  const ok = await window.confirm("Đăng xuất Dropbox khỏi app này?");
  if (!ok) return;

  try {
    const res = await window.pywebview.api.storage_clear_dropbox_token();
    if (!res?.ok) {
      window.alert(res?.msg || "Không đăng xuất được Dropbox.");
      return;
    }
    addLog("Đã đăng xuất Dropbox.", "success");
    closeDropboxPicker();
  } catch (e) {
    window.alert(`Không đăng xuất được Dropbox: ${String(e)}`);
  }
}

async function loadDropboxPickerPath(path, allowRootFallback = false) {
  if (!dropboxPickerState) return;
  const normalized = normalizeDropboxPath(path);
  const pathEl = document.getElementById("dropbox-picker-path");
  const listEl = document.getElementById("dropbox-picker-list");
  const loadingEl = document.getElementById("dropbox-picker-loading");
  if (pathEl) pathEl.textContent = normalized;
  if (loadingEl) loadingEl.classList.remove("hidden");
  if (listEl) listEl.innerHTML = "";

  try {
    const apiPath = normalized === "/" ? "" : normalized;
    const res = await window.pywebview.api.storage_list_dropbox_folder(apiPath, false);
    if (!res?.ok) {
      throw new Error(res?.msg || "Không tải được Dropbox.");
    }
    dropboxPickerState.currentPath = normalized;
    if (dropboxPickerState.selectedPaths) dropboxPickerState.selectedPaths.clear();
    renderDropboxEntries(res.entries || []);
  } catch (e) {
    if (allowRootFallback && normalized !== "/") {
      await loadDropboxPickerPath("/", false);
      return;
    }
    if (listEl) {
      listEl.innerHTML = `<div class="empty-state error">Không mở được Dropbox: ${String(e.message || e)}</div>`;
    }
  } finally {
    if (loadingEl) loadingEl.classList.add("hidden");
  }
}

function updateDropboxSelectButton() {
  const selectBtn = document.getElementById("btn-dropbox-select-current");
  if (!selectBtn || !dropboxPickerState) return;
  
  if (dropboxPickerState.mode === "folder") {
    selectBtn.textContent = "Chọn thư mục này";
    return;
  }
  
  const count = dropboxPickerState.selectedPaths ? dropboxPickerState.selectedPaths.size : 0;
  if (count > 0) {
    selectBtn.textContent = `Chọn ${count} ảnh`;
  } else {
    selectBtn.textContent = "Chọn thư mục này";
  }
}

function renderDropboxEntries(entries) {
  const listEl = document.getElementById("dropbox-picker-list");
  if (!listEl || !dropboxPickerState) return;
  const sorted = [...entries].sort((a, b) => {
    if (a.type !== b.type) return a.type === "folder" ? -1 : 1;
    return String(a.name || "").localeCompare(String(b.name || ""));
  });
  if (!sorted.length) {
    listEl.innerHTML = '<div class="empty-state">Thư mục Dropbox này đang trống.</div>';
    updateDropboxSelectButton();
    return;
  }
  listEl.innerHTML = "";
  sorted.forEach((entry) => {
    const row = document.createElement("button");
    row.type = "button";
    
    const isFolder = entry.type === "folder";
    const isSelected = !isFolder && dropboxPickerState.selectedPaths && dropboxPickerState.selectedPaths.has(entry.path);
    row.className = "dropbox-picker-row" + (isSelected ? " selected" : "");
    
    const size = isFolder ? "" : formatDropboxSize(entry.size);
    row.innerHTML = `
      <span>${isFolder ? "📁" : (isSelected ? "☑️" : "🖼")}</span>
      <span class="dropbox-picker-name"></span>
      <span class="dropbox-picker-meta">${isFolder ? "Mở" : size}</span>
    `;
    row.querySelector(".dropbox-picker-name").textContent = entry.name || entry.path || "";
    row.onclick = async () => {
      if (isFolder) {
        if (dropboxPickerState.selectedPaths) dropboxPickerState.selectedPaths.clear();
        updateDropboxSelectButton();
        await loadDropboxPickerPath(entry.path || entry.name || "/");
        return;
      }
      if (dropboxPickerState.mode === "folder") return;
      
      if (!dropboxPickerState.selectedPaths) dropboxPickerState.selectedPaths = new Set();
      if (dropboxPickerState.selectedPaths.has(entry.path)) {
        dropboxPickerState.selectedPaths.delete(entry.path);
      } else {
        dropboxPickerState.selectedPaths.add(entry.path);
      }
      
      renderDropboxEntries(entries);
    };
    listEl.appendChild(row);
  });
  updateDropboxSelectButton();
}

function resolveDropboxPicker(path) {
  let spec;
  if (dropboxPickerState && dropboxPickerState.selectedPaths && dropboxPickerState.selectedPaths.size > 0) {
    const filePaths = Array.from(dropboxPickerState.selectedPaths);
    spec = {
      provider: "dropbox",
      paths: filePaths
    };
  } else {
    spec = makeDropboxSpec(path || dropboxPickerState?.currentPath || "/");
  }
  
  const modal = document.getElementById("dropbox-picker-modal");
  if (modal) modal.classList.add("hidden");
  const resolve = dropboxPickerState?.resolve;
  dropboxPickerState = null;
  if (resolve) resolve(spec);
}

function navigateDropboxParent() {
  if (!dropboxPickerState) return;
  loadDropboxPickerPath(dropboxParentPath(dropboxPickerState.currentPath));
}

function selectCurrentDropboxPath() {
  if (!dropboxPickerState) return;
  resolveDropboxPicker(dropboxPickerState.currentPath || "/");
}

window.closeDropboxPicker = closeDropboxPicker;
window.logoutDropboxFromPicker = logoutDropboxFromPicker;
window.navigateDropboxParent = navigateDropboxParent;
window.selectCurrentDropboxPath = selectCurrentDropboxPath;

function dropboxDefaultProjectName(spec, fallback) {
  if (isDropboxSpec(spec) && Array.isArray(spec.paths) && spec.paths.length > 0) {
    const base = pathWithoutExtension(spec.paths[0]);
    return sanitizeFolderName(base || fallback || `Dropbox_${new Date().toISOString().slice(0, 10).replace(/-/g, "")}`);
  }
  const base = pathWithoutExtension(spec);
  return sanitizeFolderName(base || fallback || `Dropbox_${new Date().toISOString().slice(0, 10).replace(/-/g, "")}`);
}

function sanitizeFolderName(name) {
  return String(name || "")
    .replace(/[<>:"/\\|?*\x00-\x1F]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 80);
}

function joinPath(parent, child) {
  if (isDropboxSpec(parent)) {
    const root = String(parent.path || "/").replace(/\/+$/, "");
    return makeDropboxSpec((root || "") + "/" + child);
  }
  if (isDropboxLinkSpec(parent)) {
    return makeDropboxSpec("/AutoFotello/" + child);
  }
  const sep = String(parent || "").includes("\\") ? "\\" : "/";
  return String(parent || "").replace(/[\\/]+$/, "") + sep + child;
}

async function requestUploadOutputDir(defaultName) {
  let folderName = "";
  while (!folderName) {
    const rawName = window.prompt("Nhập tên thư mục lưu kết quả:", "");
    if (rawName === null) {
      addLog("⚠ Đã hủy vì chưa nhập tên thư mục lưu.", "warn");
      return null;
    }
    folderName = sanitizeFolderName(rawName);
    if (!folderName) {
      window.alert("Vui lòng nhập tên thư mục lưu kết quả.");
    }
  }

  return requestOutputDirForFolder(folderName);
}

async function requestOutputDirForFolder(folderName) {
  const safeFolderName = sanitizeFolderName(folderName);
  if (!safeFolderName) {
    addLog("⚠ Tên thư mục lưu không hợp lệ.", "warn");
    return null;
  }

  if (await window.confirm(`Bạn muốn lưu thư mục "${safeFolderName}" ở đâu?`, 'Dropbox', 'Lưu trên máy')) {
    const parentSpec = await requestDropboxPath("Chọn thư mục Dropbox cha để lưu kết quả", "/AutoFotello");
    if (!parentSpec) return null;
    return {
      folderName: safeFolderName,
      outputDir: joinPath(parentSpec, safeFolderName),
    };
  }

  window.alert(`Bước tiếp theo: chọn nơi lưu thư mục "${safeFolderName}".`);
  addLog("📁 Chọn nơi lưu thư mục kết quả...", "info");
  const parentDir = await window.pywebview.api.browse_folder();
  if (!parentDir) {
    addLog("⚠ Đã hủy vì chưa chọn nơi lưu.", "warn");
    return null;
  }

  return {
    folderName: safeFolderName,
    outputDir: joinPath(parentDir, safeFolderName),
  };
}

function requestProjectName(label, defaultName) {
  let name = "";
  while (!name) {
    const value = window.prompt(label, "");
    if (value === null) {
      addLog("⚠ Đã hủy vì chưa nhập tên dự án.", "warn");
      return null;
    }
    name = sanitizeFolderName(value);
    if (!name) {
      window.alert("Vui lòng nhập tên dự án.");
    }
  }
  return name;
}

async function browseImageFiles() {
  const service = currentService;
  if (!serviceLoginState[service]) {
    addLog(`⚠️ Bạn chưa đăng nhập ${serviceDisplayName(service)}. Đang tự động mở trình duyệt đăng nhập...`, "warn");
    toggleServiceLogin();
    return;
  }

  if (!window.pywebview || !window.pywebview.api.browse_files) return;
  try {
    const files = await window.pywebview.api.browse_files("upload");
    if (!files || files.length === 0) return;

    selectedInputFiles = files;
    selectedInputBaseDir = pathDirname(files[0]);

    // Extract default project name from first file or default timestamp
    const baseName = pathWithoutExtension(files[0]);
    const defaultProjName = baseName ? sanitizeFolderName(baseName) : "DuAn_" + new Date().toISOString().slice(0, 10).replace(/-/g, "");

    openWizardModal(defaultProjName, selectedInputBaseDir);
  } catch (e) {
    addLog(`Lỗi chọn ảnh: ${e}`, "error");
  }
}

function openWizardModal(defaultName, baseDir, context = "upload") {
  const modal = document.getElementById("project-wizard-modal");
  if (!modal) return;

  wizardContext = context === "upscale" ? "upscale" : "upload";
  wizardParentDir = baseDir || "";
  wizardOutputTarget = null;

  // Make project name empty by default for user to input
  document.getElementById("wizard-project-name").value = "";

  // The save path will show `<Chưa nhập tên dự án>` when empty
  const initialPath = joinPath(wizardParentDir, "<Chưa nhập tên dự án>");
  const destPathEl = document.getElementById("wizard-dest-path");
  if (destPathEl) {
    const label = formatStorageTarget(initialPath);
    destPathEl.textContent = label;
    destPathEl.title = label;
    destPathEl.classList.add("placeholder-path");
  }

  const footerLeft = document.querySelector(".wizard-footer-left-wrapper");
  const gearButton = document.querySelector(".btn-icon-gear");
  const modelArrow = document.querySelector(".wizard-footer-left .arrow-indicator");

  // Set visible settings panel based on task context/service
  // Reset Autoenhance submenu slide position only (keep user's selected preset/version/options across jobs)
  const aePrefsContainer = document.getElementById("wizard-autoenhance-prefs");
  if (aePrefsContainer) {
    aePrefsContainer.style.height = "240px";
  }
  const aeMainView = document.getElementById("wizard-autoenhance-prefs-main");
  if (aeMainView) {
    aeMainView.style.transform = "translateX(0)";
  }
  document.querySelectorAll(".wizard-autoenhance-sub").forEach((sub) => {
    sub.style.transform = "translateX(100%)";
  });

  // Set visible settings panel based on task context/service
  if (wizardContext === "upscale") {
    document.getElementById("wizard-fotello-prefs").classList.add("hidden");
    document.getElementById("wizard-autohdr-prefs").classList.add("hidden");
    document.getElementById("wizard-autoenhance-prefs")?.classList.add("hidden");
    document.getElementById("wizard-upscale-prefs")?.classList.remove("hidden");
    document.getElementById("wizard-upscale-settings")?.classList.remove("hidden");
    document.getElementById("wizard-version-wrapper")?.classList.add("hidden");
    if (footerLeft) footerLeft.style.display = "flex";
    if (gearButton) gearButton.style.display = "";
    if (modelArrow) modelArrow.style.display = "";

    // Reset upscale sub-menu positions
    const upscalePrefs = document.getElementById("wizard-upscale-prefs");
    if (upscalePrefs) {
      upscalePrefs.style.height = "340px";
    }
    const upscaleMainView = document.getElementById("wizard-upscale-prefs-main");
    if (upscaleMainView) {
      upscaleMainView.style.transform = "translateX(0)";
    }
    const upscaleScaleView = document.getElementById("wizard-upscale-prefs-scale");
    if (upscaleScaleView) {
      upscaleScaleView.style.transform = "translateX(100%)";
    }

    updateWizardModelLabel();
    updateUpscaleProviderUI();
  } else if (currentService === "fotello") {
    document.getElementById("wizard-upscale-settings")?.classList.add("hidden");
    document.getElementById("wizard-upscale-prefs")?.classList.add("hidden");
    document.getElementById("wizard-version-wrapper")?.classList.add("hidden");
    if (footerLeft) footerLeft.style.display = "flex";
    if (gearButton) gearButton.style.display = "";
    if (modelArrow) modelArrow.style.display = "";
    const fotelloPrefs = document.getElementById("wizard-fotello-prefs");
    if (fotelloPrefs) {
      fotelloPrefs.style.height = "210px";
    }
    const mainView = document.getElementById("wizard-fotello-prefs-main");
    if (mainView) {
      mainView.style.transform = "translateX(0)";
    }
    const cloudView = document.getElementById("wizard-fotello-prefs-cloud");
    if (cloudView) {
      cloudView.style.transform = "translateX(100%)";
    }
    document.getElementById("wizard-fotello-prefs").classList.remove("hidden");
    document.getElementById("wizard-autohdr-prefs").classList.add("hidden");
    document.getElementById("wizard-autoenhance-prefs")?.classList.add("hidden");
    const topBtn = document.getElementById("btn-top-manage-profiles");
    if (topBtn) topBtn.style.display = "none";
  } else if (currentService === "autoenhance") {
    document.getElementById("wizard-upscale-settings")?.classList.add("hidden");
    document.getElementById("wizard-upscale-prefs")?.classList.add("hidden");
    document.getElementById("wizard-version-wrapper")?.classList.remove("hidden");
    if (footerLeft) footerLeft.style.display = "flex";
    if (gearButton) gearButton.style.display = "";
    if (modelArrow) modelArrow.style.display = "";
    document.getElementById("wizard-fotello-prefs").classList.add("hidden");
    document.getElementById("wizard-autohdr-prefs").classList.add("hidden");
    document.getElementById("wizard-autoenhance-prefs")?.classList.remove("hidden");
    const topBtn = document.getElementById("btn-top-manage-profiles");
    if (topBtn) topBtn.style.display = "none";
  } else {
    document.getElementById("wizard-upscale-settings")?.classList.add("hidden");
    document.getElementById("wizard-upscale-prefs")?.classList.add("hidden");
    document.getElementById("wizard-version-wrapper")?.classList.add("hidden");
    if (footerLeft) footerLeft.style.display = "flex";
    if (gearButton) gearButton.style.display = "";
    if (modelArrow) modelArrow.style.display = "";
    document.getElementById("wizard-fotello-prefs").classList.add("hidden");
    document.getElementById("wizard-autohdr-prefs").classList.remove("hidden");
    document.getElementById("wizard-autoenhance-prefs")?.classList.add("hidden");
    const topBtn = document.getElementById("btn-top-manage-profiles");
    if (topBtn) topBtn.style.display = "flex";
  }

  // Hide the advanced panel by default
  const advDropdown = document.getElementById("wizard-advanced-dropdown");
  if (advDropdown) advDropdown.classList.add("hidden");
  
  // Reset gear rotation
  const gear = document.querySelector(".btn-icon-gear");
  if (gear) gear.style.transform = "none";

  updateWizardModelLabel();

  modal.classList.remove("hidden");
  scheduleWizardContextTour();
}

function closeWizardModal() {
  const modal = document.getElementById("project-wizard-modal");
  if (modal) modal.classList.add("hidden");
}

function toggleWizardAdvanced(event) {
  if (event) event.stopPropagation();
  const panel = document.getElementById("wizard-advanced-dropdown");
  if (!panel) return;
  
  const isHidden = panel.classList.contains("hidden");
  const gear = document.querySelector(".btn-icon-gear");
  
  if (isHidden) {
    // Hide the model dropdown first to avoid overlapping menus
    const modelDropdown = document.getElementById("wizard-model-dropdown");
    if (modelDropdown) modelDropdown.classList.add("hidden");

    panel.classList.remove("hidden");
    if (gear) gear.style.transform = "rotate(45deg)";
  } else {
    panel.classList.add("hidden");
    if (gear) gear.style.transform = "none";
  }
}

function toggleWizardModelDropdown(event) {
  if (event) event.stopPropagation();
  const dropdown = document.getElementById("wizard-model-dropdown");
  if (!dropdown) return;
  
  const isHidden = dropdown.classList.contains("hidden");
  if (isHidden) {
    // Hide the advanced dropdown first to avoid overlapping menus
    const advDropdown = document.getElementById("wizard-advanced-dropdown");
    if (advDropdown) {
      advDropdown.classList.add("hidden");
      const gear = document.querySelector(".btn-icon-gear");
      if (gear) gear.style.transform = "none";
    }

    populateWizardModelDropdown();
    dropdown.classList.remove("hidden");
  } else {
    dropdown.classList.add("hidden");
  }
}

function populateWizardModelDropdown() {
  const dropdown = document.getElementById("wizard-model-dropdown");
  if (!dropdown) return;
  dropdown.innerHTML = "";

  if (wizardContext === "upscale") {
    const providerSelect = document.getElementById("upscale-provider");
    if (providerSelect) {
      Array.from(providerSelect.options).forEach(opt => {
        const item = document.createElement("div");
        item.className = "wizard-dropdown-item";
        if (providerSelect.value === opt.value) {
          item.classList.add("active");
        }
        item.textContent = opt.text;
        item.onclick = async (e) => {
          e.stopPropagation();
          dropdown.classList.add("hidden");
          await selectWizardUpscaleProvider(opt.value);
        };
        dropdown.appendChild(item);
      });
    }
    return;
  }

  if (currentService === "fotello") {
    const contrastSelect = document.getElementById("wizard-pref-contrast");
    if (contrastSelect) {
      Array.from(contrastSelect.options).forEach(opt => {
        const item = document.createElement("div");
        item.className = "wizard-dropdown-item";
        if (contrastSelect.value === opt.value) {
          item.classList.add("active");
        }
        item.textContent = opt.text;
        item.onclick = (e) => {
          e.stopPropagation();
          selectWizardModel(opt.value);
        };
        dropdown.appendChild(item);
      });
    }
  } else if (currentService === "autoenhance") {
    const modelSelect = document.getElementById("wizard-autoenhance-model");
    if (modelSelect) {
      Array.from(modelSelect.options).forEach(opt => {
        const item = document.createElement("div");
        item.className = "wizard-dropdown-item";
        if (modelSelect.value === opt.value) {
          item.classList.add("active");
        }
        item.textContent = opt.text;
        item.onclick = (e) => {
          e.stopPropagation();
          selectWizardModel(opt.value);
        };
        dropdown.appendChild(item);
      });
    }
  } else {
    const modelSelect = document.getElementById("wizard-autohdr-model");
    if (modelSelect) {
      Array.from(modelSelect.options).forEach(opt => {
        const item = document.createElement("div");
        item.className = "wizard-dropdown-item";
        if (modelSelect.value === opt.value) {
          item.classList.add("active");
        }
        const shortName = opt.text.split(" (")[0].split(" [")[0];
        item.textContent = shortName;
        item.onclick = (e) => {
          e.stopPropagation();
          selectWizardModel(opt.value);
        };
        dropdown.appendChild(item);
      });
    }
  }
}

async function selectWizardUpscaleProvider(value) {
  const providerSelect = document.getElementById("upscale-provider");
  if (providerSelect) {
    providerSelect.value = value;
    updateUpscaleProviderUI();
  }

  if (value === "local") {
    await refreshLocalUpscaleStatus();
    const recommendedId = localUpscaleStatus?.recommended_engine?.id;
    const installed = (localUpscaleStatus?.installed_engines || []).some((item) => item.id === recommendedId);
    if (!installed) {
      const wantDownload = await window.confirm("Engine local chưa được tải. Bạn có muốn tải về máy ngay bây giờ không?");
      if (wantDownload) {
        installLocalUpscaleEngine();
      }
    }
  }
}

function selectWizardModel(value) {
  if (currentService === "fotello") {
    const contrastSelect = document.getElementById("wizard-pref-contrast");
    if (contrastSelect) {
      contrastSelect.value = value;
    }
  } else if (currentService === "autoenhance") {
    const modelSelect = document.getElementById("wizard-autoenhance-model");
    if (modelSelect) {
      modelSelect.value = value;
    }
  } else {
    const modelSelect = document.getElementById("wizard-autohdr-model");
    if (modelSelect) {
      modelSelect.value = value;
    }
  }
  
  updateWizardModelLabel();

  const dropdown = document.getElementById("wizard-model-dropdown");
  if (dropdown) {
    dropdown.classList.add("hidden");
  }
}

// Add a global click listener to dismiss the model dropdown & advanced dropdown when clicking outside
document.addEventListener("click", (e) => {
  // Model dropdown dismiss
  const dropdown = document.getElementById("wizard-model-dropdown");
  if (dropdown && !dropdown.classList.contains("hidden")) {
    const wrapper = document.querySelector(".wizard-footer-left-wrapper");
    if (wrapper && !wrapper.contains(e.target)) {
      dropdown.classList.add("hidden");
    }
  }

  // Advanced settings dropdown dismiss
  const advDropdown = document.getElementById("wizard-advanced-dropdown");
  if (advDropdown && !advDropdown.classList.contains("hidden")) {
    const wrapper = document.querySelector(".wizard-footer-right");
    if (wrapper && !wrapper.contains(e.target)) {
      advDropdown.classList.add("hidden");
      const gear = document.querySelector(".btn-icon-gear");
      if (gear) gear.style.transform = "none";
    }
  }

  // Version dropdown dismiss
  const versionDropdown = document.getElementById("wizard-version-dropdown");
  if (versionDropdown && !versionDropdown.classList.contains("hidden")) {
    const wrapper = document.getElementById("wizard-version-wrapper");
    if (wrapper && !wrapper.contains(e.target)) {
      versionDropdown.classList.add("hidden");
    }
  }
});

function updateWizardDestFolder(val) {
  const name = sanitizeFolderName(val);
  const destPathEl = document.getElementById("wizard-dest-path");
  if (destPathEl && wizardParentDir) {
    if (name) {
      const fullPath = joinPath(wizardParentDir, name);
      wizardOutputTarget = fullPath;
      const label = formatStorageTarget(fullPath);
      destPathEl.textContent = label;
      destPathEl.title = label;
      destPathEl.classList.remove("placeholder-path");
    } else {
      const fullPath = joinPath(wizardParentDir, "<Chưa nhập tên dự án>");
      wizardOutputTarget = null;
      const label = formatStorageTarget(fullPath);
      destPathEl.textContent = label;
      destPathEl.title = label;
      destPathEl.classList.add("placeholder-path");
    }
  }
}

async function changeWizardDestFolder() {
  if (!window.pywebview || !window.pywebview.api.browse_folder) return;
  try {
    const parentDir = await window.pywebview.api.browse_folder();
    if (parentDir) {
      wizardParentDir = parentDir;
      const projName = sanitizeFolderName(document.getElementById("wizard-project-name").value);
      const destPathEl = document.getElementById("wizard-dest-path");
      if (destPathEl) {
        if (projName) {
          const fullPath = joinPath(parentDir, projName);
          wizardOutputTarget = fullPath;
          const label = formatStorageTarget(fullPath);
          destPathEl.textContent = label;
          destPathEl.title = label;
          destPathEl.classList.remove("placeholder-path");
        } else {
          const fullPath = joinPath(parentDir, "<Chưa nhập tên dự án>");
          wizardOutputTarget = null;
          const label = formatStorageTarget(fullPath);
          destPathEl.textContent = label;
          destPathEl.title = label;
          destPathEl.classList.add("placeholder-path");
        }
      }
    }
  } catch (e) {
    addLog(`Lỗi chọn nơi lưu: ${e}`, "error");
  }
}

async function changeWizardDropboxFolder() {
  const spec = await requestDropboxPath("Chọn thư mục Dropbox để lưu dự án", "/AutoFotello");
  if (!spec) return;
  wizardParentDir = spec;
  const projName = sanitizeFolderName(document.getElementById("wizard-project-name").value);
  const destPathEl = document.getElementById("wizard-dest-path");
  const fullPath = joinPath(spec, projName || "<Chưa nhập tên dự án>");
  if (projName) wizardOutputTarget = fullPath;
  if (destPathEl) {
    const label = formatStorageTarget(fullPath);
    destPathEl.textContent = label;
    destPathEl.title = label;
    destPathEl.classList.toggle("placeholder-path", !projName);
  }
}

function updateWizardModelLabel() {
  const label = document.getElementById("wizard-model-display-label");
  if (!label) return;

  if (wizardContext === "upscale") {
    const provider = document.getElementById("upscale-provider")?.value === "local" ? "Local" : "Cloud";
    const scaleSelect = document.getElementById("wizard-upscale-scale");
    const mode = scaleSelect ? scaleSelect.value : "upscale_2x";
    const keepSizeCheck = document.getElementById("wizard-upscale-keep-size");
    const keepOriginalSize = keepSizeCheck ? keepSizeCheck.checked : true;
    let modeText = "Làm nét giữ size";
    if (!keepOriginalSize) {
      modeText = mode === "upscale_4x" ? "Upscale x4" : "Upscale x2";
    }
    label.textContent = `${provider} ${modeText}`;
  } else if (currentService === "fotello") {
    const contrastSelect = document.getElementById("wizard-pref-contrast");
    const selectedText = contrastSelect.options[contrastSelect.selectedIndex].text;
    const shortName = selectedText.split(" (")[0].split(" [")[0];
    label.textContent = `Fotello ${shortName}`;
  } else if (currentService === "autoenhance") {
    const modelSelect = document.getElementById("wizard-autoenhance-model");
    const selectedText = modelSelect ? modelSelect.options[modelSelect.selectedIndex].text : "Autoenhance AI";
    const shortName = selectedText.split(" (")[0].split(" [")[0];
    label.textContent = `Autoenhance ${shortName}`;
  } else {
    const model = document.getElementById("wizard-autohdr-model");
    const modelText = model.options[model.selectedIndex].text;
    const shortName = modelText.split(" (")[0].split(" [")[0];
    label.textContent = `AutoHDR ${shortName}`;
  }
}

function clearLogs() {
  const output = document.getElementById("console-output");
  if (output) output.innerHTML = "";
}

// ═══ UPLOAD & ENHANCE ACTION ═══

function syncFotelloBracketMode(mode) {
  let normalizedMode = "order_fixed";
  if (mode === "time") normalizedMode = "time";

  const modalMode = document.getElementById("bracket-modal-mode");
  if (modalMode) modalMode.value = normalizedMode;

  const sizeContainer = document.getElementById("group-size-container");
  if (sizeContainer) {
    if (normalizedMode === "order_fixed") {
      sizeContainer.style.display = "block";
    } else {
      sizeContainer.style.display = "none";
    }
  }
  return normalizedMode;
}

async function submitWizard() {
  if (!window.pywebview) return;
  
  const btn = document.getElementById("btn-wizard-continue");
  if (btn) {
    if (btn.disabled) return;
    btn.disabled = true;
    btn.dataset.originalText = btn.innerHTML;
    btn.innerHTML = '<span>Đang xử lý...</span>';
  }

  const resetBtn = () => {
    if (btn) {
      btn.disabled = false;
      if (btn.dataset.originalText) {
        btn.innerHTML = btn.dataset.originalText;
      }
    }
  };

  const projectName = sanitizeFolderName(document.getElementById("wizard-project-name").value);
  if (!projectName) {
    window.alert("Vui lòng nhập tên dự án.");
    document.getElementById("wizard-project-name").focus();
    resetBtn();
    return;
  }

  const destText = document.getElementById("wizard-dest-path").textContent.trim();
  const destDir = wizardOutputTarget || destText;
  if (!destDir || destText === "Đang tải..." || destText.includes("<Chưa nhập tên dự án>")) {
    window.alert("Vui lòng chọn thư mục lưu kết quả hợp lệ.");
    resetBtn();
    return;
  }

  if (wizardContext === "upscale") {
    resetBtn();
    closeWizardModal();
    runUpscaleToProject(destDir);
    return;
  }

  const canUseService = await requireServiceLogin("upload và xử lý ảnh");
  if (!canUseService) {
    resetBtn();
    return;
  }

  resetBtn();
  closeWizardModal();

  // Show stop button in footer while running
  const footer = document.getElementById("upload-footer");
  if (footer) footer.style.display = "flex";

  if (currentService === "fotello") {
    const preferences = {
      contrast_style: document.getElementById("wizard-pref-contrast").value,
      exterior_sky_replacement: document.getElementById("wizard-pref-sky").checked ? "on" : "off",
      perspective_correction: document.getElementById("wizard-pref-perspective").checked ? "on" : "off",
      cloud_style: document.getElementById("wizard-pref-cloud").value,
      project_name: projectName,
      convert_to_jpg: document.getElementById("wizard-fotello-convert-jpg").checked,
    };

    const loader = document.getElementById("group-loading-overlay");
    if (loader) loader.classList.remove("hidden");

    try {
      addLog("🔎 Đang phân tích tự động các nhóm ảnh...", "info");
      const groupingMode = "order_fixed";
      const res = await window.pywebview.api.fotello_analyze_brackets(
        selectedInputFiles,
        3,
        groupingMode,
      );
      if (!res || !res.ok) {
        addLog("⚠ " + (res?.msg || "Lỗi phân tích nhóm ảnh"), "error");
        if (footer) footer.style.display = "none";
        return;
      }
      try {
        openBracketModal(
          res.brackets,
          selectedInputFiles,
          destDir,
          preferences,
          groupingMode,
        );
      } catch (e) {
        addLog("❌ Lỗi mở màn hình kiểm tra nhóm: " + e.message, "error");
        console.error("openBracketModal error:", e);
        if (footer) footer.style.display = "none";
      }
    } catch (e) {
      addLog("❌ Lỗi xử lý gom nhóm ảnh: " + e.message, "error");
      if (footer) footer.style.display = "none";
    } finally {
      if (loader) loader.classList.add("hidden");
    }
  } else if (currentService === "autoenhance") {
    const options = {
      preset: document.getElementById("wizard-autoenhance-model").value,
      ai_version: document.getElementById("wizard-autoenhance-version").value,
      sky_replacement: document.getElementById("wizard-autoenhance-sky-type").value,
      perspective_correction: document.getElementById("wizard-autoenhance-perspective").checked,
      lens_correction: document.getElementById("wizard-autoenhance-lens").checked,
      auto_privacy: document.getElementById("wizard-autoenhance-privacy").checked,
      window_pull: document.getElementById("wizard-autoenhance-window-type").value,
      grass: document.getElementById("wizard-autoenhance-grass-type").value,
      tv_blackout: document.getElementById("wizard-autoenhance-tv-type").value,
      fireplace: document.getElementById("wizard-autoenhance-fireplace-type").value,
      photographer: document.getElementById("wizard-autoenhance-photographer-type").value,
    };
    addLog("▶ Khởi chạy luồng Upload & Enhance (Autoenhance)...", "info");
    window.pywebview.api.autoenhance_upload(
      selectedInputFiles,
      destDir,
      projectName,
      options
    );
  } else {
    let activeProfileName = 'Mặc định';
    if (typeof styleProfiles !== 'undefined' && styleProfiles) {
      const actualProfileId = (typeof currentProfileId !== 'undefined' && currentProfileId) ? currentProfileId : 'default';
      let selectedProfile = styleProfiles.find(p => p.id.toString() === actualProfileId);
      if (!selectedProfile) {
        selectedProfile = styleProfiles.find(p => p.is_default);
      }
      if (selectedProfile) {
        activeProfileName = selectedProfile.name;
      }
    }

    const options = {
      model: document.getElementById("wizard-autohdr-model").value,
      processing_mode: document.getElementById("wizard-autohdr-mode").value,
      sky_replacement: document.getElementById("wizard-autohdr-sky").checked,
      perspective_correction: document.getElementById("wizard-autohdr-perspective").checked,
      grass_replacement: document.getElementById("wizard-autohdr-grass").checked,
      declutter: document.getElementById("wizard-autohdr-declutter").checked,
      strip_metadata_keep_copyright: document.getElementById("wizard-autohdr-strip-metadata").checked,
      style_profile_id: (typeof currentProfileId !== 'undefined' && currentProfileId) ? currentProfileId : 'default',
      style_profile_name: activeProfileName,
      convert_to_jpg: document.getElementById("wizard-autohdr-convert-jpg").checked,
      multi_style: document.getElementById("sp-multi-style") ? document.getElementById("sp-multi-style").checked : false,
    };

    if (window.deferredProfileUpdate && window.deferredProfileUpdate.id === options.style_profile_id) {
      options.deferred_profile = window.deferredProfileUpdate.payload;
      window.deferredProfileUpdate = null;
    }

    addLog("▶ Khởi chạy luồng Upload & Enhance (AutoHDR)...", "info");
    window.pywebview.api.autohdr_upload(
      selectedInputFiles,
      destDir,
      projectName,
      options,
    );
  }
}

// ═══ GROUP REVIEW LOGIC ═══
let currentUploadContext = null;
let bracketViewMode = "filmstrip";
let bracketThumbLoadToken = 0;
let isBracketDragging = false;

function waitForBracketDragIdle() {
  if (!isBracketDragging) return Promise.resolve();
  return new Promise((resolve) => {
    const timer = setInterval(() => {
      if (!isBracketDragging) {
        clearInterval(timer);
        resolve();
      }
    }, 80);
  });
}

function applyBracketViewMode(mode = "filmstrip") {
  bracketViewMode = mode === "carousel" ? "carousel" : "filmstrip";
  const select = document.getElementById("bracket-view-mode");
  const container = document.getElementById("bracket-container");

  if (select) select.value = bracketViewMode;
  if (container) {
    container.classList.toggle(
      "bracket-view-filmstrip",
      bracketViewMode === "filmstrip",
    );
    container.classList.toggle(
      "bracket-view-carousel",
      bracketViewMode === "carousel",
    );
  }

  if (currentUploadContext) {
    currentUploadContext.viewMode = bracketViewMode;
  }
  updateBracketCounts();
}

function createBracketHeader(titleText = "") {
  const header = document.createElement("div");
  header.className = "bracket-header";

  const title = document.createElement("h4");
  title.textContent = titleText;
  header.appendChild(title);

  const actions = document.createElement("div");
  actions.className = "bracket-header-actions";

  const controls = document.createElement("div");
  controls.className = "bracket-carousel-controls";

  const prevBtn = document.createElement("button");
  prevBtn.type = "button";
  prevBtn.className = "bracket-carousel-btn";
  prevBtn.textContent = "‹";
  prevBtn.onclick = () => moveBracketCarousel(prevBtn, -1);
  controls.appendChild(prevBtn);

  const counter = document.createElement("span");
  counter.className = "bracket-carousel-count";
  counter.textContent = "0/0";
  controls.appendChild(counter);

  const nextBtn = document.createElement("button");
  nextBtn.type = "button";
  nextBtn.className = "bracket-carousel-btn";
  nextBtn.textContent = "›";
  nextBtn.onclick = () => moveBracketCarousel(nextBtn, 1);
  controls.appendChild(nextBtn);

  actions.appendChild(controls);

  const deleteBtn = document.createElement("button");
  deleteBtn.type = "button";
  deleteBtn.className = "bracket-delete-btn";
  deleteBtn.innerHTML = "×";
  deleteBtn.title = "Xóa nhóm";
  deleteBtn.onclick = (e) => {
    e.stopPropagation();
    const groupEl = deleteBtn.closest(".bracket-group");
    if (!groupEl) return;
    groupEl.remove();
    updateBracketCounts();
  };
  actions.appendChild(deleteBtn);

  header.appendChild(actions);
  return header;
}

function setBracketActiveIndex(group, requestedIndex = 0) {
  if (!group) return;
  const items = Array.from(group.querySelectorAll(".bracket-item"));
  const count = items.length;
  const activeIndex = count
    ? Math.max(0, Math.min(requestedIndex, count - 1))
    : 0;

  group.dataset.activeIndex = String(activeIndex);
  items.forEach((item, idx) => {
    item.classList.toggle("active", idx === activeIndex);
  });

  const counter = group.querySelector(".bracket-carousel-count");
  if (counter) {
    counter.textContent = count ? `${activeIndex + 1}/${count}` : "0/0";
  }
}

function moveBracketCarousel(button, direction) {
  const group = button.closest(".bracket-group");
  if (!group) return;
  const count = group.querySelectorAll(".bracket-item").length;
  if (!count) return;

  const current = Number.parseInt(group.dataset.activeIndex || "0", 10) || 0;
  const next = (current + direction + count) % count;
  setBracketActiveIndex(group, next);
}

function createBracketItemElement(path) {
  const item = document.createElement("div");
  item.className = "bracket-item";
  item.dataset.img = path;

  const imgEl = document.createElement("img");
  imgEl.className = "bracket-thumb";
  imgEl.alt = path;
  imgEl.decoding = "async";
  imgEl.draggable = false;
  item.appendChild(imgEl);

  const txt = document.createElement("span");
  txt.className = "bracket-filename";
  txt.textContent = pathBasename(path);
  item.appendChild(txt);

  const delBtn = document.createElement("button");
  delBtn.type = "button";
  delBtn.className = "bracket-item-delete";
  delBtn.innerHTML = "×";
  delBtn.title = "Xóa ảnh này";
  delBtn.onclick = (e) => {
    e.stopPropagation();
    item.remove();
    updateBracketCounts();
  };
  item.appendChild(delBtn);

  return { item, imgEl };
}

function setupBracketDropEvents(list) {
  list.addEventListener("dragover", (e) => {
    e.preventDefault();
    e.stopPropagation();
    list.classList.add("drag-over-external");
  });
  list.addEventListener("dragleave", (e) => {
    e.preventDefault();
    e.stopPropagation();
    list.classList.remove("drag-over-external");
  });
  list.addEventListener("drop", async (e) => {
    e.preventDefault();
    e.stopPropagation();
    list.classList.remove("drag-over-external");
    if (!e.dataTransfer || !e.dataTransfer.files || e.dataTransfer.files.length === 0) return;

    const files = Array.from(e.dataTransfer.files);
    const addedPaths = [];
    const imageExtensions = [".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp", ".heic", ".heif", ".3fr", ".ari", ".arw", ".bay", ".cr2", ".cr3", ".crw", ".dcr", ".dng", ".erf", ".fff", ".gpr", ".iiq", ".k25", ".kdc", ".mdc", ".mef", ".mos", ".mrw", ".nef", ".nrw", ".orf", ".pef", ".raf", ".raw", ".rw2", ".rwl", ".sr2", ".srf", ".srw", ".x3f"];

    for (const file of files) {
      if (file.path) {
        const ext = file.path.substring(file.path.lastIndexOf(".")).toLowerCase();
        if (imageExtensions.includes(ext)) {
          addedPaths.push(file.path);
        }
      }
    }

    if (addedPaths.length === 0) return;

    for (const path of addedPaths) {
      const exists = Array.from(list.querySelectorAll(".bracket-item")).some(
        (item) => item.dataset.img === path
      );
      if (exists) continue;

      const { item, imgEl } = createBracketItemElement(path);
      list.appendChild(item);

      try {
        const thumbData = await window.pywebview.api.get_thumbnail_base64(path);
        if (thumbData) {
          imgEl.src = thumbData;
        }
      } catch (err) {}
    }

    updateBracketCounts();
  });
}

function createBracketSortable(list) {
  if (!window.Sortable) return;
  new Sortable(list, {
    group: "shared-brackets",
    animation: 0,
    draggable: ".bracket-item",
    filter: ".bracket-carousel-btn",
    preventOnFilter: false,
    ghostClass: "sortable-ghost",
    chosenClass: "sortable-chosen",
    dragClass: "sortable-drag",
    forceFallback: true,
    fallbackClass: "sortable-fallback",
    fallbackOnBody: true,
    fallbackTolerance: 4,
    scroll: true,
    scrollSensitivity: 70,
    scrollSpeed: 14,
    swapThreshold: 0.45,
    setData: function (dataTransfer) {
      if (dataTransfer) dataTransfer.setData("text/plain", "");
    },
    onStart: function () {
      isBracketDragging = true;
      document.body.classList.add("bracket-dragging");
    },
    onEnd: function () {
      isBracketDragging = false;
      document.body.classList.remove("bracket-dragging");
      
      // Remove empty groups
      const container = document.getElementById("bracket-container");
      if (container) {
        const groups = container.querySelectorAll(".bracket-group");
        groups.forEach((group) => {
          const list = group.querySelector(".bracket-list");
          if (list && list.querySelectorAll(".bracket-item").length === 0) {
            group.remove();
          }
        });
      }
      
      updateBracketCounts();
    },
  });
}

function openBracketModal(
  brackets,
  inputSource,
  savedir,
  preferences,
  groupingMode = "order_fixed",
  viewMode = "filmstrip",
  bracketSize = 3,
  context = "upload",
) {
  const normalizedMode = syncFotelloBracketMode(groupingMode);
  const sizeSelect = document.getElementById("bracket-group-size");
  if (sizeSelect) sizeSelect.value = String(bracketSize);

  currentUploadContext = {
    inputdir: inputSource,
    savedir,
    preferences,
    groupingMode: normalizedMode,
    bracketSize,
    viewMode: viewMode === "carousel" ? "carousel" : "filmstrip",
    context: context === "merge" ? "merge" : "upload",
  };

  const title = document.getElementById("bracket-modal-title");
  if (title) {
    title.textContent = currentUploadContext.context === "merge"
      ? "Kiểm tra nhóm ảnh gốc Merge"
      : "Kiểm tra nhóm ảnh (Kéo & Thả)";
  }

  const container = document.getElementById("bracket-container");
  container.innerHTML = "";
  applyBracketViewMode(currentUploadContext.viewMode);
  const loadToken = ++bracketThumbLoadToken;

  // Collect all thumb requests to load in batch after DOM is ready
  const thumbQueue = [];

  brackets.forEach((b, i) => {
    const groupEl = document.createElement("div");
    groupEl.className = "bracket-group";
    groupEl.dataset.activeIndex = "0";
    groupEl.appendChild(createBracketHeader("Nhóm " + (i + 1) + " (" + b.length + " ảnh)"));

    const list = document.createElement("div");
    list.className = "bracket-list";
    list.id = "bracket-list-" + i;

    b.forEach((img) => {
      const { item, imgEl } = createBracketItemElement(img);
      list.appendChild(item);

      // Queue thumbnail load
      thumbQueue.push({ imgEl, path: img });
    });

    groupEl.appendChild(list);
    container.appendChild(groupEl);

    setupBracketDropEvents(list);

    createBracketSortable(list);
    setBracketActiveIndex(groupEl, 0);
  });

  if (window.pywebview && window.pywebview.api && window.pywebview.api.bind_bracket_drops) {
    const selectors = brackets.map((_, idx) => `#bracket-list-${idx}`);
    window.pywebview.api.bind_bracket_drops(selectors);
  }

  document.getElementById("bracket-modal-status").textContent =
    `Tổng cộng: ${brackets.length} nhóm`;
  const confirmBtn = document.getElementById("btn-confirm-bracket-upload");
  if (confirmBtn) {
    confirmBtn.textContent = currentUploadContext.context === "merge"
      ? "Xác nhận"
      : "Xác nhận & Upload";
  }
  document.getElementById("bracket-review-modal").classList.remove("hidden");
  scheduleBracketContextTour();

  // Load thumbnails sequentially (avoid flooding the backend, yield to browser rendering)
  (async () => {
    for (const { imgEl, path } of thumbQueue) {
      if (loadToken !== bracketThumbLoadToken) break;
      if (
        document
          .getElementById("bracket-review-modal")
          .classList.contains("hidden")
      ) {
        break; // Stop loading if modal was closed
      }
      await waitForBracketDragIdle();
      if (loadToken !== bracketThumbLoadToken) break;
      try {
        const thumbData = await window.pywebview.api.get_thumbnail_base64(path);
        if (loadToken === bracketThumbLoadToken && thumbData) {
          imgEl.src = thumbData;
        }
      } catch (e) {}
      // Yield execution to the browser UI thread
      await new Promise((resolve) => setTimeout(resolve, 35));
    }
  })();
}

async function reanalyzeBracketModal() {
  if (!window.pywebview || !currentUploadContext) return;

  const mode = syncFotelloBracketMode(
    document.getElementById("bracket-modal-mode")?.value ||
      currentUploadContext.groupingMode ||
      "order_fixed",
  );
  currentUploadContext.groupingMode = mode;

  const btn = document.getElementById("btn-reanalyze-brackets");
  const oldText = btn ? btn.textContent : "";
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Đang phân tích...";
  }

  try {
    addLog("🔎 Đang phân tích lại nhóm theo logic mới...", "info");
    const sizeSelect = document.getElementById("bracket-group-size");
    const bracketSize = sizeSelect ? parseInt(sizeSelect.value, 10) : 3;
    currentUploadContext.bracketSize = bracketSize;

    const res = currentUploadContext.context === "merge"
      ? await window.pywebview.api.detail_merge_analyze_brackets(
          currentUploadContext.inputdir,
          bracketSize,
          mode,
        )
      : await window.pywebview.api.fotello_analyze_brackets(
          currentUploadContext.inputdir,
          bracketSize,
          mode,
        );
    if (!res || !res.ok) {
      addLog("⚠ " + (res?.msg || "Lỗi phân tích lại nhóm"), "error");
      return;
    }

    openBracketModal(
      res.brackets,
      currentUploadContext.inputdir,
      currentUploadContext.savedir,
      currentUploadContext.preferences,
      mode,
      currentUploadContext.viewMode,
      bracketSize,
      currentUploadContext.context || "upload",
    );
  } catch (e) {
    addLog("❌ Lỗi phân tích lại nhóm: " + e.message, "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = oldText || "Áp dụng lại";
    }
  }
}

function updateBracketCounts() {
  const container = document.getElementById("bracket-container");
  if (!container) return;
  const groups = container.querySelectorAll(".bracket-group");
  let totalBrackets = 0;

  groups.forEach((group, idx) => {
    const list = group.querySelector(".bracket-list");
    const items = list.querySelectorAll(".bracket-item");
    const count = items.length;

    const title = group.querySelector("h4");
    if (title) {
      title.textContent = `Nhóm ${idx + 1} (${count} ảnh)`;
    }
    setBracketActiveIndex(
      group,
      Number.parseInt(group.dataset.activeIndex || "0", 10) || 0,
    );

    if (count > 0) {
      totalBrackets++;
    }
  });

  document.getElementById("bracket-modal-status").textContent =
    `Tổng cộng: ${totalBrackets} nhóm`;
}

function addNewBracketGroup() {
  const container = document.getElementById("bracket-container");
  const groupIndex = container.querySelectorAll(".bracket-group").length;

  const groupEl = document.createElement("div");
  groupEl.className = "bracket-group";
  groupEl.dataset.activeIndex = "0";
  groupEl.appendChild(createBracketHeader(`Nhóm ${groupIndex + 1} (0 ảnh)`));

  const list = document.createElement("div");
  list.className = "bracket-list";
  list.id = "bracket-list-new-" + Date.now() + "-" + groupIndex;

  groupEl.appendChild(list);
  container.appendChild(groupEl);

  setupBracketDropEvents(list);
  createBracketSortable(list);

  if (window.pywebview && window.pywebview.api && window.pywebview.api.bind_bracket_drops) {
    window.pywebview.api.bind_bracket_drops(["#" + list.id]);
  }

  updateBracketCounts();
  // Scroll container to the right/bottom to show the new group
  container.scrollLeft = container.scrollWidth;
}

function closeBracketModal() {
  bracketThumbLoadToken++;
  isBracketDragging = false;
  document.body.classList.remove("bracket-dragging");
  document.getElementById("bracket-review-modal").classList.add("hidden");
  currentUploadContext = null;
}

function confirmBracketUpload() {
  if (!currentUploadContext) return;

  const container = document.getElementById("bracket-container");
  const lists = container.querySelectorAll(".bracket-list");

  const finalBrackets = [];
  lists.forEach((list) => {
    const items = list.querySelectorAll(".bracket-item");
    if (items.length > 0) {
      const arr = Array.from(items).map((item) => item.dataset.img);
      finalBrackets.push(arr);
    }
  });

  if (finalBrackets.length === 0) {
    addLog("⚠ Không có nhóm nào hợp lệ.", "warn");
    return;
  }

  const { inputdir, savedir, preferences, context } = currentUploadContext;
  closeBracketModal();

  if (context === "merge") {
    detailMergeFolders.original = finalBrackets;
    updateMergeFolderUI();
    addLog(`Đã lưu ${finalBrackets.length} nhóm bracket ảnh gốc. Bạn có thể bấm "Bắt đầu merge chi tiết" để gộp và merge cùng lúc.`, "success");
    return;
  }

  addLog(

    `▶ Khởi chạy luồng Upload & Enhance Fotello với ${finalBrackets.length} nhóm đã được kiểm duyệt...`,
    "info",
  );
  window.pywebview.api.fotello_upload(
    inputdir,
    savedir,
    preferences,
    finalBrackets,
  );
}

let fotelloStatsUpdateTimer = null;

// ═══ DOWNLOAD LISTINGS ACTION ═══

let listingsData = [];

function getFakeListings(service) {
  const prefix = service === "fotello" ? "Fotello" : "AutoHDR";
  return [
    { id: `fake-${service}-1`, name: `BietThu_HoTay_${prefix}`, brackets: "120", created_at: "2026-06-18 15:30", isFake: true },
    { id: `fake-${service}-2`, name: `DuAn_VIP_01_${prefix}`, brackets: "45", created_at: "2026-06-18 10:00", isFake: true },
    { id: `fake-${service}-3`, name: `ChungCu_Landmark_${prefix}`, brackets: "32", created_at: "2026-06-17 09:15", isFake: true },
    { id: `fake-${service}-4`, name: `NhaPho_Q1_${prefix}`, brackets: "18", created_at: "2026-06-16 14:20", isFake: true },
    { id: `fake-${service}-5`, name: `Demo_${prefix}`, brackets: "5", created_at: "2026-06-15 08:00", isFake: true }
  ];
}

async function refreshListings(force = false) {
  if (document.querySelector('.introjs-overlay')) {
    if (!listingsData.some(l => l.isFake)) {
      listingsData = getFakeListings(currentService);
      renderListings();
    }
    return;
  }

  if (!window.pywebview) return;
  const tbody = document.getElementById("listings-list");

  const serviceName = serviceDisplayName();

  // Check login first
  const loggedIn = await isServiceLoggedIn(currentService);
  if (!loggedIn) {
    if (force) {
      const canUseService = await requireServiceLogin("tải danh sách listings");
      if (!canUseService) {
        listingsData = [];
        renderListings();
        return;
      }
    } else {
      listingsData = [];
      renderListings();
      return;
    }
  }

  tbody.innerHTML =
    '<tr><td colspan="4" class="empty-state">Đang tải danh sách Listings...</td></tr>';

  try {
    let res;
    if (currentService === "fotello") {
      res = await window.pywebview.api.fotello_list_listings(force);
    } else if (currentService === "autohdr") {
      res = await window.pywebview.api.autohdr_list_listings(force);
    } else if (currentService === "autoenhance") {
      res = await window.pywebview.api.autoenhance_list_listings();
      // Autoenhance orders are listed as orders not listings, but data format should be mapped to listing format in backend or here.
      // Assuming autoenhance_list_listings returns {ok: true, data: [{id, name, ...}]}
      if (res && res.ok && res.data) {
          res.listings = res.data.map(order => ({
              id: order.order_id,
              name: order.name || 'Untitled order',
              brackets: order.total_images || 0,
              created_at: order.created_at || 'N/A'
          }));
      }
    } else {
      res = await window.pywebview.api.autohdr_list_listings(force);
    }
    if (res.ok) {
      listingsData = res.listings || [];
      renderListings();
      checkConnectionStatus();
    } else {
      tbody.innerHTML = `<tr><td colspan="4" class="empty-state error">❌ Lỗi: ${res.msg || "Không thể tải danh sách."}</td></tr>`;
    }
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="4" class="empty-state error">❌ Lỗi kết nối API: ${e}</td></tr>`;
  }
}

function renderListings() {
  const tbody = document.getElementById("listings-list");
  if (listingsData.length === 0) {
    tbody.innerHTML =
      '<tr><td colspan="4" class="empty-state">Không có dữ liệu dự án. Vui lòng tải dự án mới hoặc kiểm tra lại đăng nhập.</td></tr>';
    return;
  }

  tbody.innerHTML = listingsData
    .map(
      (l, idx) => `
    <tr id="tr-listing-${l.id}">
      <td><input type="checkbox" class="listing-cb" value="${l.id}" onchange="updateRowHighlight('${l.id}')"></td>
      <td class="listing-name" style="font-weight: 500; cursor: pointer; color: var(--primary); text-decoration: underline;" onclick="viewListingDetails('${l.id}', '${l.name}')">${l.name} ${l.isFake ? '<span style="font-size: 10px; background: var(--border); padding: 2px 6px; border-radius: 4px; margin-left: 8px;">Mẫu</span>' : ''}</td>
      <td>${l.brackets}</td>
      <td>${l.created_at}</td>
    </tr>
  `,
    )
    .join("");

  document.getElementById("select-all-cb").checked = false;
}

async function viewListingDetails(id, name) {
  if (listingsData.find(l => l.id === id)?.isFake) {
    addLog("Bạn đang xem dữ liệu mẫu. Hãy đăng nhập để xem dữ liệu thật.", "info");
    return;
  }
  const modal = document.getElementById('listing-images-modal');
  const title = document.getElementById('listing-images-title');
  const grid = document.getElementById('listing-images-grid');
  const loading = document.getElementById('listing-images-loading');
  
  if (!modal) return;
  title.innerText = `Danh sách: ${name}`;
  title.style.flex = "1";
  title.style.textAlign = "center";
  title.style.fontSize = "18px";
  title.style.letterSpacing = "0.5px";
  
  grid.innerHTML = '';
  loading.classList.remove('hidden');
  modal.classList.remove('hidden');
  
  // Reset selection states
  currentListingPhotos = [];
  selectedPhotoIds.clear();
  currentPhotoshootId = null;
  currentPhotoshootUserId = null;
  currentListingId = id;
  currentListingService = currentService;
  updateSelectedCountUI();
  
  try {
    let res;
    if (currentListingService === 'fotello') {
      res = await window.pywebview.api.fotello_get_listing_details(id);
    } else if (currentListingService === 'autoenhance') {
      res = await window.pywebview.api.autoenhance_get_listing_details(id);
    } else {
      res = await window.pywebview.api.autohdr_get_listing_details(id);
    }
    loading.classList.add('hidden');
    if (res.ok) {
      currentPhotoshootId = res.photoshoot_id;
      currentPhotoshootUserId = res.user_id;
      currentListingPhotos = res.photos || [];
      
      if (currentListingPhotos.length > 0) {
        grid.innerHTML = currentListingPhotos.map((p, index) => {
          let imgSrc = p.url || "assets/autoenhance-logo.svg";
          if (currentListingService === 'autoenhance' && !p.url) {
            imgSrc = "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='100%' height='100%' viewBox='0 0 100 100'><rect width='100' height='100' fill='%231a1a24'/></svg>";
          }
          return `
            <div class="gallery-item" onclick="toggleSelectPhoto(this, ${index})">
              <div class="gallery-select-badge">✓</div>
              <img id="img-thumb-${p.id}" src="${imgSrc}" loading="lazy" decoding="async" style="${currentListingService === 'autoenhance' && !p.url ? 'opacity: 0.5; filter: blur(2px); transition: all 0.3s ease;' : ''}">
              <div class="gallery-caption" title="${p.name}">${p.name}</div>
            </div>
          `;
        }).join('');

        if (currentListingService === 'autoenhance') {
          currentListingPhotos.forEach(async (p) => {
            if (!p.url) {
              try {
                let thumbRes = await window.pywebview.api.autoenhance_get_image_thumbnail_base64(p.id);
                if (thumbRes && thumbRes.ok && thumbRes.base64) {
                  p.url = thumbRes.base64;
                  p.thumb_url = thumbRes.base64;
                  const imgEl = document.getElementById(`img-thumb-${p.id}`);
                  if (imgEl) {
                    imgEl.src = thumbRes.base64;
                    imgEl.style.opacity = "1";
                    imgEl.style.filter = "none";
                  }
                } else {
                  const imgEl = document.getElementById(`img-thumb-${p.id}`);
                  if (imgEl) {
                    imgEl.src = "assets/autoenhance-logo.svg";
                    imgEl.style.opacity = "0.7";
                    imgEl.style.filter = "none";
                  }
                }
              } catch (e) {
                console.error("Error loading thumbnail for " + p.id, e);
              }
            }
          });
        }
      } else {
        grid.innerHTML = '<div style="grid-column: 1/-1; text-align: center; padding: 20px;">Không có ảnh nào được tìm thấy.</div>';
      }
    } else {
      grid.innerHTML = `<div style="grid-column: 1/-1; text-align: center; padding: 20px; color: var(--error);">Lỗi: ${res.msg}</div>`;
    }
  } catch(e) {
    loading.classList.add('hidden');
    grid.innerHTML = `<div style="grid-column: 1/-1; text-align: center; padding: 20px; color: var(--error);">Lỗi kết nối API.</div>`;
  }
}

function closeListingImagesModal() {
  const modal = document.getElementById('listing-images-modal');
  if (modal) modal.classList.add('hidden');
}

function toggleSelectPhoto(element, photoIndex) {
  const photoId = currentListingPhotos[photoIndex]?.id;
  if (photoId === undefined || photoId === null) return;
  if (selectedPhotoIds.has(photoId)) {
    selectedPhotoIds.delete(photoId);
    element.classList.remove('selected');
  } else {
    selectedPhotoIds.add(photoId);
    element.classList.add('selected');
  }
  updateSelectedCountUI();
}

function selectAllListingImages(select) {
  const items = document.querySelectorAll('#listing-images-grid .gallery-item');
  selectedPhotoIds.clear();
  
  if (select && currentListingPhotos.length > 0) {
    currentListingPhotos.forEach(p => {
      if (p.id !== undefined && p.id !== null) selectedPhotoIds.add(p.id);
    });
    items.forEach(el => el.classList.add('selected'));
  } else {
    items.forEach(el => el.classList.remove('selected'));
  }
  updateSelectedCountUI();
}

function updateSelectedCountUI() {
  const countEl = document.getElementById('selected-images-count');
  const btnDownload = document.getElementById('btn-download-selected-images');
  if (countEl) {
    countEl.innerText = selectedPhotoIds.size;
  }
  if (btnDownload) {
    btnDownload.disabled = (selectedPhotoIds.size === 0);
  }
}

async function downloadSelectedImages() {
  if (selectedPhotoIds.size === 0) return;
  if (currentListingService === 'autohdr' && !currentPhotoshootId) {
    addLog("Không tìm thấy thông tin photoshoot.", "error");
    return;
  }
  if (currentListingService === 'fotello' && !currentListingId) {
    addLog("Không tìm thấy thông tin listing.", "error");
    return;
  }
  
  const selectedPhotos = currentListingPhotos.filter(p => selectedPhotoIds.has(p.id));
  const defaultName = sanitizeFolderName(
    `${serviceDisplayName(currentListingService)}_${selectedPhotos.length}_anh`,
  ) || "selected-images";
  const outputChoice = await requestOutputDirForFolder(defaultName);
  if (!outputChoice) {
    addLog("⚠ Đã hủy vì chưa chọn nơi lưu.", "warn");
    return;
  }
  
  closeListingImagesModal();

  let res;
  if (currentListingService === 'fotello') {
    res = await window.pywebview.api.fotello_download_selected_photos(
      currentListingId,
      selectedPhotos.map(photo => photo.id),
      outputChoice.outputDir,
    );
  } else if (currentListingService === 'autoenhance') {
    res = await window.pywebview.api.autoenhance_download_selected_photos(
      currentListingId,
      selectedPhotos.map(photo => photo.id),
      outputChoice.outputDir,
    );
  } else {
    res = await window.pywebview.api.autohdr_download_selected_photos(
      currentListingId,
      currentPhotoshootId,
      currentPhotoshootUserId,
      selectedPhotos,
      outputChoice.outputDir,
    );
  }
  
  if (!res || !res.ok) {
    addLog(`Lỗi tải ảnh: ${res ? res.msg : "Không xác định"}`, "error");
  }
}

function toggleRowCheckbox(id) {
  const tr = document.getElementById(`tr-listing-${id}`);
  if (!tr) return;
  const cb = tr.querySelector(".listing-cb");
  cb.checked = !cb.checked;
  updateRowHighlight(id);
}

function updateRowHighlight(id) {
  const tr = document.getElementById(`tr-listing-${id}`);
  if (!tr) return;
  const cb = tr.querySelector(".listing-cb");
  if (cb.checked) {
    tr.classList.add("selected");
  } else {
    tr.classList.remove("selected");
  }
}

function toggleAllListings() {
  const checked = document.getElementById("select-all-cb").checked;
  document.querySelectorAll(".listing-cb").forEach((cb) => {
    cb.checked = checked;
    const tr = cb.closest("tr");
    if (checked) {
      tr.classList.add("selected");
    } else {
      tr.classList.remove("selected");
    }
  });
}

async function startDownload() {
  if (!window.pywebview) return;
  const canUseService = await requireServiceLogin("tải ảnh từ listings");
  if (!canUseService) return;

  const selectedIds = [];
  document.querySelectorAll(".listing-cb:checked").forEach((cb) => {
    selectedIds.push(cb.value);
  });

  if (selectedIds.length === 0) {
    addLog("⚠ Vui lòng chọn ít nhất 1 listing để tải về.", "warn");
    return;
  }

  const firstListing = listingsData.find((item) => item.id === selectedIds[0]);
  const listingFolderName =
    selectedIds.length === 1
      ? firstListing?.name
      : `${firstListing?.name || serviceDisplayName()} (+${selectedIds.length - 1})`;
  const defaultName = sanitizeFolderName(
    listingFolderName || `${currentService}-download-${selectedIds.length}`,
  );
  const outputChoice = await requestOutputDirForFolder(defaultName);
  if (!outputChoice) return;

  if (currentService === "fotello") {
    addLog(
      `▶ Đang thêm ${selectedIds.length} listings của Fotello vào hàng đợi tải...`,
      "info",
    );
    window.pywebview.api.fotello_download(
      selectedIds,
      outputChoice.outputDir,
      defaultName,
    );
  } else if (currentService === "autoenhance") {
    addLog(
      `▶ Đang thêm ${selectedIds.length} orders của Autoenhance vào hàng đợi tải...`,
      "info",
    );
    window.pywebview.api.autoenhance_download(
      selectedIds,
      outputChoice.outputDir,
    );
  } else {
    addLog(
      `▶ Đang thêm ${selectedIds.length} listings của AutoHDR vào hàng đợi tải...`,
      "info",
    );
    window.pywebview.api.autohdr_download(
      selectedIds,
      outputChoice.outputDir,
      defaultName,
    );
  }

  // Clear checkboxes and selection styles
  document.querySelectorAll(".listing-cb").forEach((cb) => {
    cb.checked = false;
    const tr = cb.closest("tr");
    if (tr) tr.classList.remove("selected");
  });
  const selectAll = document.getElementById("select-all-cb");
  if (selectAll) selectAll.checked = false;
}

async function stopTask() {
  if (!await confirm("Bạn có chắc chắn muốn dừng tác vụ này không?")) return;
  if (window.pywebview) {
    if (currentTab === "upscale" && window.pywebview.api.stop_all_tasks) {
      window.pywebview.api.stop_all_tasks();
      return;
    }
    if (currentService === "fotello") {
      window.pywebview.api.fotello_stop();
    } else if (currentService === "autoenhance") {
      window.pywebview.api.autoenhance_stop();
    } else {
      window.pywebview.api.autohdr_stop();
    }
  }
}

// ═══ AUTO-UPDATE VERSION CHECK ═══
let updateDownloadUrl = "";
let isForceUpdate = false;

async function checkAppUpdate() {
  if (!window.pywebview) return;
  try {
    const res = await window.pywebview.api.check_app_update();
    if (res && res.success && res.has_update) {
      updateDownloadUrl = res.download_url;
      isForceUpdate = res.force_update;

      const currVerEl = document.getElementById("update-curr-ver");
      const lateVerEl = document.getElementById("update-late-ver");
      const notesEl = document.getElementById("update-notes-content");
      const warningEl = document.getElementById("update-force-warning");
      const btnLater = document.getElementById("btn-update-later");
      const btnClose = document.getElementById("btn-close-update");

      if (currVerEl) currVerEl.textContent = res.current_version;
      if (lateVerEl) lateVerEl.textContent = res.latest_version;
      if (notesEl)
        notesEl.textContent =
          res.update_notes || "Cải tiến hiệu năng và sửa lỗi.";

      if (isForceUpdate) {
        if (warningEl) warningEl.classList.remove("hidden");
        if (btnLater) btnLater.classList.add("hidden");
        if (btnClose) btnClose.classList.add("hidden");
      } else {
        if (warningEl) warningEl.classList.add("hidden");
        if (btnLater) btnLater.classList.remove("hidden");
        if (btnClose) btnClose.classList.remove("hidden");
      }

      const modal = document.getElementById("update-modal");
      if (modal) modal.classList.remove("hidden");
    }
  } catch (e) {
    console.error("Check update failed:", e);
  }
}

function doUpdateApp() {
  const btnUpdate = document.getElementById("btn-update-now");
  const btnLater = document.getElementById("btn-update-later");
  const btnClose = document.getElementById("btn-close-update");

  if (btnUpdate) {
    btnUpdate.disabled = true;
    btnUpdate.textContent = "Đang tải bản cập nhật... 0%";
  }
  if (btnLater) btnLater.classList.add("hidden");
  if (btnClose) btnClose.classList.add("hidden");

  if (window.pywebview) {
    window.pywebview.api.start_auto_update();
  }
}

window.updateDownloadProgress = function (percent) {
  const btnUpdate = document.getElementById("btn-update-now");

  if (percent <= 0) {
    if (btnUpdate) {
      btnUpdate.disabled = false;
      btnUpdate.textContent = "⚡ Cập nhật ngay";
    }
    const btnLater = document.getElementById("btn-update-later");
    const btnClose = document.getElementById("btn-close-update");
    if (!isForceUpdate) {
      if (btnLater) btnLater.classList.remove("hidden");
      if (btnClose) btnClose.classList.remove("hidden");
    }
    return;
  }

  if (btnUpdate) {
    btnUpdate.textContent = `Đang tải bản cập nhật... ${percent}%`;
    if (percent >= 100) {
      btnUpdate.textContent = "Đang cài đặt và khởi động lại...";
    }
  }
};

window.updateInstallStatus = function (status, message = "") {
  const btnUpdate = document.getElementById("btn-update-now");
  const btnLater = document.getElementById("btn-update-later");
  const btnClose = document.getElementById("btn-close-update");

  if (status === "dev_saved") {
    if (btnUpdate) {
      btnUpdate.disabled = false;
      btnUpdate.textContent = "✅ Đã tải xong";
    }
    if (btnLater) {
      btnLater.classList.remove("hidden");
      btnLater.textContent = "Đóng";
    }
    if (btnClose) btnClose.classList.remove("hidden");
    addLog(`💡 Đang chạy code nguồn. Đã lưu tệp cập nhật tại: ${message}`, "info");
    return;
  }

  if (status === "error") {
    if (btnUpdate) {
      btnUpdate.disabled = false;
      btnUpdate.textContent = "Thử cập nhật lại";
    }
    if (!isForceUpdate) {
      if (btnLater) btnLater.classList.remove("hidden");
      if (btnClose) btnClose.classList.remove("hidden");
    }
    addLog(`❌ Lỗi tự động cập nhật: ${message || "Không rõ nguyên nhân"}`, "error");
  }
};

function closeUpdateModal() {
  if (isForceUpdate) return; // Prevent closing if force update
  const modal = document.getElementById("update-modal");
  if (modal) modal.classList.add("hidden");
}

function selectCloudStyle(value) {
  const select = document.getElementById('wizard-pref-cloud');
  if (select) select.value = value;
  
  document.querySelectorAll('.cloud-style-item').forEach(el => {
    el.classList.remove('active');
  });
  
  document.querySelectorAll('.cloud-check').forEach(el => {
    el.style.display = 'none';
  });
  
  const checkIcon = document.getElementById('check-cloud-' + value);
  if (checkIcon) {
    checkIcon.style.display = 'block';
    const parentLabel = checkIcon.closest('.cloud-style-item');
    if (parentLabel) {
      parentLabel.classList.add('active');
    }
  }
}
window.selectCloudStyle = selectCloudStyle;

// Upscale preferences and submenu controllers
window.openUpscaleSubmenu = function(type) {
  const mainView = document.getElementById("wizard-upscale-prefs-main");
  const subView = document.getElementById("wizard-upscale-prefs-" + type);
  const container = document.getElementById("wizard-upscale-prefs");
  if (!mainView || !subView || !container) return;

  mainView.style.transform = "translateX(-100%)";
  subView.style.transform = "translateX(0)";
  container.style.height = "160px";
};

window.closeUpscaleSubmenu = function(type) {
  const mainView = document.getElementById("wizard-upscale-prefs-main");
  const subView = document.getElementById("wizard-upscale-prefs-" + type);
  const container = document.getElementById("wizard-upscale-prefs");
  if (!mainView || !subView || !container) return;

  subView.style.transform = "translateX(100%)";
  mainView.style.transform = "translateX(0)";
  container.style.height = "340px";
};

window.selectUpscaleScale = function(value, labelText) {
  const select = document.getElementById("wizard-upscale-scale");
  if (select) {
    select.value = value;
    const event = new Event('change', { bubbles: true });
    select.dispatchEvent(event);
  }

  const labelEl = document.getElementById("wizard-upscale-scale-label");
  if (labelEl) {
    labelEl.textContent = labelText;
  }

  const subView = document.getElementById("wizard-upscale-prefs-scale");
  if (subView) {
    subView.querySelectorAll(".upscale-scale-check").forEach((check) => {
      check.style.display = "none";
    });
    subView.querySelectorAll(".upscale-scale-item").forEach((item) => {
      item.classList.remove("active");
    });

    const valShort = value.replace("upscale_", "");
    const activeCheck = document.getElementById("check-upscale-" + valShort);
    if (activeCheck) {
      activeCheck.style.display = "block";
      activeCheck.parentElement.classList.add("active");
    }
  }
};

// Autoenhance preferences and submenu controllers
window.openAutoenhanceSubmenu = function(type) {
  const mainView = document.getElementById("wizard-autoenhance-prefs-main");
  const subView = document.getElementById("wizard-autoenhance-prefs-" + type);
  const container = document.getElementById("wizard-autoenhance-prefs");
  if (!mainView || !subView || !container) return;

  mainView.style.transform = "translateX(-100%)";
  subView.style.transform = "translateX(0)";

  let height = "240px";
  if (type === "sky") height = "240px";
  else if (type === "window") height = "160px";
  else if (type === "grass") height = "120px";
  else if (type === "tv") height = "120px";
  else if (type === "fireplace") height = "120px";
  else if (type === "photographer") height = "120px";

  container.style.height = height;
};

window.closeAutoenhanceSubmenu = function(type) {
  const mainView = document.getElementById("wizard-autoenhance-prefs-main");
  const subView = document.getElementById("wizard-autoenhance-prefs-" + type);
  const container = document.getElementById("wizard-autoenhance-prefs");
  if (!mainView || !subView || !container) return;

  subView.style.transform = "translateX(100%)";
  mainView.style.transform = "translateX(0)";
  container.style.height = "240px";
};

window.selectAutoenhanceOption = function(type, value, labelText) {
  const hiddenInput = document.getElementById("wizard-autoenhance-" + type + "-type");
  if (hiddenInput) {
    hiddenInput.value = value;
  }

  const labelEl = document.getElementById("wizard-autoenhance-" + type + "-label");
  if (labelEl) {
    labelEl.textContent = labelText;
  }

  const subView = document.getElementById("wizard-autoenhance-prefs-" + type);
  if (subView) {
    subView.querySelectorAll(".check-icon").forEach((check) => {
      check.style.display = "none";
    });
    subView.querySelectorAll(".check-menu-item").forEach((item) => {
      item.classList.remove("active");
    });

    const activeCheck = document.getElementById("check-ae-" + type + "-" + value);
    if (activeCheck) {
      activeCheck.style.display = "block";
      activeCheck.parentElement.classList.add("active");
    }
  }

  closeAutoenhanceSubmenu(type);
};

window.resetAutoenhanceOption = function(type, value, labelText) {
  const hiddenInput = document.getElementById("wizard-autoenhance-" + type + "-type");
  if (hiddenInput) {
    hiddenInput.value = value;
  }

  const labelEl = document.getElementById("wizard-autoenhance-" + type + "-label");
  if (labelEl) {
    labelEl.textContent = labelText;
  }

  const subView = document.getElementById("wizard-autoenhance-prefs-" + type);
  if (subView) {
    subView.querySelectorAll(".check-icon").forEach((check) => {
      check.style.display = "none";
    });
    subView.querySelectorAll(".check-menu-item").forEach((item) => {
      item.classList.remove("active");
    });

    const activeCheck = document.getElementById("check-ae-" + type + "-" + value);
    if (activeCheck) {
      activeCheck.style.display = "block";
      activeCheck.parentElement.classList.add("active");
    }
  }
};

window.toggleWizardVersionDropdown = function(event) {
  if (event) event.stopPropagation();
  const dropdown = document.getElementById("wizard-version-dropdown");
  if (dropdown) {
    dropdown.classList.toggle("hidden");
  }
};

window.selectWizardVersion = function(value, event) {
  if (event) event.stopPropagation();
  const select = document.getElementById("wizard-autoenhance-version");
  if (select) {
    select.value = value;
  }
  const label = document.getElementById("wizard-version-display-label");
  if (label) {
    if (value === "beta") {
        label.textContent = "V5 Beta";
    } else if (value === "latest") {
        label.textContent = "V5 Latest";
    } else {
        label.textContent = "V5 Stable";
    }
  }
  const dropdown = document.getElementById("wizard-version-dropdown");
  if (dropdown) {
    dropdown.classList.add("hidden");
  }
};



// Intro.js onboarding and contextual feature guides
const ONBOARDING_STORAGE_KEY = "autofotello_onboarding_v2";
const FEATURE_GUIDES_STORAGE_KEY = "autofotello_feature_guides_v1";
const WIZARD_CONTEXT_GUIDE_KEY = "wizard-section-tour";
const UPSCALE_WIZARD_GUIDE_KEY = "upscale-wizard-section-tour";
const UPSCALE_MAIN_GUIDE_KEY = "upscale-main-section-tour";
const BRACKET_CONTEXT_GUIDE_KEY = "bracket-section-tour";
let onboardingPromptScheduled = false;
let featureGuideInProgress = false;
let persistentGuideState = { onboarding: "", features: {} };
let persistentGuideStateLoaded = false;

const featureGuideDefinitions = [
  {
    key: "service-selector",
    selector: "#service-dropdown",
    title: "Chọn dịch vụ",
    intro: "Chuyển đổi giữa Fotello, AutoHDR và Autoenhance. Mỗi dịch vụ có phiên đăng nhập, cấu hình xử lý và danh sách dự án riêng.",
    position: "bottom",
  },
  {
    key: "service-login",
    selector: "#btn-connect",
    title: "Đăng nhập dịch vụ",
    intro: "Mở Chrome để đăng nhập dịch vụ đang chọn. Ứng dụng sẽ lưu phiên để lần sau tự kết nối lại.",
    position: "bottom",
  },
  {
    key: "upload-tab",
    selector: "#tab-upload",
    title: "Upload và xử lý ảnh",
    intro: "Khu vực này dùng để chọn ảnh mới, cấu hình dự án và gửi xử lý HDR.",
    position: "right",
  },
  {
    key: "download-tab",
    selector: "#tab-download",
    title: "Tải dự án đã có",
    intro: "Xem lại các listing đã xử lý, xem trước ảnh hoặc tải một hay nhiều dự án về máy.",
    position: "right",
  },
  {
    key: "upload-images",
    selector: ".upload-dropzone",
    title: "Chọn ảnh đầu vào",
    intro: "Bấm để chọn nhiều ảnh hoặc kéo thả ảnh vào đây. Ứng dụng hỗ trợ JPG và các định dạng RAW phổ biến.",
    position: "left",
  },
  {
    key: "refresh-listings",
    selector: "#btn-refresh-listings",
    title: "Làm mới danh sách",
    intro: "Bỏ qua dữ liệu cache và tải lại danh sách dự án mới nhất từ dịch vụ đang chọn.",
    position: "bottom",
  },
  {
    key: "listing-preview",
    selector: ".listing-name",
    title: "Xem ảnh trong dự án",
    intro: "Bấm tên listing để mở toàn bộ ảnh, xem trước và chọn riêng những ảnh cần tải.",
    position: "top",
  },
  {
    key: "select-listings",
    selector: ".listing-cb, #select-all-cb",
    title: "Chọn nhiều listing",
    intro: "Tick một hoặc nhiều listing để tải hàng loạt trong cùng một lần.",
    position: "right",
  },
  {
    key: "batch-download",
    selector: "#btn-start-download",
    title: "Bắt đầu tải",
    intro: "Tải tất cả listing đã tick. Mỗi dự án sẽ có tiến trình riêng trong khu vực trạng thái.",
    position: "top",
  },
  {
    key: "stop-task",
    selector: "#btn-stop-upload, #btn-stop-download",
    title: "Dừng tác vụ",
    intro: "Yêu cầu dừng tác vụ đang chạy. Các file đã hoàn thành trước đó vẫn được giữ lại.",
    position: "top",
  },
  {
    key: "wizard-destination",
    selector: ".btn-link-edit",
    title: "Đổi nơi lưu kết quả",
    intro: "Chọn thư mục cha; ứng dụng sẽ tự tạo thư mục con theo tên dự án.",
    position: "bottom",
  },
  {
    key: "wizard-model",
    selector: ".wizard-footer-left",
    title: "Chọn model xử lý",
    intro: "Chọn phong cách hoặc model AI phù hợp cho Fotello hay AutoHDR.",
    position: "top",
  },
  {
    key: "wizard-advanced",
    selector: ".btn-icon-gear",
    title: "Tùy chọn nâng cao",
    intro: "Tinh chỉnh thay trời, phối cảnh, dọn vật thể và các thiết lập riêng của dịch vụ.",
    position: "left",
  },
  {
    key: "wizard-continue",
    selector: ".btn-wizard-continue",
    title: "Phân tích và tiếp tục",
    intro: "Xác nhận cấu hình, phân tích nhóm ảnh và chuyển sang bước kiểm tra trước khi upload.",
    position: "left",
  },
  {
    key: "bracket-mode",
    selector: "#btn-reanalyze-brackets",
    title: "Cấu hình nhóm ảnh",
    intro: "Chọn cách gom nhóm, số ảnh mỗi nhóm và kiểu hiển thị; bấm Áp dụng lại để phân tích lại.",
    position: "bottom",
  },
  {
    key: "bracket-edit",
    selector: ".bracket-item, .bracket-delete-btn, #bracket-review-modal .modal-footer .btn-secondary",
    title: "Chỉnh nhóm ảnh",
    intro: "Kéo thả ảnh giữa các nhóm, thêm nhóm mới hoặc xóa nhóm không cần thiết trước khi upload.",
    position: "top",
  },
  {
    key: "bracket-confirm",
    selector: "#bracket-review-modal .modal-footer .btn-primary",
    title: "Xác nhận và upload",
    intro: "Gửi các nhóm đã kiểm tra lên dịch vụ và bắt đầu xử lý HDR.",
    position: "top",
  },
  {
    key: "gallery-select",
    selector: ".gallery-item, #listing-images-modal .modal-footer .btn-secondary",
    title: "Chọn ảnh cần tải",
    intro: "Chọn từng ảnh hoặc dùng Chọn tất cả/Bỏ chọn để điều chỉnh nhanh danh sách tải.",
    position: "bottom",
  },
  {
    key: "gallery-download",
    selector: "#btn-download-selected-images",
    title: "Tải ảnh đã chọn",
    intro: "Chọn thư mục lưu và chỉ tải những ảnh đang được đánh dấu trong dự án.",
    position: "top",
  },
  {
    key: "job-logs",
    selector: ".log-tab, #btn-copy-log",
    title: "Theo dõi tiến trình",
    intro: "Mỗi tác vụ có một tab log riêng. Bạn có thể chuyển tab hoặc sao chép log khi cần hỗ trợ.",
    position: "top",
  },
];

function readSeenFeatureGuides() {
  const persisted = persistentGuideState.features || {};
  try {
    return {
      ...JSON.parse(localStorage.getItem(FEATURE_GUIDES_STORAGE_KEY) || "{}"),
      ...persisted,
    };
  } catch (error) {
    return { ...persisted };
  }
}

async function loadPersistentGuideState() {
  if (persistentGuideStateLoaded) return persistentGuideState;
  try {
    if (window.pywebview?.api?.get_guide_state) {
      const state = await window.pywebview.api.get_guide_state();
      if (state && typeof state === "object") {
        persistentGuideState = {
          onboarding: state.onboarding || "",
          features: state.features || {},
        };
      }
    }
  } catch (error) {
    console.warn("Không tải được trạng thái hướng dẫn:", error);
  }

  // Migrate state saved by older builds whose localhost port could change.
  try {
    const localOnboarding = localStorage.getItem(ONBOARDING_STORAGE_KEY) || "";
    const localFeatures = JSON.parse(
      localStorage.getItem(FEATURE_GUIDES_STORAGE_KEY) || "{}",
    );
    if (!persistentGuideState.onboarding && localOnboarding) {
      persistentGuideState.onboarding = localOnboarding;
      await window.pywebview?.api?.set_onboarding_state?.(localOnboarding);
    }
    const missingFeatureKeys = Object.keys(localFeatures).filter(
      (key) => localFeatures[key] && !persistentGuideState.features[key],
    );
    if (missingFeatureKeys.length > 0) {
      missingFeatureKeys.forEach((key) => {
        persistentGuideState.features[key] = true;
      });
      await window.pywebview?.api?.mark_all_feature_guides_seen?.(missingFeatureKeys);
    }
  } catch (error) {
    console.warn("Không di chuyển được trạng thái hướng dẫn cũ:", error);
  }

  persistentGuideStateLoaded = true;
  return persistentGuideState;
}

function markFeatureGuideSeen(key) {
  const seen = readSeenFeatureGuides();
  seen[key] = true;
  persistentGuideState.features[key] = true;
  try {
    localStorage.setItem(FEATURE_GUIDES_STORAGE_KEY, JSON.stringify(seen));
  } catch (error) {
    console.warn("Không lưu được trạng thái hướng dẫn:", error);
  }
  window.pywebview?.api?.mark_feature_guide_seen?.(key).catch((error) => {
    console.warn("Không lưu được hướng dẫn vào máy:", error);
  });
}

function markAllFeatureGuidesSeen() {
  const seen = {};
  featureGuideDefinitions.forEach((guide) => {
    seen[guide.key] = true;
  });
  seen[WIZARD_CONTEXT_GUIDE_KEY] = true;
  seen[UPSCALE_WIZARD_GUIDE_KEY] = true;
  seen[UPSCALE_MAIN_GUIDE_KEY] = true;
  seen[BRACKET_CONTEXT_GUIDE_KEY] = true;
  try {
    localStorage.setItem(FEATURE_GUIDES_STORAGE_KEY, JSON.stringify(seen));
  } catch (error) {
    console.warn("Không lưu được trạng thái tour:", error);
  }
  persistentGuideState.features = { ...persistentGuideState.features, ...seen };
  window.pywebview?.api
    ?.mark_all_feature_guides_seen?.(Object.keys(seen))
    .catch((error) => console.warn("Không lưu được trạng thái tour vào máy:", error));
}

function saveOnboardingState(state) {
  persistentGuideState.onboarding = state;
  try {
    localStorage.setItem(ONBOARDING_STORAGE_KEY, state);
  } catch (error) {
    console.warn("Không lưu được trạng thái onboarding:", error);
  }
  window.pywebview?.api?.set_onboarding_state?.(state).catch((error) => {
    console.warn("Không lưu được onboarding vào máy:", error);
  });
}

async function maybeOfferFirstTour(auth) {
  if (onboardingPromptScheduled) return;
  onboardingPromptScheduled = true;
  
  await loadPersistentGuideState();
  if (persistentGuideState.onboarding) return;
  try {
    if (localStorage.getItem(ONBOARDING_STORAGE_KEY)) return;
  } catch (error) {
    return;
  }

  setTimeout(() => {
    document.getElementById("onboarding-welcome-modal")?.classList.remove("hidden");
  }, 700);
}

function completeOnboardingChoice(wantsTour) {
  document.getElementById("onboarding-welcome-modal")?.classList.add("hidden");
  saveOnboardingState(wantsTour ? "accepted" : "declined");
  if (wantsTour) {
    setTimeout(() => document.getElementById("btn-tour")?.click(), 180);
  }
}

function scheduleWizardContextTour() {
  setTimeout(() => {
    if (
      featureGuideInProgress ||
      typeof introJs === "undefined" ||
      document.querySelector(".introjs-overlay")
    ) {
      return;
    }

    const modal = document.getElementById("project-wizard-modal");
    const seen = readSeenFeatureGuides();
    const guideKey = wizardContext === "upscale"
      ? UPSCALE_WIZARD_GUIDE_KEY
      : WIZARD_CONTEXT_GUIDE_KEY;
    if (!modal || modal.classList.contains("hidden") || seen[guideKey]) {
      return;
    }

    const stepDefinitions = wizardContext === "upscale"
      ? [
          {
            selector: "#project-wizard-modal .modal-card",
            title: "Tạo dự án Upscale",
            intro: "Đặt tên và chọn nơi lưu cho toàn bộ ảnh kết quả trong lần xử lý này.",
            position: "bottom",
          },
          {
            selector: "#wizard-project-name",
            title: "Đặt tên dự án",
            intro: "Tên này sẽ trở thành thư mục chứa ảnh làm nét giữ size.",
            position: "bottom",
          },
          {
            selector: ".wizard-dest-row",
            title: "Nơi lưu kết quả",
            intro: "Bấm Thay đổi để chọn thư mục cha khác. Tool sẽ tự tạo thư mục dự án bên trong.",
            position: "bottom",
          },
          {
            selector: ".wizard-footer-left",
            title: "Thiết lập đang dùng",
            intro: "Hiển thị preset làm nét giữ size đang dùng cho lần xử lý này.",
            position: "top",
          },
          {
            selector: ".btn-wizard-continue",
            title: "Bắt đầu xử lý",
            intro: "Bấm Tiếp tục để tạo thư mục dự án và đưa ảnh vào hàng đợi xử lý.",
            position: "top",
          },
        ]
      : [
          {
            selector: "#project-wizard-modal .modal-card",
            title: "Thiết lập dự án",
            intro: "Đây là bước chuẩn bị trước khi xử lý. Bạn sẽ đặt tên, chọn nơi lưu và cấu hình model cho dự án.",
            position: "bottom",
          },
          {
            selector: "#wizard-project-name",
            title: "Đặt tên dự án",
            intro: "Nhập tên dễ nhận biết. Tên này cũng được dùng để tạo thư mục lưu kết quả.",
            position: "bottom",
          },
          {
            selector: ".wizard-dest-row",
            title: "Nơi lưu kết quả",
            intro: "Đường dẫn cập nhật theo tên dự án. Bấm Thay đổi nếu bạn muốn chọn thư mục cha khác.",
            position: "bottom",
          },
          {
            selector: ".wizard-footer-left",
            title: "Model xử lý",
            intro: "Bấm vào tên model để chọn phong cách xử lý phù hợp với dịch vụ hiện tại.",
            position: "top",
          },
          {
            selector: ".btn-icon-gear",
            title: "Tùy chọn nâng cao",
            intro: "Mở các thiết lập như thay trời, chỉnh phối cảnh, dọn vật thể và những tùy chọn chuyên sâu khác.",
            position: "top",
          },
          {
            selector: ".btn-wizard-continue",
            title: "Tiếp tục xử lý",
            intro: "Sau khi hoàn tất thiết lập, bấm Tiếp tục để phân tích và kiểm tra nhóm ảnh trước khi upload.",
            position: "top",
          },
        ];

    const steps = stepDefinitions
      .map((step) => ({ ...step, element: document.querySelector(step.selector) }))
      .filter((step) => step.element);
    if (steps.length === 0) return;

    featureGuideInProgress = true;
    markFeatureGuideSeen(guideKey);
    const featureKeys = wizardContext === "upscale"
      ? ["upscale-wizard-destination", "upscale-wizard-summary", "upscale-wizard-continue"]
      : ["wizard-destination", "wizard-model", "wizard-advanced", "wizard-continue"];
    featureKeys.forEach(
      markFeatureGuideSeen,
    );

    const intro = introJs();
    let finished = false;
    const finish = () => {
      if (finished) return;
      finished = true;
      featureGuideInProgress = false;
      document.getElementById("wizard-project-name")?.focus();
    };
    intro.setOptions({
      nextLabel: "Tiếp tục",
      prevLabel: "Quay lại",
      doneLabel: "Bắt đầu nhập",
      showProgress: true,
      showBullets: false,
      exitOnOverlayClick: false,
      tooltipClass: "custom-intro-tooltip",
      steps,
    });
    intro.oncomplete(finish);
    intro.onexit(finish);
    intro.start();
  }, 250);
}

function scheduleBracketContextTour() {
  setTimeout(() => {
    if (
      featureGuideInProgress ||
      typeof introJs === "undefined" ||
      document.querySelector(".introjs-overlay")
    ) {
      return;
    }

    const modal = document.getElementById("bracket-review-modal");
    const seen = readSeenFeatureGuides();
    if (!modal || modal.classList.contains("hidden") || seen[BRACKET_CONTEXT_GUIDE_KEY]) {
      return;
    }

    const stepDefinitions = [
      {
        selector: "#bracket-review-modal .modal-card",
        title: "Kiểm tra nhóm ảnh",
        intro: "Ảnh đã được gom theo thứ tự và số lượng bạn chọn. Hãy kiểm tra lại các nhóm trước khi upload.",
        position: "bottom",
      },
      {
        selector: ".bracket-mode-bar",
        title: "Cách gom và hiển thị",
        intro: "Chọn logic gom nhóm, số ảnh trong mỗi nhóm và cách trình bày. Bấm Áp dụng lại khi thay đổi cấu hình.",
        position: "bottom",
      },
      {
        selector: "#bracket-container",
        title: "Sắp xếp ảnh bằng kéo thả",
        intro: "Kéo ảnh để đổi vị trí hoặc chuyển sang nhóm khác. Dấu x ở góc nhóm dùng để xóa nhóm không cần thiết.",
        position: "top",
      },
      {
        selector: "#btn-add-bracket-group",
        title: "Thêm nhóm thủ công",
        intro: "Tạo một nhóm trống mới khi bạn muốn tách ảnh hoặc tự sắp xếp lại kết quả gom nhóm.",
        position: "top",
      },
      {
        selector: "#btn-confirm-bracket-upload",
        title: "Xác nhận và upload",
        intro: "Khi các nhóm đã đúng, bấm nút này để gửi ảnh lên dịch vụ và bắt đầu xử lý HDR.",
        position: "top",
      },
    ];

    const steps = stepDefinitions
      .map((step) => ({ ...step, element: document.querySelector(step.selector) }))
      .filter((step) => step.element);
    if (steps.length === 0) return;

    featureGuideInProgress = true;
    markFeatureGuideSeen(BRACKET_CONTEXT_GUIDE_KEY);
    ["bracket-mode", "bracket-edit", "bracket-confirm"].forEach(markFeatureGuideSeen);

    const intro = introJs();
    let finished = false;
    const finish = () => {
      if (finished) return;
      finished = true;
      featureGuideInProgress = false;
    };
    intro.setOptions({
      nextLabel: "Tiếp tục",
      prevLabel: "Quay lại",
      doneLabel: "Đã hiểu",
      showProgress: true,
      showBullets: false,
      exitOnOverlayClick: false,
      tooltipClass: "custom-intro-tooltip",
      steps,
    });
    intro.oncomplete(finish);
    intro.onexit(finish);
    intro.start();
  }, 350);
}

function startContextualFeatureGuide(guide, element, resumeAction) {
  if (typeof introJs === "undefined") {
    resumeAction();
    return;
  }

  featureGuideInProgress = true;
  markFeatureGuideSeen(guide.key);
  const intro = introJs();
  let resumed = false;
  const finish = () => {
    if (resumed) return;
    resumed = true;
    featureGuideInProgress = false;
    setTimeout(resumeAction, 0);
  };

  intro.setOptions({
    nextLabel: "Đã hiểu",
    doneLabel: "Bắt đầu",
    showProgress: false,
    showBullets: false,
    exitOnOverlayClick: false,
    tooltipClass: "custom-intro-tooltip",
    steps: [{
      element,
      title: guide.title,
      intro: guide.intro,
      position: guide.position || "auto",
    }],
  });
  intro.oncomplete(finish);
  intro.onexit(finish);
  intro.start();
}

document.addEventListener("click", (event) => {
  if (
    featureGuideInProgress ||
    typeof introJs === "undefined" ||
    document.querySelector(".introjs-overlay")
  ) {
    return;
  }

  const seen = readSeenFeatureGuides();
  const guide = featureGuideDefinitions.find((item) => {
    if (seen[item.key]) return false;
    return event.target.closest(item.selector);
  });
  if (!guide) return;

  const element = event.target.closest(guide.selector);
  if (!element || element.disabled) return;
  event.preventDefault();
  event.stopPropagation();
  event.stopImmediatePropagation();
  startContextualFeatureGuide(guide, element, () => element.click());
}, true);

function startAppTour() {
  if (typeof introJs !== 'undefined') {
    const intro = introJs();
    
    intro.setOptions({
      nextLabel: 'Tiếp tục',
      prevLabel: 'Quay lại',
      doneLabel: 'Hoàn tất',
      showProgress: true,
      showBullets: true,
      exitOnOverlayClick: false,
      tooltipClass: 'custom-intro-tooltip',
      steps: [
        {
          element: document.querySelector('.service-selector'),
          title: 'Chọn Dịch Vụ',
          intro: 'Chuyển đổi giữa 2 dịch vụ: Fotello và AutoHDR.',
          position: 'bottom'
        },
        {
          element: document.querySelector('#btn-connect'),
          title: 'Đăng Nhập Tài Khoản',
          intro: 'Đừng quên bấm vào đây để Đăng nhập vào tài khoản Fotello hoặc AutoHDR tương ứng trước khi sử dụng nhé!',
          position: 'bottom'
        },
        {
          element: document.querySelector('#tab-upload'),
          title: 'Upload Ảnh',
          intro: 'Tại tab này, bạn có thể tải ảnh lên để hệ thống tự động xử lý.',
          position: 'right'
        },
        {
          element: document.querySelector('#tab-download'),
          title: 'Tải Listings Cũ',
          intro: 'Bạn đã tắt app giữa chừng? Bấm vào đây để tải về hoặc xử lý tiếp các listing đã tải lên trước đó.',
          position: 'right'
        },
        {
          element: document.querySelector('.listings-table-container'),
          title: 'Danh Sách Dự Án',
          intro: 'Các ảnh bạn đã upload lên qua phần mềm hoặc trên web sẽ nằm hết ở đây. Bạn có thể click vào tên của dự án để xem trước toàn bộ hình ảnh chi tiết.',
          position: 'top'
        },
        {
          element: '#listing-images-modal .modal-card',
          title: 'Xem & Chọn Ảnh Chi Tiết',
          intro: 'Bảng xem chi tiết sẽ hiển thị toàn bộ hình ảnh của dự án. Bạn có thể Click vào từng tấm hình cụ thể để chọn riêng lẻ.',
          position: 'bottom'
        },
        {
          element: '#btn-download-selected-images',
          title: 'Tải Ảnh Đã Chọn',
          intro: 'Sau khi chọn một hoặc nhiều ảnh, hãy bấm nút này để chỉ tải riêng các tấm hình đã chọn về máy của bạn.',
          position: 'top'
        },
        {
          element: document.querySelector('#btn-start-download'),
          title: 'Tải Nhiều Dự Án Cùng Lúc',
          intro: 'Bạn có thể tick chọn nhiều listings cùng lúc và bấm nút này để tải về đồng thời nhiều jobs song song, giúp tiết kiệm thời gian tối đa!',
          position: 'top'
        },
        {
          element: document.querySelector('.upload-dropzone-container'),
          title: 'Khu vực Upload',
          intro: 'Nhấn hoặc Kéo thả ảnh (JPG, RAW...) vào đây để bắt đầu xử lý. Ngay sau khi chọn ảnh, hệ thống sẽ mở bảng Thiết lập dự án.',
          position: 'left'
        },
        {
          element: document.querySelector('#project-wizard-modal .modal-card'),
          title: 'Bảng Thiết Lập Dự Án',
          intro: 'Ngay sau khi chọn ảnh xong, bảng cấu hình này sẽ tự động hiện lên để bạn thiết lập dự án trước khi chạy.',
          position: 'bottom'
        },
        {
          element: document.querySelector('#wizard-project-name'),
          title: 'Tên Dự Án',
          intro: 'Bạn bắt buộc phải nhập tên cho dự án để tiếp tục (hệ thống sẽ không tự sinh tên mặc định nữa).',
          position: 'bottom'
        },
        {
          element: document.querySelector('.wizard-dest-row'),
          title: 'Thư mục Lưu Trữ',
          intro: 'Hệ thống sẽ tự động tạo một thư mục con cùng tên dự án. Bạn có thể bấm "Thay đổi" để chọn đường dẫn khác.',
          position: 'bottom'
        },
        {
          element: document.querySelector('.wizard-footer-left-wrapper'),
          title: 'Chọn AI Model',
          intro: 'Bạn có thể chuyển đổi giữa các phong cách xử lý khác nhau (ví dụ đối với fotello : Signature và Natural còn auto HDR thì là Classic, Classic v4, Lisa ... ).',  
          position: 'top'
        },
        {
          element: document.querySelector('.btn-icon-gear'),
          title: 'Tùy Chỉnh Nâng Cao',
          intro: 'If bạn là người dùng chuyên nghiệp, bấm vào icon này để tinh chỉnh Độ sáng, Tương phản, Cân bằng trắng...',
          position: 'left'
        },
        {
          element: document.querySelector('.btn-wizard-continue'),
          title: 'Bắt Đầu Xử Lý',
          intro: 'Khi mọi thiết lập đã sẵn sàng, bấm nút này. Bạn hoàn toàn có thể tạo và chạy nhiều jobs liên tiếp để hệ thống tự động upload và xử lý song song nhiều dự án cùng lúc!',
          position: 'left'
        },
        {
          element: document.querySelector('#bracket-review-modal .modal-card'),
          title: 'Kiểm Tra Nhóm Ảnh',
          intro: 'Bảng gom nhóm này chỉ dành riêng cho Fotello (khi bạn cần ghép nhiều ảnh lại với nhau). Bạn có thể kiểm tra lại các nhóm ảnh tại đây trước khi bắt đầu xử lý.',
          position: 'bottom'
        },
        {
          element: document.querySelector('.bracket-mode-bar'),
          title: 'Cấu Hình Gom Nhóm',
          intro: 'Tại đây, bạn có thể đổi Logic gom nhóm (Theo thời gian chụp / Số lượng cố định) và Kiểu hiển thị. Nhấn Áp dụng lại để hệ thống tự động quét lại.',
          position: 'bottom'
        },
        {
          element: document.querySelector('#bracket-container'),
          title: 'Khu Vực Kéo & Thả',
          intro: 'Danh sách các nhóm sẽ hiện ở đây. If hệ thống gom nhầm, bạn có thể Kéo và Thả tên từng ảnh để chuyển ảnh sang nhóm khác rất dễ dàng.',
          position: 'top'
        },
        {
          element: document.querySelector('#bracket-review-modal .modal-footer'),
          title: 'Hoàn Tất Upload',
          intro: 'Bạn cũng có thể thêm nhóm mới thủ công. Sau khi vừa ý, bấm Xác nhận & Upload để siêu AI bắt đầu làm việc. Chúc bạn trải nghiệm phần mềm vui vẻ!',
          position: 'top'
        },
        {
          element: document.querySelector('.console-box'),
          title: 'Trạng thái Jobs (Console)',
          intro: 'Đây sẽ là nơi cho bạn thấy thông tin trạng thái của các jobs bạn đăng tải lên. Bạn có thể ấn vào từng tab dự án để xem quá trình xử lý chi tiết. Khi tải xong, các tab sẽ hiện hình icon Thư mục (📁), bạn có thể click vào đấy để kiểm tra hình đã xử lý cực kỳ nhanh chóng!',
          position: 'top'
        }
      ]
    });
 
    intro.onbeforechange(function() {
      // Step index 3 to 7 are download related
      if (this._currentStep >= 3 && this._currentStep <= 7) {
        switchTab('download');
        if (!listingsData.some(l => l.isFake)) {
          listingsData = getFakeListings(currentService);
          renderListings();
        }
      } else {
        switchTab('upload');
      }
 
      // Handle listing images modal during tour (Step 5 and 6)
      const imagesModal = document.getElementById('listing-images-modal');
      if (this._currentStep === 5 || this._currentStep === 6) {
        if (imagesModal) {
          imagesModal.classList.remove('hidden');
          const grid = document.getElementById('listing-images-grid');
          if (grid && grid.innerHTML.trim() === '') {
            const fakePhotos = [
              { id: 9001, name: 'living_room.jpg', url: 'https://images.unsplash.com/photo-1586023492125-27b2c045efd7?auto=format&fit=crop&w=300&q=80' },
              { id: 9002, name: 'kitchen.jpg', url: 'https://images.unsplash.com/photo-1556912172-45b7abe8b7e1?auto=format&fit=crop&w=300&q=80' },
              { id: 9003, name: 'bedroom.jpg', url: 'https://images.unsplash.com/photo-1505691938895-1758d7feb511?auto=format&fit=crop&w=300&q=80' },
              { id: 9004, name: 'bathroom.jpg', url: 'https://images.unsplash.com/photo-1584622650111-993a426fbf0a?auto=format&fit=crop&w=300&q=80' }
            ];
            currentListingPhotos = fakePhotos;
            grid.innerHTML = fakePhotos.map((p, index) => `
              <div class="gallery-item" onclick="toggleSelectPhoto(this, ${index})">
                <div class="gallery-select-badge">✓</div>
                <img src="${p.url}" loading="lazy" decoding="async">
                <div class="gallery-caption" title="${p.name}">${p.name}</div>
              </div>
            `).join('');
            
            // Auto select the first photo for demonstration
            setTimeout(() => {
              const firstItem = grid.querySelector('.gallery-item');
              if (firstItem) {
                toggleSelectPhoto(firstItem, 0);
              }
            }, 100);
          }
        }
      } else {
        if (imagesModal && !imagesModal.classList.contains('hidden') && this._currentStep !== 6) {
          imagesModal.classList.add('hidden');
        }
      }
 
      // Step index 10 to 15 is Wizard
      // Step index 16 to 19 is Bracket Modal
      const bracketModal = document.getElementById('bracket-review-modal');
      const container = document.getElementById('bracket-container');
      const statusLabel = document.getElementById('bracket-modal-status');
      
      if (this._currentStep >= 9 && this._currentStep <= 14) {
        if (bracketModal) bracketModal.classList.add('hidden');
        openWizardModal('Dự án Demo', '');
      } else if (this._currentStep >= 15 && this._currentStep <= 18) {
        closeWizardModal();
        if (bracketModal) bracketModal.classList.remove('hidden');
        if (container && container.innerHTML.trim() === '') {
          applyBracketViewMode('filmstrip');
          container.innerHTML = `
            <div class="bracket-group" data-active-index="0" style="border: 1px dashed var(--border); margin-bottom: 16px;">
              <div class="bracket-header" style="display:flex; justify-content:space-between; align-items:center; padding: 12px 16px; background: rgba(255,255,255,0.03);">
                <div class="bracket-title" style="color:var(--primary); font-weight:bold;">Nhóm Góc Chụp 1 (2 ảnh)</div>
              </div>
              <div class="bracket-list" style="padding: 12px;">
                <div class="bracket-item"><img class="bracket-thumb" src="https://cdn.fink.io.vn/3W1A4501.JPG" style="width:100%; height:100%; min-height:80px; border-radius:4px; object-fit:cover;"><span class="bracket-filename">3W1A4501.JPG</span></div>
                <div class="bracket-item"><img class="bracket-thumb" src="https://cdn.fink.io.vn/3W1A4502.JPG" style="width:100%; height:100%; min-height:80px; border-radius:4px; object-fit:cover;"><span class="bracket-filename">3W1A4502.JPG</span></div>
              </div>
            </div>
            
            <div class="bracket-group" data-active-index="0" style="border: 1px dashed var(--border); margin-bottom: 16px;">
              <div class="bracket-header" style="display:flex; justify-content:space-between; align-items:center; padding: 12px 16px; background: rgba(255,255,255,0.03);">
                <div class="bracket-title" style="color:var(--primary); font-weight:bold;">Nhóm Góc Chụp 2 (2 ảnh)</div>
              </div>
              <div class="bracket-list" style="padding: 12px;">
                <div class="bracket-item"><img class="bracket-thumb" src="https://cdn.fink.io.vn/3W1A4503.JPG" style="width:100%; height:100%; min-height:80px; border-radius:4px; object-fit:cover;"><span class="bracket-filename">3W1A4503.JPG</span></div>
                <div class="bracket-item"><img class="bracket-thumb" src="https://cdn.fink.io.vn/3W1A4504.JPG" style="width:100%; height:100%; min-height:80px; border-radius:4px; object-fit:cover;"><span class="bracket-filename">3W1A4504.JPG</span></div>
              </div>
            </div>
          `;
          if (statusLabel) statusLabel.innerText = 'Tổng cộng: 2 nhóm (Sample)';
        }
      } else {
        closeWizardModal();
        if (bracketModal) bracketModal.classList.add('hidden');
        if (container) container.innerHTML = '';
        if (statusLabel) statusLabel.innerText = 'Tổng cộng: 0 nhóm';
      }
 
      // Step index 20 is Console Box
      if (this._currentStep === 19) {
        if (!document.getElementById('tab-tourfake_1')) {
          addLog("Đang xử lý ảnh bầu trời...", "info", "tourfake_1", "Fotello: 123 Main St");
          addLog("Đang blend HDR...", "info", "tourfake_2", "AutoHDR: 456 Oak Ave");
          addLog("Đang xuất file JPG...", "info", "tourfake_3", "Fotello: 789 Pine Rd");
          addLog("Hoàn tất xử lý!", "success", "tourfake_4", "AutoHDR: 101 Maple Blvd");
          
          window.showFolderBtn("tourfake_1", "/Users/fake/123_Main_St");
          window.showFolderBtn("tourfake_2", "/Users/fake/456_Oak_Ave");
          window.showFolderBtn("tourfake_3", "/Users/fake/789_Pine_Rd");
          window.showFolderBtn("tourfake_4", "/Users/fake/101_Maple_Blvd");
        }
      } else {
        ['tourfake_1', 'tourfake_2', 'tourfake_3', 'tourfake_4'].forEach(id => {
          const tab = document.getElementById(`tab-${id}`);
          const output = document.getElementById(`console-output-${id}`);
          if (tab) tab.remove();
          if (output) output.remove();
        });
        switchLogTab("global");
      }
    });
 
    intro.oncomplete(function() {
      saveOnboardingState("completed");
      markAllFeatureGuidesSeen();
      closeWizardModal();
      const imagesModal = document.getElementById('listing-images-modal');
      if (imagesModal) imagesModal.classList.add('hidden');
      const bracketModal = document.getElementById('bracket-review-modal');
      if (bracketModal) bracketModal.classList.add('hidden');
      const container = document.getElementById('bracket-container');
      if (container) container.innerHTML = '';
      const statusLabel = document.getElementById('bracket-modal-status');
      if (statusLabel) statusLabel.innerText = 'Tổng cộng: 0 nhóm';
 
      ['tourfake_1', 'tourfake_2', 'tourfake_3', 'tourfake_4'].forEach(id => {
        const tab = document.getElementById(`tab-${id}`);
        const output = document.getElementById(`console-output-${id}`);
        if (tab) tab.remove();
        if (output) output.remove();
      });
      switchLogTab("global");
      
      if (listingsData.some(l => l.isFake)) {
        listingsData = [];
        renderListings();
      }
    });
 
    intro.onexit(function() {
      closeWizardModal();
      const imagesModal = document.getElementById('listing-images-modal');
      if (imagesModal) imagesModal.classList.add('hidden');
      const bracketModal = document.getElementById('bracket-review-modal');
      if (bracketModal) bracketModal.classList.add('hidden');
      const container = document.getElementById('bracket-container');
      if (container) container.innerHTML = '';
      const statusLabel = document.getElementById('bracket-modal-status');
      if (statusLabel) statusLabel.innerText = 'Tổng cộng: 0 nhóm';
 
      ['tourfake_1', 'tourfake_2', 'tourfake_3', 'tourfake_4'].forEach(id => {
        const tab = document.getElementById(`tab-${id}`);
        const output = document.getElementById(`console-output-${id}`);
        if (tab) tab.remove();
        if (output) output.remove();
      });
      switchLogTab("global");
      
      if (listingsData.some(l => l.isFake)) {
        listingsData = [];
        renderListings();
      }
    });

    intro.start();
  } else {
    addLog("Intro.js hasn't loaded yet.", "error");
  }
}

// === DRAG & DROP ===
let nativeDropHandledAt = 0;
let lastDropSignature = '';
let lastDropHandledAt = 0;

async function handleDroppedPaths(paths) {
  if (!Array.isArray(paths) || paths.length === 0) return;

  const signature = paths.join('\n');
  const now = Date.now();
  if (signature === lastDropSignature && now - lastDropHandledAt < 1000) return;
  lastDropSignature = signature;
  lastDropHandledAt = now;

  const service = currentService;
  if (!serviceLoginState[service]) {
    addLog(`⚠️ Bạn chưa đăng nhập ${serviceDisplayName(service)}. Đang tự động mở trình duyệt đăng nhập...`, "warn");
    toggleServiceLogin();
    return;
  }

  selectedInputFiles = paths;
  selectedInputBaseDir = pathDirname(paths[0]);

  const baseName = pathWithoutExtension(paths[0]);
  const defaultProjName = baseName ? sanitizeFolderName(baseName) : "DuAn_" + new Date().toISOString().slice(0, 10).replace(/-/g, "");
  openWizardModal(defaultProjName, selectedInputBaseDir);
}

async function handleUpscaleDroppedPaths(paths) {
  if (!Array.isArray(paths) || paths.length === 0) return;

  const signature = `upscale\n${paths.join('\n')}`;
  const now = Date.now();
  if (signature === lastDropSignature && now - lastDropHandledAt < 1000) return;
  lastDropSignature = signature;
  lastDropHandledAt = now;

  switchTab("upscale");
  openUpscaleWizard(paths);
}

function handleNativeDroppedFiles(paths) {
  nativeDropHandledAt = Date.now();
  return handleDroppedPaths(paths);
}

function handleNativeUpscaleDroppedFiles(paths) {
  nativeDropHandledAt = Date.now();
  return handleUpscaleDroppedPaths(paths);
}

function handleNativeDteDroppedFiles(paths) {
  nativeDropHandledAt = Date.now();
  if (!paths || !paths.length) return;
  const IMAGE_EXTS = /\.(jpe?g|png|webp|tiff?|heic|heif)$/i;
  const filtered = paths.filter(p => IMAGE_EXTS.test(p));
  if (!filtered.length) return;
  selectedDteFiles = [...new Set([...selectedDteFiles, ...filtered])];
  updateDteDropzoneList();
}

function handleNativeMergeDroppedFiles(kind, paths) {
  nativeDropHandledAt = Date.now();
  return handleMergeDroppedPaths(kind, paths);
}

window.handleNativeBracketDroppedFiles = function (groupId, paths) {
  nativeDropHandledAt = Date.now();
  const cleanId = String(groupId || "").replace("#", "");
  const list = document.getElementById(cleanId);
  if (!list) return;

  const files = getSupportedMergeFiles(paths);
  if (!files.length) return;

  (async () => {
    for (const path of files) {
      const exists = Array.from(list.querySelectorAll(".bracket-item")).some(
        (item) => item.dataset.img === path
      );
      if (exists) continue;

      const { item, imgEl } = createBracketItemElement(path);
      list.appendChild(item);

      try {
        const thumbData = await window.pywebview.api.get_thumbnail_base64(path);
        if (thumbData) {
          imgEl.src = thumbData;
        }
      } catch (err) {}
    }
    updateBracketCounts();
  })();
};

const dropzone = document.querySelector('.upload-dropzone');
if (dropzone) {
  dropzone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropzone.style.borderColor = '#f472b6';
    dropzone.style.background = 'rgba(236, 72, 153, 0.05)';
  });

  dropzone.addEventListener('dragleave', (e) => {
    e.preventDefault();
    dropzone.style.borderColor = '';
    dropzone.style.background = '';
  });

  dropzone.addEventListener('drop', async (e) => {
    e.preventDefault();
    dropzone.style.borderColor = '';
    dropzone.style.background = '';

    if (!e.dataTransfer.files || e.dataTransfer.files.length === 0) return;

    const files = Array.from(e.dataTransfer.files);
    const paths = [];
    let missingPath = false;

    for (let f of files) {
      if (f.path) {
        paths.push(f.path);
      } else {
        missingPath = true;
      }
    }

    if (paths.length > 0) {
      await handleDroppedPaths(paths);
    } else if (missingPath) {
      const dropStartedAt = Date.now();
      setTimeout(() => {
        if (nativeDropHandledAt < dropStartedAt) {
          addLog("⚠ Không lấy được đường dẫn tệp khi kéo thả. Vui lòng bấm vào khung để chọn ảnh.", "warn");
        }
      }, 500);
    }
  });
}

document.querySelectorAll(".merge-input-drop").forEach((row) => {
  row.addEventListener("dragover", (e) => {
    e.preventDefault();
    row.classList.add("drag-over");
  });

  row.addEventListener("dragleave", (e) => {
    e.preventDefault();
    row.classList.remove("drag-over");
  });

  row.addEventListener("drop", async (e) => {
    e.preventDefault();
    row.classList.remove("drag-over");

    const kind = row.dataset.mergeKind;
    if (!["original", "ai"].includes(kind)) return;
    if (!e.dataTransfer.files || e.dataTransfer.files.length === 0) return;

    const files = Array.from(e.dataTransfer.files);
    const paths = [];
    let missingPath = false;

    for (let f of files) {
      if (f.path) {
        paths.push(f.path);
      } else {
        missingPath = true;
      }
    }

    if (paths.length > 0) {
      await handleMergeDroppedPaths(kind, paths);
    } else if (missingPath) {
      const dropStartedAt = Date.now();
      setTimeout(() => {
        if (nativeDropHandledAt < dropStartedAt) {
          addLog("⚠ Không lấy được đường dẫn ảnh khi kéo thả. Vui lòng bấm Chọn để chọn thư mục.", "warn");
        }
      }, 500);
    }
  });
});

const upscaleDropzone = document.querySelector('.upscale-dropzone');
if (upscaleDropzone) {
  upscaleDropzone.addEventListener('dragover', (e) => {
    e.preventDefault();
    upscaleDropzone.style.borderColor = '#34c759';
    upscaleDropzone.style.background = 'rgba(52, 199, 89, 0.06)';
  });

  upscaleDropzone.addEventListener('dragleave', (e) => {
    e.preventDefault();
    upscaleDropzone.style.borderColor = '';
    upscaleDropzone.style.background = '';
  });

  upscaleDropzone.addEventListener('drop', async (e) => {
    e.preventDefault();
    upscaleDropzone.style.borderColor = '';
    upscaleDropzone.style.background = '';

    if (!e.dataTransfer.files || e.dataTransfer.files.length === 0) return;

    const files = Array.from(e.dataTransfer.files);
    const paths = [];
    let missingPath = false;

    for (let f of files) {
      if (f.path) {
        paths.push(f.path);
      } else {
        missingPath = true;
      }
    }

    if (paths.length > 0) {
      await handleUpscaleDroppedPaths(paths);
    } else if (missingPath) {
      const dropStartedAt = Date.now();
      setTimeout(() => {
        if (nativeDropHandledAt < dropStartedAt) {
          addLog("⚠ Không lấy được đường dẫn tệp khi kéo thả. Vui lòng bấm vào khung để chọn ảnh.", "warn");
        }
      }, 500);
    }
  });
}

const dteDropzone = document.querySelector('.dte-dropzone');
if (dteDropzone) {
  dteDropzone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dteDropzone.style.borderColor = 'rgba(236, 72, 153, 0.7)';
    dteDropzone.style.background = 'rgba(236, 72, 153, 0.09)';
  });

  dteDropzone.addEventListener('dragleave', (e) => {
    e.preventDefault();
    dteDropzone.style.borderColor = '';
    dteDropzone.style.background = '';
  });

  dteDropzone.addEventListener('drop', async (e) => {
    e.preventDefault();
    dteDropzone.style.borderColor = '';
    dteDropzone.style.background = '';

    if (!e.dataTransfer.files || e.dataTransfer.files.length === 0) return;

    const files = Array.from(e.dataTransfer.files);
    const paths = [];
    let missingPath = false;

    for (let f of files) {
      if (f.path) {
        paths.push(f.path);
      } else {
        missingPath = true;
      }
    }

    if (paths.length > 0) {
      handleNativeDteDroppedFiles(paths);
    } else if (missingPath) {
      const dropStartedAt = Date.now();
      setTimeout(() => {
        if (nativeDropHandledAt < dropStartedAt) {
          addLog("⚠ Không lấy được đường dẫn ảnh khi kéo thả. Vui lòng bấm Chọn để chọn ảnh.", "warn");
        }
      }, 500);
    }
  });
}

// ==========================================
// LOG PANEL RESIZING & COLLAPSING LOGIC
// ==========================================

function toggleLogPanel() {
  const footer = document.getElementById('app-footer');
  if (!footer) return;
  const isCollapsed = footer.classList.contains('collapsed');
  if (isCollapsed) {
    footer.classList.remove('collapsed');
    localStorage.setItem('console_panel_collapsed', 'false');
  } else {
    footer.classList.add('collapsed');
    localStorage.setItem('console_panel_collapsed', 'true');
  }
}
window.toggleLogPanel = toggleLogPanel;

// Setup Drag to Resize
function initLogPanelResizable() {
  const resizer = document.getElementById('footer-resizer');
  const footer = document.getElementById('app-footer');
  if (!resizer || !footer) return;

  // Restore state from LocalStorage
  const savedHeight = localStorage.getItem('console_panel_height');
  if (savedHeight) {
    document.documentElement.style.setProperty('--footer-height', `${savedHeight}px`);
  } else {
    document.documentElement.style.setProperty('--footer-height', '200px');
  }

  const isCollapsed = localStorage.getItem('console_panel_collapsed') === 'true';
  if (isCollapsed) {
    footer.classList.add('collapsed');
  }

  resizer.addEventListener('mousedown', (e) => {
    e.preventDefault();
    const startY = e.clientY;
    const startHeight = footer.offsetHeight;
    
    resizer.classList.add('dragging');

    function onMouseMove(moveEvent) {
      const dy = moveEvent.clientY - startY;
      let newHeight = startHeight - dy;
      
      // Constraints
      const minHeight = 90;
      const maxHeight = window.innerHeight * 0.7; // Max 70% of screen height
      if (newHeight < minHeight) newHeight = minHeight;
      if (newHeight > maxHeight) newHeight = maxHeight;

      document.documentElement.style.setProperty('--footer-height', `${newHeight}px`);
    }

    function onMouseUp() {
      resizer.classList.remove('dragging');
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
      
      // Save height
      if (!footer.classList.contains('collapsed')) {
        const finalHeight = footer.offsetHeight;
        localStorage.setItem('console_panel_height', finalHeight);
      }
    }

    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
  });
}

// Hook it to DomContentLoaded or run instantly
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initLogPanelResizable);
} else {
  initLogPanelResizable();
}

// --- FREE CREDITS GIFT DIALOG CONTROLLER ---
let currentLoggedUid = null;
const FREE_CREDITS_TRIGGER = "1tianhdonemotngay";
let freeCreditsTriggerBuffer = "";

function syncFreeCreditsGiftStatus(status) {
  const userId = status?.user_id;
  if (!userId) {
    currentLoggedUid = null;
    return;
  }
  if (currentLoggedUid === userId) return;
  currentLoggedUid = userId;

  const key = `claimed_credits_${userId}`;
  if (status.claimed_free_credits === true || localStorage.getItem(key) === "true") {
    localStorage.setItem(key, "true");
  }
}

document.addEventListener("keydown", (event) => {
  if (event.isComposing || event.ctrlKey || event.metaKey || event.altKey) return;
  if (typeof event.key !== "string" || event.key.length !== 1) return;

  freeCreditsTriggerBuffer = (
    freeCreditsTriggerBuffer + event.key.toLowerCase()
  ).slice(-FREE_CREDITS_TRIGGER.length);

  if (freeCreditsTriggerBuffer !== FREE_CREDITS_TRIGGER) return;
  freeCreditsTriggerBuffer = "";

  if (!currentLoggedUid) {
    addLog("⚠ Vui lòng đăng nhập AutoHDR trước khi nhận quà.", "warn");
    return;
  }
  openFreeCreditsGiftModal();
}, true);

function openFreeCreditsGiftModal() {
  const modal = document.getElementById("free-credits-gift-modal");
  if (modal) modal.classList.remove("hidden");
  
  const statusBox = document.getElementById("gift-claim-status");
  if (statusBox) {
    statusBox.classList.add("hidden");
    statusBox.textContent = "";
  }
}

function closeFreeCreditsGiftModal() {
  const modal = document.getElementById("free-credits-gift-modal");
  if (modal) modal.classList.add("hidden");
}

async function claimFreeCreditsGift() {
  const btnAccept = document.getElementById("btn-gift-accept");
  const btnDecline = document.getElementById("btn-gift-decline");
  const statusBox = document.getElementById("gift-claim-status");
  
  if (!btnAccept || !statusBox) return;
  if (!currentLoggedUid) {
    alert("Không tìm thấy thông tin tài khoản đăng nhập!");
    return;
  }
  
  btnAccept.disabled = true;
  if (btnDecline) btnDecline.disabled = true;
  btnAccept.textContent = "Đang nhận...";
  
  statusBox.classList.remove("hidden");
  statusBox.textContent = "Đang kết nối nhận quà...";
  
  try {
    if (!window.pywebview || !window.pywebview.api || !window.pywebview.api.claim_free_credits) {
      throw new Error("API không khả dụng.");
    }
    
    const response = await window.pywebview.api.claim_free_credits();
    if (response.status >= 400 || response.status === -1) {
      const errMsg = response.error || (response.data && (response.data.detail || response.data.message)) || `Lỗi HTTP ${response.status}`;
      throw new Error(errMsg);
    }
    
    const result = response.data;
    const total = result.total || 0;
    const steps = result.steps || [];
    
    const allClaimed = steps.length ? steps.every(s => s.already_claimed && !s.error) : false;
    
    // Lưu vào localStorage để không hiện lại cho account này nữa
    localStorage.setItem(`claimed_credits_${currentLoggedUid}`, "true");
    
    if (total > 0) {
      statusBox.textContent = `Nhận thành công +${total} Credits!`;
      addLog(`🎁 Nhận thành công +${total} Free Credits cho user ${currentLoggedUid}`, "success");
      
      triggerConfettiCelebration();
      
      // Refresh credits balance in the sidebar
      isAuthChecking = false;
      verifyAuthLoop();
      
      setTimeout(() => {
        closeFreeCreditsGiftModal();
      }, 3000);
      
    } else if (allClaimed) {
      statusBox.textContent = "Tài khoản đã nhận gói quà này trước đó.";
      addLog("ℹ️ Tài khoản này đã nhận đủ các phần quà Free Credits trước đó.", "warn");
      
      setTimeout(() => {
        closeFreeCreditsGiftModal();
      }, 2000);
    } else {
      statusBox.textContent = "Không nhận thêm được credits mới nào.";
      setTimeout(() => {
        closeFreeCreditsGiftModal();
      }, 2000);
    }
  } catch (error) {
    statusBox.textContent = "Lỗi: " + error.message;
    addLog(`✗ Lỗi nhận quà tặng: ${error.message}`, "error");
    btnAccept.disabled = false;
    if (btnDecline) btnDecline.disabled = false;
    btnAccept.textContent = "Nhận 70 Credits";
  }
}

// Hàm kích hoạt pháo bông bắn confetti từ canvas-confetti
function triggerConfettiCelebration() {
  if (typeof confetti === "function") {
    // Bắn phát đầu ở giữa
    confetti({
      particleCount: 150,
      spread: 80,
      origin: { y: 0.6 }
    });
    
    // Bắn liên tiếp từ hai góc
    let duration = 2 * 1000;
    let end = Date.now() + duration;

    (function frame() {
      confetti({
        particleCount: 5,
        angle: 60,
        spread: 55,
        origin: { x: 0 }
      });
      confetti({
        particleCount: 5,
        angle: 120,
        spread: 55,
        origin: { x: 1 }
      });

      if (Date.now() < end) {
        requestAnimationFrame(frame);
      }
    }());
  } else {
    console.warn("confetti library not loaded");
  }
}

function promptCheckEmail() {
  document.getElementById('check-email-input').value = '';
  document.getElementById('check-email-result').style.display = 'none';
  document.getElementById('check-email-modal').classList.remove('hidden');
  setTimeout(() => document.getElementById('check-email-input').focus(), 100);
}

function closeCheckEmailModal() {
  document.getElementById('check-email-modal').classList.add('hidden');
}

async function executeCheckEmail() {
  const email = document.getElementById('check-email-input').value.trim();
  if (!email) return;
  
  const btn = document.getElementById('btn-execute-check-email');
  const resDiv = document.getElementById('check-email-result');
  
  btn.disabled = true;
  btn.textContent = 'Đang tra cứu...';
  resDiv.style.display = 'block';
  resDiv.innerHTML = '<span style="color: var(--text-muted);">Đang tìm kiếm thông tin...</span>';
  
  try {
    const res = await window.pywebview.api.autohdr_check_email_credits(email);
    if (res && res.ok) {
      let extraInfo = '';
      if (res.data.phone) extraInfo += `<br><b>SĐT:</b> ${res.data.phone}`;
      if (res.data.business_name) extraInfo += `<br><b>Doanh nghiệp:</b> ${res.data.business_name}`;
      
      resDiv.innerHTML = `<span style="color: var(--success-color);">Thành công!</span><br><b>Email:</b> ${res.data.email}<br><b>ID:</b> ${res.data.id}<br><b>Credits:</b> ${res.data.credits}${extraInfo}`;
    } else {
      resDiv.innerHTML = `<span style="color: var(--danger-color);">Thất bại:</span> ${res ? res.msg : 'Lỗi không xác định'}`;
    }
  } catch (e) {
    resDiv.innerHTML = `<span style="color: var(--danger-color);">Lỗi:</span> ${e.message}`;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Tra cứu';
  }
}

// --- DTE FEATURE LOGIC ---
async function browseDteFiles() {
  if (!window.pywebview || !window.pywebview.api.browse_files) return;
  const paths = await window.pywebview.api.browse_files('upload');
  if (paths && paths.length > 0) {
    selectedDteFiles = [...new Set([...selectedDteFiles, ...paths])];
    updateDteDropzoneList();
  }
}

function updateDteDropzoneList() {
  const container = document.getElementById("dte-dropzone-list");
  const footer = document.getElementById("dte-footer");
  if (!container || !footer) return;

  if (selectedDteFiles.length === 0) {
    container.classList.add("hidden");
    container.innerHTML = "";
    footer.style.display = "none";
    return;
  }
  container.classList.remove("hidden");
  footer.style.display = "block";
  let html = "";
  selectedDteFiles.forEach((f, idx) => {
    let name = f.split(/[\/\\]/).pop();
    html += `
      <div class="file-item" style="display:flex; align-items:center; gap:12px; padding:10px 16px; background:rgba(255,255,255,0.03); border:1px solid rgba(255,255,255,0.08); border-radius:10px; font-size:13px; margin-bottom:8px;">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0;"><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"></path><polyline points="13 2 13 9 20 9"></polyline></svg>
        <span style="flex:1;">${name}</span>
        <span class="remove" onclick="removeDteFile(${idx})" title="Xóa" style="color:#ff6b6b; cursor:pointer; font-weight:bold;">✕</span>
      </div>`;
  });
  container.innerHTML = html;
}

function removeDteFile(idx) {
  selectedDteFiles.splice(idx, 1);
  updateDteDropzoneList();
}

async function startDte() {
  if (!window.pywebview || !window.pywebview.api.start_dte) return;
  if (!selectedDteFiles.length) {
    addLog("⚠ Vui lòng chọn ảnh trước.", "warn");
    return;
  }
  
  // Skip asking for folder, Python will default to the image's folder
  const outputDir = "";
  
  const options = {
    twiMode: document.getElementById("dte-mode")?.value || "exterior",
    twiSky: document.getElementById("dte-sky")?.value || "colorful",
    twiPrompt: document.getElementById("dte-prompt")?.value || "",
    autoUpscale: document.getElementById("dte-upscale")?.checked !== false
  };
  
  const btn = document.getElementById("btn-start-dte");
  if (btn) btn.disabled = true;
  
  try {
    addLog(`▶ Đang bắt đầu xử lý Virtual Twilight cho ${selectedDteFiles.length} ảnh...`, "info");
    await window.pywebview.api.start_dte(selectedDteFiles, outputDir, options);
    
    selectedDteFiles = [];
    updateDteDropzoneList();
    const prompt = document.getElementById("dte-prompt");
    if(prompt) prompt.value = "";
  } catch (e) {
    addLog(`❌ Lỗi: ${e.message}`, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}
