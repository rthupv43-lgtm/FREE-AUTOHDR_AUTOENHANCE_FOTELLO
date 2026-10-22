"""
Clean AutoFotello — Minimalist PyWebView GUI + FastAPI server + Selenium bridge
No license checks, no bloat.
"""

import sys
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:
    pass

import os
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

from tls_config import configure_ca_bundle
configure_ca_bundle()

import shutil
import subprocess
import threading
import time
import json
import hashlib
import html as html_lib
import urllib.parse
import urllib.request
import webview
from pathlib import Path
import queue
import free_tools_engine

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


import requests

if sys.platform == "win32":
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
else:
    USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

_orig_request_init = urllib.request.Request.__init__


def _patched_request_init(self, *args, **kwargs):
    _orig_request_init(self, *args, **kwargs)
    has_ua = False
    for k in self.headers:
        if k.lower() == "user-agent":
            has_ua = True
            break
    if not has_ua:
        self.add_header("User-Agent", USER_AGENT)


urllib.request.Request.__init__ = _patched_request_init

_orig_session_request = requests.Session.request


def _patched_session_request(self, method, url, **kwargs):
    if "headers" not in kwargs or kwargs["headers"] is None:
        kwargs["headers"] = {}
    headers = kwargs["headers"]
    has_ua = any(k.lower() == "user-agent" for k in headers)
    if not has_ua:
        headers["User-Agent"] = USER_AGENT
    return _orig_session_request(self, method, url, **kwargs)


requests.Session.request = _patched_session_request

import fotello_engine as fotello
import autohdr_engine as autohdr
import autoenhance_engine as autoenhance
import upscale_engine as upscale
import detail_merge_engine as detail_merge
import storage_manager


def get_base_dir():
    return sys._MEIPASS if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))


def get_exe_dir():
    return os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))


BASE_DIR = get_base_dir()
EXE_DIR = get_exe_dir()
UI_DIR = os.path.join(BASE_DIR, "ui")



chrome_process = None
window = None
fotello_running = False
fotello_task_thread = None
autohdr_stop_event = threading.Event()
autohdr_task_thread = None
autohdr_running = False
autohdr_connected = False

_listings_cache = {
    "fotello": {"timestamp": 0, "data": None},
    "autohdr": {"timestamp": 0, "data": None},
}

active_upload_events = []
active_download_events = []
active_task_events = {}
active_task_lock = threading.Lock()
MAX_CONCURRENT_AUTOHDR_JOBS = 5
autohdr_session_lock = threading.BoundedSemaphore(MAX_CONCURRENT_AUTOHDR_JOBS)
deferred_profile_updates = {}


def register_task_session(session_id, stop_event, task_type, options=None, name=None):
    with active_task_lock:
        active_task_events[session_id] = {
            "event": stop_event,
            "type": task_type,
            "options": options,
            "name": name,
        }


def unregister_task_session(session_id):
    with active_task_lock:
        active_task_events.pop(session_id, None)


def has_active_task_type(task_type):
    with active_task_lock:
        return any(t.get("type") == task_type for t in active_task_events.values())


def stop_task_session(session_id):
    with active_task_lock:
        task = active_task_events.get(session_id)
        if not task:
            return None
        task["event"].set()
        return task["type"]


def input_label(input_source, fallback="Upload"):
    if storage_manager.is_remote_spec(input_source):
        return os.path.basename((input_source.get("path") or "").strip("/")) or "Dropbox"
    if isinstance(input_source, list):
        if not input_source:
            return fallback
        first = input_label(input_source[0], fallback=fallback)
        return f"{first} (+{len(input_source) - 1})" if len(input_source) > 1 else first
    return os.path.basename(str(input_source or "").rstrip("/\\")) or fallback


def _prepare_storage_input(input_source, session_id, session_name, stop_event=None):
    return storage_manager.prepare_input(
        input_source,
        run_id=session_id,
        log=lambda msg, type="info": js_log(msg, type, session_id, session_name),
        stop_event=stop_event,
    )


def _prepare_storage_output(output_target, session_id):
    return storage_manager.prepare_output(output_target, run_id=session_id)


def _finalize_storage_output(output_handle, output_folder, session_id, session_name, stop_event=None):
    if stop_event is not None and stop_event.is_set():
        return
    output_handle.finalize(
        output_folder,
        log=lambda msg, type="info": js_log(msg, type, session_id, session_name),
    )


def _show_output_location(session_id, output_handle, output_folder):
    if output_handle.is_remote:
        remote_path = str(output_handle.remote_path or "")
        if remote_path and not remote_path.startswith("/"):
            remote_path = "/" + remote_path
        dropbox_url = f"https://www.dropbox.com/home{remote_path}"
        js_show_folder_btn(session_id, dropbox_url)
    else:
        js_show_folder_btn(session_id, str(output_folder))



SUPABASE_URL = "https://mcqbutujzfcrrhdbiitw.supabase.co"
SUPABASE_KEY = "sb_publishable_fYskC8-1FzLuvWIcRmH-qw_jYp8KoyB"

AUTOHDR_COOKIE_FILE = Path(os.path.expanduser("~")) / ".autohdr_cookies.json"
AUTOHDR_CLAIMS_FILE = Path(os.path.expanduser("~")) / ".autohdr_claims.json"
AUTOENHANCE_TOKEN_FILE = Path(os.path.expanduser("~")) / ".autoenhance_token.json"
GUIDE_STATE_FILE = Path(os.path.expanduser("~")) / ".autofotello_guide_state.json"
DIALOG_STATE_FILE = Path(os.path.expanduser("~")) / ".autofotello_dialog_state.json"

guide_state_lock = threading.Lock()
dialog_state_lock = threading.Lock()
autohdr_cookie_lock = threading.RLock()


def _save_autohdr_claim(user_id):
    if not user_id:
        return
    try:
        claims = _load_autohdr_claims()
        claims.add(str(user_id))
        AUTOHDR_CLAIMS_FILE.write_text(
            json.dumps({"claims": list(claims)}, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _load_autohdr_claims():
    try:
        if AUTOHDR_CLAIMS_FILE.exists():
            data = json.loads(AUTOHDR_CLAIMS_FILE.read_text(encoding="utf-8"))
            return set(str(x) for x in data.get("claims", []))
    except Exception:
        pass
    return set()


autohdr_cookie_val = ""
autoenhance_token_val = ""
autoenhance_connected = False


def _save_autohdr_cookie(cookie):
    temp_path = None
    try:
        with autohdr_cookie_lock:
            AUTOHDR_COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
            temp_path = AUTOHDR_COOKIE_FILE.with_name(
                f"{AUTOHDR_COOKIE_FILE.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            temp_path.write_text(
                json.dumps({"cookie": str(cookie or "")}, indent=2),
                encoding="utf-8",
            )
            os.replace(temp_path, AUTOHDR_COOKIE_FILE)
    except Exception:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass


def _load_autohdr_cookie():
    try:
        with autohdr_cookie_lock:
            if AUTOHDR_COOKIE_FILE.exists():
                data = json.loads(AUTOHDR_COOKIE_FILE.read_text(encoding="utf-8"))
                return str(data.get("cookie") or "")
    except Exception:
        pass
    return ""


def _clear_autohdr_cookie():
    try:
        with autohdr_cookie_lock:
            AUTOHDR_COOKIE_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _is_explicit_autohdr_auth_error(error):
    """Only invalidate a saved session for an explicit authentication failure."""
    response = getattr(error, "response", None)
    if response is not None and getattr(response, "status_code", None) in (401, 403):
        return True
    message = str(error or "").lower()
    return "phiên đăng nhập autohdr đã hết hạn" in message or "autohdr session expired" in message


def _save_autoenhance_token(token):
    if not isinstance(token, str) or not token.strip():
        return
    try:
        with open(AUTOENHANCE_TOKEN_FILE, "w") as f:
            json.dump({"token": token.strip()}, f)
    except Exception:
        pass


def _clear_autoenhance_token():
    try:
        AUTOENHANCE_TOKEN_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _load_autoenhance_token():
    try:
        if AUTOENHANCE_TOKEN_FILE.exists():
            with open(AUTOENHANCE_TOKEN_FILE, "r") as f:
                data = json.load(f)
                tok = data.get("token", "")
                return tok.strip() if isinstance(tok, str) else ""
    except Exception:
        pass
    return ""


def _clear_chrome_cookies(domain_keyword):
    return 0


def _load_guide_state():
    with guide_state_lock:
        try:
            if GUIDE_STATE_FILE.exists():
                data = json.loads(GUIDE_STATE_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data.setdefault("onboarding", "")
                    if not isinstance(data.get("features"), dict):
                        data["features"] = {}
                    else:
                        data.setdefault("features", {})
                    return data
        except Exception:
            pass
        return {"onboarding": "", "features": {}}


def _update_guide_state(update_fn):
    with guide_state_lock:
        state = {"onboarding": "", "features": {}}
        try:
            if GUIDE_STATE_FILE.exists():
                loaded = json.loads(GUIDE_STATE_FILE.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    state.update(loaded)
        except Exception:
            pass
        if not isinstance(state.get("features"), dict):
            state["features"] = {}
        update_fn(state)
        try:
            GUIDE_STATE_FILE.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass
        return state


def _load_dialog_state():
    with dialog_state_lock:
        try:
            if DIALOG_STATE_FILE.exists():
                data = json.loads(DIALOG_STATE_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}


def _save_dialog_state(state):
    with dialog_state_lock:
        try:
            DIALOG_STATE_FILE.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass


def _is_cloud_file_provider_path(path):
    if sys.platform != "darwin" or not path:
        return False
    try:
        normalized = str(Path(path).expanduser())
    except Exception:
        normalized = str(path)
    markers = (
        "/Library/CloudStorage/",
        "/Library/Mobile Documents/",
        "/Library/Application Support/FileProvider/",
    )
    return any(marker in normalized for marker in markers)


def _dialog_start_dir():
    state = _load_dialog_state()
    candidates = [
        state.get("last_dir"),
        str(Path.home() / "Downloads"),
        str(Path.home()),
        EXE_DIR,
    ]
    for candidate in candidates:
        if not candidate or _is_cloud_file_provider_path(candidate):
            continue
        try:
            if Path(candidate).expanduser().is_dir():
                return str(Path(candidate).expanduser())
        except Exception:
            continue
    return str(Path.home())


def _remember_dialog_path(path):
    if not path:
        return
    try:
        selected = Path(path).expanduser()
        folder = selected if selected.is_dir() else selected.parent
        if not folder.is_dir() or _is_cloud_file_provider_path(folder):
            return
        state = _load_dialog_state()
        state["last_dir"] = str(folder)
        _save_dialog_state(state)
    except Exception:
        pass


def _applescript_string(value):
    return json.dumps(str(value))


autohdr_cookie_val = _load_autohdr_cookie()
autoenhance_token_val = _load_autoenhance_token()

settings = {"port": 9222, "threads": 8}


def read_app_version():
    try:
        version_file = os.path.join(BASE_DIR, "VERSION")
        with open(version_file, "r", encoding="utf-8") as f:
            version = f.read().strip()
            if version:
                return version
    except Exception:
        pass
    return "0.0.0"


CURRENT_VERSION = read_app_version()


def is_update_available(current, latest):
    try:
        curr_parts = [int(x) for x in str(current).split(".")]
        late_parts = [int(x) for x in str(latest).split(".")]
        max_len = max(len(curr_parts), len(late_parts))
        curr_parts.extend([0] * (max_len - len(curr_parts)))
        late_parts.extend([0] * (max_len - len(late_parts)))
        return late_parts > curr_parts
    except Exception:
        return str(latest) != str(current)


def js_log(msg, msg_type="", session_id="global", session_name=None):
    print(f"[{msg_type.upper()}][{session_id}] {msg}")
    if window:
        try:
            window.evaluate_js(
                f"addLog({json.dumps(msg)}, {json.dumps(msg_type)}, {json.dumps(session_id)}, {json.dumps(session_name)})"
            )
        except Exception:
            pass


def js_status(state, text, task_type="upload", session_name=None):
    print(f"[STATUS] {state} ({task_type}): {text}")
    if window:
        import json
        try:
            window.evaluate_js(
                f"setStatus('{state}', '{text}', '{task_type}', {json.dumps(session_name)})"
            )
        except Exception:
            pass


def js_session_status(text, session_id="global"):
    print(f"[SESSION_STATUS][{session_id}] {text}")
    if window:
        try:
            window.evaluate_js(
                f"updateSessionStatus({json.dumps(session_id)}, {json.dumps(text)})"
            )
        except Exception:
            pass


def js_show_folder_btn(session_id, folder_path):
    if window:
        try:
            window.evaluate_js(
                f"showFolderBtn({json.dumps(session_id)}, {json.dumps(folder_path)})"
            )
        except Exception:
            pass


def cdp_extract_autoenhance_token():
    try:
        import websocket
        url = f"http://127.0.0.1:{settings.get('port')}/json"
        with urllib.request.urlopen(url) as response:
            tabs = json.loads(response.read().decode())
        target_tab = next((t for t in tabs if "autoenhance.ai" in t.get("url", "")), None)
        if not target_tab:
            return None
        ws_url = target_tab["webSocketDebuggerUrl"]
        ws = websocket.create_connection(ws_url, suppress_origin=True)
        ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": "localStorage.getItem('token')"},
        }))
        res = json.loads(ws.recv())
        ws.close()
        token = res.get("result", {}).get("result", {}).get("value")
        if token:
            _save_autoenhance_token(token)
            return token
    except Exception:
        pass
    return None


def js_progress(current, total, session_id="global"):
    pct = int(current / total * 100) if total > 0 else 0
    print(f"[PROGRESS] {current}/{total} ({pct}%)")
    if window:
        try:
            window.evaluate_js(
                f"updateProgress({current}, {total}, {pct}, {json.dumps(session_id)})"
            )
        except Exception:
            pass



_PERMANENT_HWID_POOL = [
    "e4dcc52fd354fff3c60495a5adaa02d3",
    "bfaa4ed9dd81db945476c3d75089a2a9",
    "053f6ba80eec9511daae7400a9de9756",
    "86f64914b138f50e46d728ba1d2e0859",
    "1078934abf45db66a52fee21a0233da6",
    "aae34e3178341532a4fce0c80a5ac509",
    "88c214c50bbd57d50db6257df1faad6f",
    "85be12ce1891c5c4d7e7b57b9231a5e9",
    "7ff5896d2a07a7f54ae468648710aab9",
    "2dd5ea2e411fcdbc40950f40887e8239",
]


def get_hwid():
    """Return a permanently activated HWID distributed across machines."""
    try:
        import platform
        node = platform.node() or "default"
        idx = int(hashlib.md5(node.encode("utf-8")).hexdigest(), 16) % len(_PERMANENT_HWID_POOL)
        return _PERMANENT_HWID_POOL[idx]
    except Exception:
        return "e4dcc52fd354fff3c60495a5adaa02d3"


def is_valid_hwid(hwid):
    """Only allow IDs produced by the SHA-256 hardware fingerprint paths."""
    value = str(hwid or "").strip().lower()
    return len(value) == 32 and all(char in "0123456789abcdef" for char in value)


def find_chrome():
    if sys.platform == "darwin":
        paths = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
        for p in paths:
            if os.path.exists(p):
                return p
    else:
        portable = os.path.join(EXE_DIR, "GoogleChromePortable", "App", "Chrome-bin", "chrome.exe")
        if os.path.exists(portable):
            return portable
        windows_paths = [
            os.path.expandvars(r"%ProgramW6432%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(Arm)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
        for p in windows_paths:
            if p and os.path.exists(p):
                return p
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
            )
            path, _ = winreg.QueryValueEx(key, "")
            winreg.CloseKey(key)
            if path and os.path.exists(path):
                return path
        except Exception:
            pass
    return None


CHROME_PATH = find_chrome()


def get_chrome_profile_dir():
    if sys.platform == "darwin":
        return os.path.join(
            os.path.expanduser("~/Library/Application Support"),
            "AutoFotello",
            "chrome_profiles",
            "default",
        )
    return os.path.join(EXE_DIR, "chrome_profiles", "default")


PROFILE_DIR = get_chrome_profile_dir()


def do_open_chrome(target_url=None):
    global CHROME_PATH, chrome_process
    CHROME_PATH = find_chrome()
    if not CHROME_PATH:
        js_log("❌ Không tìm thấy Google Chrome. Tải tại: https://www.google.com/chrome", "error")
        return False
    try:
        os.makedirs(PROFILE_DIR, exist_ok=True)
    except Exception as e:
        js_log(f"Không tạo được Chrome profile tại {PROFILE_DIR}: {e}", "error")
        return False

    port = settings.get("port", 9222)
    if chrome_process and chrome_process.poll() is None:
        if check_chrome_responsive(port):
            js_log("Chrome đã mở và debug port sẵn sàng.", "warn")
            if target_url:
                try:
                    cdp_open_tab(target_url)
                except Exception:
                    pass
            return True
        js_log("Chrome cũ không phản hồi debug port, đang khởi động lại...", "warn")
        try:
            chrome_process.terminate()
            chrome_process.wait(timeout=3)
        except Exception:
            try:
                chrome_process.kill()
            except Exception:
                pass
        chrome_process = None
    elif check_chrome_responsive(port):
        js_log("Chrome đã mở và debug port sẵn sàng.", "warn")
        if target_url:
            try:
                cdp_open_tab(target_url)
            except Exception:
                pass
        return True

    for lock_file in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        lock_path = os.path.join(PROFILE_DIR, lock_file)
        if os.path.exists(lock_path):
            try:
                os.remove(lock_path)
            except Exception:
                pass

    cmd = [
        CHROME_PATH,
        f"--user-data-dir={PROFILE_DIR}",
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-infobars",
        "--disable-session-crashed-bubble",
        "--disable-features=TranslateUI",
        target_url or "https://app.fotello.co",
    ]
    try:
        chrome_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        js_log(f"Chrome opened — PID {chrome_process.pid} (profile: {PROFILE_DIR})", "success")
        return True
    except Exception as e:
        js_log(f"Không khởi chạy được Chrome: {e}", "error")
        return False


def check_chrome_responsive(port):
    try:
        url = f"http://127.0.0.1:{port}/json"
        req = urllib.request.Request(url)
        handler = urllib.request.ProxyHandler({})
        opener = urllib.request.build_opener(handler)
        with opener.open(req, timeout=0.5) as response:
            data = json.loads(response.read().decode())
            return isinstance(data, list)
    except Exception:
        return False


def wait_for_chrome_debugging(port, timeout=60, interval=0.5):
    """Wait for Chrome's DevTools endpoint without failing on a cold startup."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check_chrome_responsive(port):
            return True
        time.sleep(interval)
    return check_chrome_responsive(port)


def cdp_json(path, method="GET"):
    port = settings.get("port", 9222)
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, method=method)
    handler = urllib.request.ProxyHandler({})
    opener = urllib.request.build_opener(handler)
    with opener.open(req, timeout=2) as response:
        raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else None


def cdp_tabs():
    tabs = cdp_json("/json") or []
    return [tab for tab in tabs if tab.get("type") == "page" and tab.get("webSocketDebuggerUrl")]


def cdp_open_tab(url):
    quoted = urllib.parse.quote(url, safe=":/?&=%")
    return cdp_json(f"/json/new?{quoted}", method="PUT")


def cdp_find_or_open_tab(service, autocreate=True):
    if service == "fotello":
        target_url = "https://app.fotello.co"
        target_domain = "fotello.co"
    elif service == "autoenhance":
        target_url = "https://app.autoenhance.ai"
        target_domain = "autoenhance.ai"
    else:
        target_url = "https://www.autohdr.com"
        target_domain = "autohdr.com"

    for tab in cdp_tabs():
        url = tab.get("url") or ""
        if target_domain in url or (service == "autoenhance" and "auth0.com" in url):
            return tab

    if not autocreate:
        return None

    try:
        tab = cdp_open_tab(target_url)
        if isinstance(tab, dict) and tab.get("webSocketDebuggerUrl"):
            return tab
    except Exception:
        pass

    time.sleep(1)
    for tab in cdp_tabs():
        url = tab.get("url") or ""
        if target_domain in url or (service == "autoenhance" and "auth0.com" in url):
            return tab
    return None


def cdp_matching_tabs(service):
    """Return every current browser tab that may hold auth for a service."""
    if service == "fotello":
        domains = ("fotello.co",)
    elif service == "autoenhance":
        domains = ("autoenhance.ai", "auth0.com")
    else:
        domains = ("autohdr.com",)

    return [
        tab
        for tab in cdp_tabs()
        if any(domain in (tab.get("url") or "") for domain in domains)
    ]


def cdp_call(tab, method, params=None, timeout=10):
    import websocket
    ws_url = tab.get("webSocketDebuggerUrl")
    if not ws_url:
        raise RuntimeError("Tab Chrome không có websocket debugger URL")
    ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
    try:
        message_id = int(time.time() * 1000) % 1000000
        ws.send(json.dumps({
            "id": message_id,
            "method": method,
            "params": params or {},
        }))
        while True:
            payload = json.loads(ws.recv())
            if payload.get("id") == message_id:
                if payload.get("error"):
                    raise RuntimeError(str(payload["error"]))
                return payload.get("result") or {}
    finally:
        try:
            ws.close()
        except Exception:
            pass


def cdp_eval(tab, expression, await_promise=False, timeout=10):
    result = cdp_call(
        tab,
        "Runtime.evaluate",
        {
            "expression": expression,
            "awaitPromise": bool(await_promise),
            "returnByValue": True,
        },
        timeout=timeout,
    )
    remote = result.get("result") or {}
    if "value" in remote:
        return remote.get("value")
    return None


def cdp_extract_fotello_token(tab):
    expression = """
(async () => {
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i);
    if (key && key.startsWith('firebase:authUser:')) {
      try {
        const data = JSON.parse(localStorage.getItem(key));
        if (data && data.stsTokenManager && data.stsTokenManager.refreshToken) {
          return {
            refresh_token: data.stsTokenManager.refreshToken || '',
            access_token: data.stsTokenManager.accessToken || '',
            uid: data.uid || '',
            email: data.email || '',
          };
        }
      } catch (e) {}
    }
  }

  try {
    const db = await new Promise((resolve, reject) => {
      const req = indexedDB.open('firebaseLocalStorageDb');
      req.onsuccess = (e) => resolve(e.target.result);
      req.onerror = () => reject(req.error);
    });
    const items = await new Promise((resolve, reject) => {
      const tx = db.transaction('firebaseLocalStorage', 'readonly');
      const store = tx.objectStore('firebaseLocalStorage');
      const req = store.getAll();
      req.onsuccess = (e) => resolve(e.target.result || []);
      req.onerror = () => reject(req.error);
    });
    for (const item of items) {
      const data = item && item.value;
      if (data && data.stsTokenManager && data.stsTokenManager.refreshToken) {
        return {
          refresh_token: data.stsTokenManager.refreshToken || '',
          access_token: data.stsTokenManager.accessToken || '',
          uid: data.uid || '',
          email: data.email || '',
        };
      }
    }
  } catch (e) {}
  return null;
})()
"""
    return cdp_eval(tab, expression, await_promise=True, timeout=12)


def cdp_apply_fotello_token(token_data):
    if not isinstance(token_data, dict) or not token_data.get("refresh_token"):
        return False
    fotello._fotello_state["refresh_token"] = token_data["refresh_token"]
    if token_data.get("access_token"):
        fotello._fotello_state["access_token"] = token_data["access_token"]
    tokens = fotello.fotello_get_tokens()
    fotello._fotello_state["connected"] = True
    try:
        team_id = fotello._detect_team_id(tokens["id_token"], tokens["access_token"])
        if team_id:
            fotello._fotello_state["team_id"] = team_id
    except Exception as e:
        js_log(f"⚠ Detect Team ID lỗi: {e}", "warn")
    return True


def cdp_get_cookie_header(tab, domain):
    result = cdp_call(tab, "Network.getAllCookies", timeout=10)
    pairs = []
    for cookie in result.get("cookies") or []:
        c_domain = str(cookie.get("domain") or "")
        if domain in c_domain and cookie.get("name"):
            pairs.append(f"{cookie.get('name')}={cookie.get('value')}")
    return "; ".join(pairs)


def cdp_extract_autoenhance_token(tab):
    """Extract Autoenhance API key using session cookies from the browser."""
    import re as _re
    try:
        result = cdp_call(tab, "Network.getAllCookies", timeout=10)
        all_cookies = result.get("cookies") or []
        cookie_parts = []
        for c in all_cookies:
            c_domain = c.get("domain", "")
            if "autoenhance.ai" in c_domain:
                cookie_parts.append(f"{c['name']}={c['value']}")
        if not cookie_parts:
            print("[CDP_AUTOENHANCE] No autoenhance.ai cookies found")
            return None
        cookie_header = "; ".join(cookie_parts)
        print(f"[CDP_AUTOENHANCE] Found {len(cookie_parts)} cookies, fetching settings page...")
        resp = requests.get(
            "https://app.autoenhance.ai/settings?tab=subscription",
            headers={
                "Cookie": cookie_header,
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"[CDP_AUTOENHANCE] Settings page returned {resp.status_code}")
            return None
        match = _re.search(r'\\?"(?:apiKey|api_key|key)\\?":\\?"([a-f0-9\-]{36})', resp.text)
        if match:
            api_key = match.group(1)
            print(f"[CDP_AUTOENHANCE] ✔ Extracted API key: {api_key[:8]}...")
            return api_key
        print("[CDP_AUTOENHANCE] API key not found in settings page HTML")
    except Exception as ex:
        print(f"[CDP_AUTOENHANCE] Error: {ex}")
    return None


def executable_works(path, args=None):
    if not path:
        return False
    args = args or ["--version"]
    try:
        startupinfo = None
        if sys.platform.startswith("win"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
        subprocess.check_output(
            [path, *args],
            stderr=subprocess.STDOUT,
            timeout=5,
            startupinfo=startupinfo,
        )
        return True
    except Exception as e:
        js_log(f"Bỏ qua ChromeDriver không chạy được ({path}): {e}", "warn")
        return False


def find_chromedriver():
    driver_names = ["chromedriver.exe", "chromedriver"] if sys.platform.startswith("win") else ["chromedriver"]
    search_dirs = [BASE_DIR, EXE_DIR] if getattr(sys, "frozen", False) else [os.path.dirname(os.path.abspath(__file__))]
    for directory in search_dirs:
        for name in driver_names:
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                if not sys.platform.startswith("win") and not os.access(path, os.X_OK):
                    try:
                        os.chmod(path, 0o755)
                    except Exception:
                        pass
                if (sys.platform.startswith("win") or os.access(path, os.X_OK)) and executable_works(path):
                    return path
    system_driver = shutil.which("chromedriver")
    if system_driver and executable_works(system_driver):
        return system_driver
    return None


def get_driver():
    port = settings.get("port", 9222)
    if not check_chrome_responsive(port):
        return None
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        options = Options()
        options.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
        driver_path = find_chromedriver()
        if driver_path:
            js_log(f"Đang dùng ChromeDriver: {driver_path}", "info")
            service = Service(executable_path=driver_path)
            return webdriver.Chrome(service=service, options=options)
        js_log("Không tìm thấy ChromeDriver local phù hợp, đang dùng Selenium Manager.", "warn")
        js_log("Lần đầu Selenium Manager có thể cần vài chục giây và cần internet để lấy driver đúng phiên bản Chrome.", "info")
        return webdriver.Chrome(options=options)
    except Exception as e:
        js_log(f"Selenium lỗi kết nối Chrome: {e}", "error")
        if sys.platform == "darwin":
            js_log("Nếu lỗi còn tiếp diễn, hãy cài ChromeDriver bản macOS phù hợp hoặc cho phép Selenium Manager tải driver.", "warn")
        return None


class Api:
    def storage_status(self):
        try:
            return {"ok": True, **storage_manager.get_status()}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def storage_set_dropbox_token(self, access_token):
        try:
            return {"ok": True, **storage_manager.set_dropbox_token(access_token)}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def storage_clear_dropbox_token(self):
        try:
            return {"ok": True, **storage_manager.clear_dropbox_token()}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def storage_start_dropbox_oauth(self, app_key=None, redirect_uri=None):
        try:
            import webbrowser
            session = storage_manager.start_dropbox_broker_oauth(
                hwid=self.get_hwid(),
                supabase_url=SUPABASE_URL,
                supabase_key=SUPABASE_KEY,
            )
            webbrowser.open(session["auth_url"])

            def _poll():
                deadline = time.time() + 180
                last_error = ""
                while time.time() < deadline:
                    try:
                        status = storage_manager.poll_dropbox_broker_status()
                        if status.get("connected"):
                            js_log("Đăng nhập Dropbox thành công.", "success")
                            if window:
                                try:
                                    window.evaluate_js("window.onDropboxOAuthComplete && window.onDropboxOAuthComplete(true)")
                                except Exception:
                                    pass
                            return
                        if status.get("msg"):
                            last_error = status.get("msg")
                    except Exception as poll_error:
                        last_error = str(poll_error)
                    time.sleep(2)
                msg = last_error or "Hết thời gian chờ đăng nhập Dropbox."
                js_log(f"Dropbox OAuth lỗi: {msg}", "error")
                if window:
                    try:
                        window.evaluate_js(
                            f"window.onDropboxOAuthComplete && window.onDropboxOAuthComplete(false, {json.dumps(msg)})"
                        )
                    except Exception:
                        pass

            threading.Thread(target=_poll, daemon=True).start()
            return {
                "ok": True,
                "mode": "supabase",
                "auth_url": session["auth_url"],
                "redirect_uri": session.get("redirect_uri"),
            }
        except Exception as broker_error:
            if not app_key:
                js_log(f"Supabase Dropbox OAuth chưa sẵn sàng: {broker_error}", "error")
                return {"ok": False, "msg": str(broker_error)}

        try:
            import http.server
            import webbrowser
            from urllib.parse import parse_qs, urlparse

            session = storage_manager.create_dropbox_oauth_session(
                app_key=app_key,
                redirect_uri=redirect_uri,
            )
            parsed_redirect = urlparse(session["redirect_uri"])
            host = parsed_redirect.hostname or "127.0.0.1"
            port = int(parsed_redirect.port or 53682)
            callback_path = parsed_redirect.path or "/dropbox/oauth/callback"

            def render_dropbox_callback_page(ok, message):
                title = "Dropbox đã kết nối" if ok else "Kết nối Dropbox lỗi"
                subtitle = (
                    "Bạn có thể đóng tab này và quay lại AutoFotello."
                    if ok
                    else "Vui lòng quay lại AutoFotello và thử đăng nhập Dropbox lại."
                )
                accent = "#5b5cf0" if ok else "#ff5a52"
                icon = "✓" if ok else "!"
                detail = f"<div class='detail'>{html_lib.escape(str(message or ''))}</div>" if message else ""
                return (
                    f'<!doctype html>\n<html lang="vi">\n<head>\n  <meta charset="utf-8">\n'
                    f'  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
                    f'  <title>{title} - AutoFotello</title>\n  <style>\n'
                    f'    :root {{ color-scheme: dark; --bg:#0b0b12; --panel:#171725; --panel-2:#202034; --text:#f5f5ff; --muted:#a7a7bd; --line:rgba(255,255,255,.12); --accent:{accent}; }}\n'
                    f'    * {{ box-sizing: border-box; }}\n'
                    f'    body {{ margin:0; min-height:100vh; display:grid; place-items:center; padding:24px; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif; background:radial-gradient(circle at 50% -10%, rgba(91,92,240,.28), transparent 36%), linear-gradient(180deg,#10101b 0%,var(--bg) 100%); color:var(--text); }}\n'
                    f'    main {{ width:min(520px,100%); border:1px solid var(--line); border-radius:22px; background:linear-gradient(180deg,rgba(255,255,255,.06),rgba(255,255,255,.025)),var(--panel); box-shadow:0 24px 80px rgba(0,0,0,.45); padding:34px; text-align:center; }}\n'
                    f'    .brand {{ display:inline-flex; align-items:center; gap:10px; margin-bottom:26px; color:var(--muted); font-size:13px; font-weight:700; letter-spacing:.08em; text-transform:uppercase; }}\n'
                    f'    .mark {{ width:36px; height:36px; display:grid; place-items:center; border-radius:11px; background:var(--panel-2); color:#fff; font-size:18px; box-shadow:inset 0 0 0 1px var(--line); }}\n'
                    f'    .status {{ width:76px; height:76px; display:grid; place-items:center; margin:0 auto 22px; border-radius:24px; background:color-mix(in srgb,var(--accent) 22%,transparent); color:#fff; font-size:38px; font-weight:800; box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--accent) 48%,transparent); }}\n'
                    f'    h1 {{ margin:0; font-size:30px; line-height:1.12; letter-spacing:0; }}\n'
                    f'    p {{ margin:14px auto 0; max-width:390px; color:var(--muted); font-size:16px; line-height:1.55; }}\n'
                    f'    .detail {{ margin-top:22px; padding:12px 14px; border:1px solid var(--line); border-radius:12px; background:rgba(255,255,255,.035); color:var(--text); font-size:13px; line-height:1.45; overflow-wrap:anywhere; }}\n'
                    f'    .hint {{ margin-top:24px; color:var(--muted); font-size:13px; }}\n'
                    f'  </style>\n</head>\n<body>\n  <main>\n'
                    f'    <div class="brand"><span class="mark">A</span><span>AutoFotello</span></div>\n'
                    f'    <div class="status">{icon}</div>\n    <h1>{title}</h1>\n    <p>{subtitle}</p>\n'
                    f'    {detail}\n    <div class="hint">Tab này không cần giữ mở.</div>\n  </main>\n</body>\n</html>'
                )

            class CallbackHandler(http.server.BaseHTTPRequestHandler):
                def log_message(self, format, *args):
                    return

                def do_GET(self):
                    parsed = urlparse(self.path)
                    query = parse_qs(parsed.query or "")
                    if parsed.path != callback_path:
                        self.send_response(404)
                        self.end_headers()
                        return
                    ok = False
                    message = ""
                    try:
                        if (query.get("state") or [""])[0] != session["state"]:
                            raise RuntimeError("OAuth state không khớp.")
                        if query.get("error"):
                            raise RuntimeError((query.get("error_description") or query.get("error") or [""])[0])
                        code = (query.get("code") or [""])[0]
                        if not code:
                            raise RuntimeError("Dropbox không trả authorization code.")
                        storage_manager.finish_dropbox_oauth(
                            code=code,
                            code_verifier=session["code_verifier"],
                            app_key=session["app_key"],
                            redirect_uri=session["redirect_uri"],
                        )
                        ok = True
                        message = "Đăng nhập Dropbox thành công. Bạn có thể đóng tab này."
                        js_log("Đăng nhập Dropbox thành công.", "success")
                        if window:
                            try:
                                window.evaluate_js("window.onDropboxOAuthComplete && window.onDropboxOAuthComplete(true)")
                            except Exception:
                                pass
                    except Exception as exc:
                        message = f"Đăng nhập Dropbox lỗi: {exc}"
                        js_log(message, "error")
                        if window:
                            try:
                                window.evaluate_js(
                                    f"window.onDropboxOAuthComplete && window.onDropboxOAuthComplete(false, {json.dumps(str(exc))})"
                                )
                            except Exception:
                                pass
                    finally:
                        body = render_dropbox_callback_page(ok, message).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        threading.Thread(target=self.server.shutdown, daemon=True).start()

            server = http.server.ThreadingHTTPServer((host, port), CallbackHandler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            webbrowser.open(session["auth_url"])
            return {
                "ok": True,
                "auth_url": session["auth_url"],
                "redirect_uri": session["redirect_uri"],
            }
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def storage_list_dropbox_folder(self, path="", recursive=False):
        try:
            return {"ok": True, **storage_manager.list_dropbox_folder(path, recursive=recursive)}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def get_guide_state(self):
        return _load_guide_state()

    def set_onboarding_state(self, state):
        value = str(state or "")
        _update_guide_state(lambda data: data.__setitem__("onboarding", value))
        return {"ok": True}

    def mark_feature_guide_seen(self, feature_key):
        key = str(feature_key or "")
        if key:
            _update_guide_state(lambda data: data["features"].__setitem__(key, True))
        return {"ok": True}

    def mark_all_feature_guides_seen(self, feature_keys):
        keys = [str(k) for k in (feature_keys or []) if str(k)]

        def apply_update(data):
            for key in keys:
                data["features"][key] = True

        _update_guide_state(apply_update)
        return {"ok": True}

    def get_hwid(self):
        return get_hwid()

    def claim_free_credits(self):
        import requests
        if not autohdr_cookie_val:
            return {
                "status": 401,
                "error": "Chưa đăng nhập AutoHDR. Vui lòng đăng nhập trước khi nhận quà.",
            }
        try:
            session_token = ""
            for part in autohdr_cookie_val.split(";"):
                part = part.strip()
                if part.startswith("__Secure-better-auth.session_token="):
                    session_token = part.split("=", 1)[1]
                    break
            if not session_token:
                session_token = autohdr_cookie_val
            cf_url = "https://autohdr-auth.phans3806.workers.dev"
            secret = "fotello-crack-super-secret-2026"
            payload = {
                "action": "credit_claim_free",
                "hwid": self.get_hwid(),
                "session_cookie": autohdr_cookie_val,
                "token": session_token,
            }
            headers = {
                "Authorization": f"Bearer {secret}",
                "X-API-Token": secret,
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            }
            resp = requests.post(cf_url, json=payload, headers=headers, timeout=45)
            if resp.status_code == 200:
                data = resp.json()
                user_id = data.get("user_id")
                if user_id:
                    _save_autohdr_claim(user_id)
            return {
                "status": resp.status_code,
                "data": resp.json(),
            }
        except Exception as e:
            return {"status": -1, "error": str(e)}

    def open_link(self, url):
        import webbrowser
        try:
            webbrowser.open(url)
            return {"ok": True}
        except Exception as e:
            print(f"Error opening link: {e}")
            return {"ok": False, "msg": str(e)}

    def notify_user(self, title, message):
        import platform
        import subprocess
        print(f"[NOTIFICATION] {title}: {message}")
        try:
            system = platform.system()
            if system == "Darwin":
                cmd = f'display notification "{message}" with title "{title}"'
                subprocess.run(["osascript", "-e", cmd], timeout=3)
            elif system == "Windows":
                ps_script = (
                    f'[void] [System.Reflection.Assembly]::LoadWithPartialName("System.Windows.Forms"); '
                    f'$objNotification = New-Object System.Windows.Forms.NotifyIcon; '
                    f'$objNotification.Icon = [System.Drawing.SystemIcons]::Information; '
                    f'$objNotification.BalloonTipIcon = "Info"; '
                    f'$objNotification.BalloonTipTitle = "{title}"; '
                    f'$objNotification.BalloonTipText = "{message}"; '
                    f'$objNotification.Visible = $True; '
                    f'$objNotification.ShowBalloonTip(5000);'
                )
                subprocess.run(["powershell", "-Command", ps_script], timeout=3)
            elif system == "Linux":
                subprocess.run(["notify-send", title, message], timeout=3)
        except Exception as e:
            print(f"Error showing notification: {e}")

    def get_app_version(self):
        return {"current_version": CURRENT_VERSION}

    def check_app_update(self):
        return {"success": False, "has_update": False}

    def start_auto_update(self):
        threading.Thread(target=self._run_auto_update, daemon=True).start()
        return {"ok": True}

    def _get_latest_download_url(self):
        import requests
        import platform
        try:
            url = f"{SUPABASE_URL}/rest/v1/app_version?id=eq.1"
            headers = {
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
            }
            resp = requests.get(url, headers=headers, timeout=5)
            if resp.status_code == 200:
                rows = resp.json()
                if rows:
                    row = rows[0]
                    if platform.system() == "Darwin":
                        download_url = row.get("download_url_mac") or row.get("download_url_win")
                        if download_url and platform.machine().lower() in ("x86_64", "amd64"):
                            download_url = download_url.replace("AutoFotello_mac.zip", "AutoFotello_mac_x64.zip")
                        return download_url
                    return row.get("download_url_win")
        except Exception:
            pass
        return None

    def _run_auto_update(self):
        import requests
        import platform
        import zipfile
        import shutil
        import urllib.parse
        import tempfile

        new_temp_path = ""

        def notify_update_failed(message):
            if window:
                try:
                    window.evaluate_js(f"updateInstallStatus('error', {json.dumps(message)})")
                except Exception:
                    pass

        def notify_update_saved(path):
            if window:
                try:
                    window.evaluate_js(f"updateInstallStatus('dev_saved', {json.dumps(path)})")
                except Exception:
                    pass

        try:
            download_url = self._get_latest_download_url()
            if not download_url:
                raise Exception("Không tìm thấy đường dẫn tải xuống bản cập nhật trong cơ sở dữ liệu.")

            is_frozen = getattr(sys, "frozen", False)
            current_path = os.path.abspath(sys.executable) if is_frozen else os.path.abspath(sys.argv[0])
            exe_dir = os.path.dirname(current_path)
            system = platform.system()
            is_mac_app_bundle = bool(is_frozen and system == "Darwin" and "/Contents/MacOS/" in current_path)

            if is_mac_app_bundle and "/AppTranslocation/" in current_path:
                raise Exception(
                    "AutoFotello đang chạy trong vùng App Translocation chỉ đọc của macOS. Hãy đóng ứng dụng, kéo AutoFotello.app vào thư mục Applications, chạy 'xattr -cr /Applications/AutoFotello.app' trong Terminal, rồi mở lại để cập nhật."
                )

            parsed_url = urllib.parse.urlparse(download_url)
            filename = os.path.basename(parsed_url.path)
            if not filename or "." not in filename:
                filename = "AutoFotello_new.zip" if system == "Darwin" else "AutoFotello_new.exe"

            temp_dir = tempfile.gettempdir() if is_mac_app_bundle else exe_dir
            new_temp_path = os.path.join(temp_dir, f"{filename}.{os.getpid()}.tmp")
            print(f"[AUTO_UPDATE] Starting download from {download_url} to {new_temp_path}")

            parsed = urllib.parse.urlparse(download_url)
            query = urllib.parse.parse_qs(parsed.query)
            query["t"] = [str(int(time.time()))]
            new_query = urllib.parse.urlencode(query, doseq=True)
            cache_busted_url = parsed._replace(query=new_query).geturl()

            resp = requests.get(cache_busted_url, stream=True, timeout=30)
            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "").lower()
            if "text/html" in content_type:
                raise Exception(
                    "Đường dẫn tải xuống không phải link tải file trực tiếp (trả về trang HTML). Vui lòng cấu hình lại link tải."
                )

            total_size = int(resp.headers.get("content-length", 0))
            downloaded = 0
            is_html = False

            with open(new_temp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=16384):
                    if chunk:
                        if downloaded == 0:
                            first_bytes = chunk[:200].lower().strip()
                            if first_bytes.startswith(b"<!doctype html") or first_bytes.startswith(b"<html"):
                                is_html = True
                                break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size > 0:
                            percent = int(downloaded * 100 / total_size)
                            if window:
                                try:
                                    window.evaluate_js(f"updateDownloadProgress({percent})")
                                except Exception:
                                    pass

            if is_html:
                raise Exception(
                    "Tệp tải về là trang web (HTML) chứ không phải tệp cập nhật. Vui lòng cấu hình link tải trực tiếp (Direct Download Link)."
                )

            if window:
                try:
                    window.evaluate_js("updateDownloadProgress(100)")
                except Exception:
                    pass

            time.sleep(1)

            is_zip = False
            if os.path.exists(new_temp_path):
                with open(new_temp_path, "rb") as f_check:
                    sig = f_check.read(4)
                    if sig == b"PK\x03\x04":
                        is_zip = True

            if not is_frozen:
                dev_dest = os.path.join(exe_dir, filename)
                if os.path.exists(dev_dest):
                    os.remove(dev_dest)
                os.rename(new_temp_path, dev_dest)
                js_log(f"✔ [Chế độ Dev] Đã tải tệp mới về: {dev_dest}", "success")
                notify_update_saved(dev_dest.replace(os.sep, "/"))
                return

            if system == "Windows":
                zip_dir = os.path.dirname(current_path)
                if is_zip:
                    extracted_exe = None
                    with zipfile.ZipFile(new_temp_path, "r") as zip_ref:
                        for name in zip_ref.namelist():
                            if name.endswith(".exe"):
                                zip_ref.extract(name, zip_dir)
                                extracted_exe = os.path.abspath(os.path.join(zip_dir, name))
                                break
                    try:
                        os.remove(new_temp_path)
                    except Exception:
                        pass
                    if extracted_exe and os.path.exists(extracted_exe):
                        os.rename(extracted_exe, new_temp_path)
                    else:
                        raise Exception("Không tìm thấy file .exe bên trong file zip cập nhật.")

                bat_path = os.path.join(zip_dir, "updater.bat")
                with open(bat_path, "w", encoding="utf-8") as bf:
                    bf.write(
                        f'@echo off\nsetlocal\nset "NEW_EXE={new_temp_path}"\nset "TARGET_EXE={current_path}"\n'
                        f'set "PYINSTALLER_RESET_ENVIRONMENT=1"\nset "_MEIPASS2="\n\n'
                        f':wait_for_exit\ntimeout /t 1 /nobreak > nul\n'
                        f'copy /y "%NEW_EXE%" "%TARGET_EXE%" > nul 2>&1\n'
                        f'if errorlevel 1 goto wait_for_exit\n\n'
                        f'del "%NEW_EXE%" > nul 2>&1\n'
                        f'start "" "%TARGET_EXE%"\nendlocal\ndel "%~f0"\n'
                    )
                subprocess.Popen(
                    ["cmd.exe", "/c", bat_path],
                    cwd=zip_dir,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                os._exit(0)
            elif system == "Darwin":
                is_app_bundle = "/Contents/MacOS/" in current_path
                if is_app_bundle and is_zip:
                    bundle_dir = current_path.split("/Contents/MacOS/")[0]
                    parent_dir = os.path.dirname(bundle_dir)
                    old_bundle = f"{bundle_dir}.old.{int(time.time())}"
                    updater_path = os.path.join(
                        tempfile.gettempdir(),
                        f"AutoFotello_updater_{os.getpid()}.sh",
                    )
                    bundle_name = os.path.basename(bundle_dir)
                    log_path = os.path.join(tempfile.gettempdir(), "AutoFotello_updater.log")
                    if not os.access(parent_dir, os.W_OK):
                        raise Exception(
                            f"Không có quyền cập nhật ứng dụng tại {parent_dir}. Hãy kéo AutoFotello.app vào Applications bằng Finder rồi thử lại."
                        )
                    with open(updater_path, "w", encoding="utf-8") as sf:
                        sf.write(
                            f'#!/bin/bash\nset -u\n'
                            f'ZIP_PATH={json.dumps(new_temp_path)}\n'
                            f'BUNDLE_DIR={json.dumps(bundle_dir)}\n'
                            f'OLD_BUNDLE={json.dumps(old_bundle)}\n'
                            f'PARENT_DIR={json.dumps(parent_dir)}\n'
                            f'BUNDLE_NAME={json.dumps(bundle_name)}\n'
                            f'LOG_PATH={json.dumps(log_path)}\n\n'
                            f'exec >> "$LOG_PATH" 2>&1\n'
                            f'echo "=== AutoFotello updater $(date) ==="\n'
                            f'echo "ZIP_PATH=$ZIP_PATH"\n'
                            f'echo "BUNDLE_DIR=$BUNDLE_DIR"\n'
                            f'echo "PARENT_DIR=$PARENT_DIR"\n\n'
                            f'cleanup_script() {{\n  rm -f "$0"\n}}\n'
                            f'trap cleanup_script EXIT\n\n'
                            f'sleep 2\nrm -rf "$OLD_BUNDLE"\n'
                            f'if [ -d "$BUNDLE_DIR" ]; then\n'
                            f'  if ! mv "$BUNDLE_DIR" "$OLD_BUNDLE"; then\n'
                            f'    echo "Cannot move current bundle to backup"\n    exit 1\n  fi\nfi\n\n'
                            f'if ! /usr/bin/unzip -oq "$ZIP_PATH" -d "$PARENT_DIR"; then\n'
                            f'  echo "Unzip failed; rolling back"\n  rm -rf "$BUNDLE_DIR"\n'
                            f'  if [ -d "$OLD_BUNDLE" ]; then\n    mv "$OLD_BUNDLE" "$BUNDLE_DIR"\n  fi\n'
                            f'  rm -f "$ZIP_PATH"\n  exit 1\nfi\n\n'
                            f'rm -f "$ZIP_PATH"\n'
                            f'if [ -d "$PARENT_DIR/$BUNDLE_NAME" ]; then\n'
                            f'  chmod +x "$PARENT_DIR/$BUNDLE_NAME/Contents/MacOS/"* 2>/dev/null || true\n'
                            f'else\n  echo "Updated bundle not found after unzip; rolling back"\n'
                            f'  if [ -d "$OLD_BUNDLE" ]; then\n    rm -rf "$BUNDLE_DIR"\n    mv "$OLD_BUNDLE" "$BUNDLE_DIR"\n  fi\n'
                            f'  exit 1\nfi\n\n'
                            f'if ! /usr/bin/open -n "$PARENT_DIR/$BUNDLE_NAME"; then\n'
                            f'  echo "Open command failed, but updated app exists. Removing backup to avoid doubled app size."\nfi\n\n'
                            f'sleep 5\nrm -rf "$OLD_BUNDLE"\n'
                        )
                    os.chmod(updater_path, 0o755)
                    subprocess.Popen(
                        ["/bin/bash", updater_path],
                        cwd=tempfile.gettempdir(),
                        start_new_session=True,
                    )
                    os._exit(0)
                else:
                    if is_zip:
                        zip_dir = os.path.dirname(current_path)
                        extracted_bin = None
                        with zipfile.ZipFile(new_temp_path, "r") as zip_ref:
                            for name in zip_ref.namelist():
                                if not name.endswith("/") and "/" not in name:
                                    zip_ref.extract(name, zip_dir)
                                    extracted_bin = os.path.abspath(os.path.join(zip_dir, name))
                                    break
                        try:
                            os.remove(new_temp_path)
                        except Exception:
                            pass
                        if extracted_bin and os.path.exists(extracted_bin):
                            os.rename(extracted_bin, new_temp_path)
                        else:
                            raise Exception("Không tìm thấy tệp thực thi bên trong file zip cập nhật.")

                    old_path = current_path + ".old"
                    if os.path.exists(old_path):
                        try:
                            os.remove(old_path)
                        except Exception:
                            pass
                    if os.path.exists(current_path):
                        os.rename(current_path, old_path)
                    os.rename(new_temp_path, current_path)
                    os.chmod(current_path, 0o755)
                    os.execv(sys.executable, [sys.executable] + sys.argv[1:])
        except Exception as e:
            print(f"[AUTO_UPDATE] Error during update execution: {e}")
            if new_temp_path and os.path.exists(new_temp_path):
                try:
                    os.remove(new_temp_path)
                except Exception:
                    pass
            notify_update_failed(str(e))

    def check_auth_status(self):
        hwid = self.get_hwid()
        return {
            "success": True,
            "needs_registration": False,
            "is_banned": False,
            "payment_required": False,
            "trial_active": False,
            "trial_remaining": "",
            "fotello_unlocked": True,
            "autohdr_unlocked": True,
            "autoenhance_unlocked": True,
            "fotello_remaining": "Vĩnh viễn",
            "autohdr_remaining": "Vĩnh viễn",
            "autoenhance_remaining": "Vĩnh viễn",
            "hwid": hwid,
            "is_admin": True,
            "fotello_license_active": True,
            "autohdr_license_active": True,
            "autoenhance_license_active": True,
            "fotello_permanent": True,
            "autohdr_permanent": True,
            "autoenhance_permanent": True,
            "fotello_trial_active": False,
            "autohdr_trial_active": False,
            "autoenhance_trial_active": False,
            "cloud_credits": 999999,
        }

    def _check_service_auth(self, service):
        return True
    def bind_bracket_drops(self, selectors):
        def _extract_paths(event):
            dropped = (event or {}).get("dataTransfer", {}).get("files", [])
            paths = []
            for item in dropped:
                path = item.get("pywebviewFullPath") if isinstance(item, dict) else None
                if path and os.path.isfile(path):
                    paths.append(os.path.abspath(path))
            return paths

        def make_handler(sel):
            def _handler(event):
                paths = _extract_paths(event)
                if paths and window:
                    window.evaluate_js(
                        f"handleNativeBracketDroppedFiles({json.dumps(sel)}, {json.dumps(paths, ensure_ascii=False)})"
                    )

            return _handler

        for sel in selectors:
            try:
                el = window.dom.get_element(sel)
                if el:
                    el.on("drop", make_handler(sel))
            except Exception as e:
                print(f"[DROP] Failed to bind drop for {sel}: {e}")

    def open_folder(self, folder_path):
        try:
            if str(folder_path or "").startswith(("http://", "https://")):
                import webbrowser
                webbrowser.open(str(folder_path))
                return {"ok": True}
            if sys.platform == "darwin":
                subprocess.Popen(["open", folder_path])
            elif sys.platform == "win32":
                subprocess.Popen(["explorer", folder_path])
            else:
                subprocess.Popen(["xdg-open", folder_path])
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def browse_folder(self):
        start_dir = _dialog_start_dir()
        if window:
            try:
                dialog_api = getattr(webview, "FileDialog", None)
                folder_dialog = dialog_api.FOLDER if dialog_api is not None else webview.FOLDER_DIALOG
                try:
                    result = window.create_file_dialog(folder_dialog, directory=start_dir)
                except TypeError:
                    result = window.create_file_dialog(folder_dialog)
                if result and len(result) > 0:
                    _remember_dialog_path(result[0])
                    return result[0]
            except Exception as e:
                print(f"[FILE_DIALOG] native folder picker failed: {e}")

        if sys.platform == "darwin":
            try:
                script_lines = [
                    f"set startFolder to POSIX file {_applescript_string(start_dir or str(Path.home()))} as alias",
                    'set chosenFolder to choose folder with prompt "Chọn thư mục" default location startFolder',
                    "return POSIX path of chosenFolder",
                ]
                proc = subprocess.run(
                    ["/usr/bin/osascript", *sum((["-e", line] for line in script_lines), [])],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if proc.returncode == 0:
                    path = proc.stdout.strip()
                    if path.endswith("/") and len(path) > 1:
                        path = path[:-1]
                    if path:
                        _remember_dialog_path(path)
                        return path
                elif proc.stderr and "User canceled" not in proc.stderr:
                    print(f"[FILE_DIALOG] macOS folder picker stderr: {proc.stderr.strip()}")
            except Exception as e:
                print(f"[FILE_DIALOG] macOS folder picker failed: {e}")
        return None

    def browse_files(self, mode="upload"):
        mode = str(mode or "upload").strip().lower()
        start_dir = _dialog_start_dir()
        file_types = (
            ("Upscale images (*.jpg;*.jpeg;*.png;*.webp)",)
            if mode == "upscale"
            else ("Image files (*.jpg;*.jpeg;*.png;*.tif;*.tiff;*.bmp;*.webp;*.cr2;*.cr3;*.nef;*.arw;*.dng;*.raw;*.heic;*.heif)",)
        )
        if window:
            try:
                dialog_api = getattr(webview, "FileDialog", None)
                open_dialog = dialog_api.OPEN if dialog_api is not None else webview.OPEN_DIALOG
                try:
                    result = window.create_file_dialog(
                        open_dialog,
                        directory=start_dir,
                        allow_multiple=True,
                        file_types=file_types,
                    )
                except TypeError:
                    result = window.create_file_dialog(
                        open_dialog,
                        allow_multiple=True,
                        file_types=file_types,
                    )
                if result:
                    first_path = list(result)[0]
                    _remember_dialog_path(first_path)
                    return list(result)
            except Exception as e:
                print(f"[FILE_DIALOG] native file picker failed: {e}")

        if sys.platform == "darwin":
            try:
                prompt = "Chọn ảnh upscale" if mode == "upscale" else "Chọn ảnh"
                script_lines = [
                    f"set startFolder to POSIX file {_applescript_string(start_dir or str(Path.home()))} as alias",
                    f'set chosenFiles to choose file with prompt "{prompt}" default location startFolder with multiple selections allowed',
                    'set outText to ""',
                    "repeat with chosenFile in chosenFiles",
                    "set outText to outText & POSIX path of chosenFile & linefeed",
                    "end repeat",
                    "return outText",
                ]
                proc = subprocess.run(
                    ["/usr/bin/osascript", *sum((["-e", line] for line in script_lines), [])],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if proc.returncode == 0:
                    paths = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
                    if paths:
                        _remember_dialog_path(paths[0])
                        return paths
                elif proc.stderr and "User canceled" not in proc.stderr:
                    print(f"[FILE_DIALOG] macOS file picker stderr: {proc.stderr.strip()}")
            except Exception as e:
                print(f"[FILE_DIALOG] macOS file picker failed: {e}")
        return []

    def start_connection_flow(self, service):
        if not self._check_service_auth(service):
            if window:
                window.evaluate_js("onConnectionFailed('Bản quyền hết hạn hoặc chưa được kích hoạt')")
            return {"ok": False, "msg": "Bản quyền hết hạn"}

        if not find_chrome():
            js_log("❌ Không tìm thấy Google Chrome trên máy tính!", "error")
            js_log("👉 Vui lòng tải và cài đặt Chrome tại: https://www.google.com/chrome", "warn")
            if window:
                window.evaluate_js("onConnectionFailed('Không tìm thấy Google Chrome')")
            return {"ok": False, "msg": "Chrome không được cài đặt"}

        def _run():
            global autoenhance_token_val, autoenhance_connected, autohdr_cookie_val, autohdr_connected
            if service == "fotello":
                target_url = "https://app.fotello.co"
            elif service == "autoenhance":
                target_url = "https://app.autoenhance.ai"
            else:
                target_url = "https://www.autohdr.com"

            js_log("Đang khởi chạy Chrome với Remote Debugging...", "info")
            if not do_open_chrome(target_url):
                if window:
                    window.evaluate_js("onConnectionFailed('Không khởi chạy được Chrome')")
                return

            debug_port = settings.get("port", 9222)
            chrome_ready = wait_for_chrome_debugging(debug_port, timeout=60, interval=0.5)
            if not chrome_ready:
                js_log(f"❌ Chrome không mở cổng Remote Debugging {debug_port} sau 60 giây.", "error")
                if window:
                    window.evaluate_js(f"onConnectionFailed('Chrome không mở cổng Remote Debugging {debug_port}')")
                return

            js_log(f"Đang liên kết Chrome qua DevTools. Vui lòng đăng nhập {service.upper()}...", "info")
            start_time = time.time()
            timeout = 300
            success = False
            cached_tab = None

            while time.time() - start_time < timeout:
                if not check_chrome_responsive(settings.get("port", 9222)):
                    js_log("Google Chrome đã bị đóng. Dừng luồng liên kết đăng nhập.", "warn")
                    break
                try:
                    cached_tab = cdp_find_or_open_tab(service, autocreate=cached_tab is None)
                    if not cached_tab:
                        time.sleep(2)
                        continue

                    if service == "fotello":
                        fotello_tabs = cdp_matching_tabs("fotello")
                        if cached_tab and all(tab.get("id") != cached_tab.get("id") for tab in fotello_tabs):
                            fotello_tabs.insert(0, cached_tab)
                        token_data = None
                        for fotello_tab in fotello_tabs:
                            token_data = cdp_extract_fotello_token(fotello_tab)
                            if token_data:
                                cached_tab = fotello_tab
                                break
                        if token_data and cdp_apply_fotello_token(token_data):
                            email = token_data.get("email", "unknown") or "unknown"
                            js_log(f"✔ Đăng nhập Fotello thành công! (email: {email})", "success")
                            success = True
                            break
                    elif service == "autoenhance":
                        token_str = cdp_extract_autoenhance_token(cached_tab)
                        if token_str:
                            ok, msg, _ = autoenhance.validate_token(token_str)
                            if ok:
                                autoenhance_token_val = token_str
                                autoenhance_connected = True
                                _save_autoenhance_token(token_str)
                                js_log("✔ Đăng nhập Autoenhance thành công!", "success")
                                success = True
                                break
                    else:
                        cookie_str = cdp_get_cookie_header(cached_tab, "autohdr.com")
                        if cookie_str:
                            ok, msg, new_cookie = autohdr._validate_autohdr_cookie(cookie_str)
                            if not ok:
                                js_log(f"Đang chờ đăng nhập AutoHDR... ({msg})", "info")
                            if ok:
                                autohdr_cookie_val = new_cookie or cookie_str
                                autohdr_connected = True
                                _save_autohdr_cookie(autohdr_cookie_val)
                                js_log(f"✔ Đăng nhập AutoHDR thành công: {msg}", "success")
                                success = True
                                break
                except Exception as e:
                    js_log(f"Đang chờ đăng nhập {service.upper()}... ({e})", "warn")
                    cached_tab = None

                time.sleep(3 if service == "autoenhance" else 2)

            if success:
                if window:
                    window.evaluate_js(f"onConnectionSuccess('{service}')")
            else:
                if window:
                    window.evaluate_js("onConnectionFailed('Hết thời gian chờ đăng nhập')")

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True}

    def autohdr_check_email_credits(self, target_email: str):
        if not target_email or not target_email.strip():
            return {"ok": False, "msg": "Vui lòng nhập email."}
        target_email = target_email.strip()
        import requests
        is_id = target_email.isdigit()
        found_info = None
        try:
            with requests.Session() as sess:
                uid = None
                if is_id:
                    uid = int(target_email)
                else:
                    url = f"{autohdr._AUTOHDR_BASE}/api/proxy/users/resolve-id?email={target_email}"
                    r = sess.get(
                        url,
                        headers={"Accept": "application/json", "User-Agent": autohdr._DONOR_UA},
                        timeout=15,
                    )
                    if r.status_code == 200:
                        uid = r.json().get("id") or r.json().get("user_id")
                    if not uid:
                        fallback_url = f"{autohdr._AUTOHDR_BASE}/api/proxy/users"
                        r2 = sess.post(
                            fallback_url,
                            json={"email": target_email},
                            headers={"Accept": "application/json", "User-Agent": autohdr._DONOR_UA},
                            timeout=15,
                        )
                        if r2.status_code in (200, 201):
                            uid = r2.json().get("id") or r2.json().get("user_id")
                if uid:
                    found_info = autohdr._donor_fetch_user(sess, int(uid))
        except Exception as e:
            return {"ok": False, "msg": f"Lỗi kết nối: {e}"}

        if found_info:
            return {
                "ok": True,
                "msg": f"Tìm thấy: {found_info['email']} - ID: {found_info['id']} - Credit: {found_info['credits']}",
                "data": found_info,
            }
        return {"ok": False, "msg": f"Không tìm thấy email {target_email} trên hệ thống AutoHDR."}

    def fotello_reconnect(self):
        ok = fotello.fotello_reconnect_saved(js_log)
        return {"ok": ok}

    def fotello_status(self):
        return fotello.fotello_validate_session(js_log)

    def fotello_logout(self):
        try:
            _listings_cache["fotello"] = {"timestamp": 0, "data": None}
            fotello.fotello_logout()
            js_log("Đã đăng xuất Fotello.", "success")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def fotello_list_listings(self, force=False):
        if not self._check_service_auth("fotello"):
            return {"ok": False, "msg": "Bản quyền hết hạn"}
        now = time.time()
        cache = _listings_cache["fotello"]
        if not force and cache["data"] is not None and (now - cache["timestamp"] < 300):
            js_log("✔ Sử dụng danh sách listings Fotello từ cache.", "info")
            return {"ok": True, "listings": cache["data"]}
        try:
            listings = fotello.fotello_list_listings(js_log)
            cache["data"] = listings
            cache["timestamp"] = now
            return {"ok": True, "listings": listings}
        except Exception as e:
            err_str = str(e).lower()
            if any(k in err_str for k in ("unauthorized", "401", "403", "auth", "token", "expired")):
                js_log("⚠️ Phiên đăng nhập Fotello hết hạn khi tải danh sách. Vui lòng đăng nhập Fotello lại.", "warn")
                fotello._fotello_state["connected"] = False
            return {"ok": False, "msg": str(e)}

    def fotello_get_listing_details(self, listing_id):
        if not self._check_service_auth("fotello"):
            return {"ok": False, "msg": "Bản quyền hết hạn"}
        try:
            photos = fotello.fotello_get_listing_photos(listing_id, js_log)
            return {"ok": True, "photos": photos, "listing_id": listing_id}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def fotello_download_selected_photos(self, listing_id, enhance_ids, savedir):
        if not self._check_service_auth("fotello"):
            return {"ok": False, "msg": "Bản quyền hết hạn"}
        if not enhance_ids:
            return {"ok": False, "msg": "Không có ảnh nào được chọn"}

        stop_event = threading.Event()
        active_download_events.append(stop_event)
        session_id = f"dl_f_sel_{int(time.time() * 1000)}"
        session_name = f"DL Fotello ({len(enhance_ids)} ảnh)"
        register_task_session(session_id, stop_event, "download", name=session_name)
        js_status("running", "Đang tải...", "download", session_name)

        def _run():
            output_handle = None
            output_folder = savedir
            try:
                output_handle = _prepare_storage_output(savedir, session_id)
                output_folder = output_handle.local_path
                fotello.fotello_download_selected_enhances(
                    listing_id,
                    enhance_ids,
                    output_folder,
                    log=lambda msg, type="info": js_log(msg, type, session_id, session_name),
                    progress_fn=lambda cur, total, sid=session_id: js_progress(cur, total, sid),
                    is_cancelled=lambda: stop_event.is_set(),
                )
                _finalize_storage_output(output_handle, output_folder, session_id, session_name, stop_event)
            except Exception as e:
                js_log(f"Fotello lỗi: {e}", "error", session_id, session_name)
            finally:
                if stop_event in active_download_events:
                    active_download_events.remove(stop_event)
                unregister_task_session(session_id)
                if not active_download_events:
                    js_status(
                        "idle",
                        "Đã dừng" if stop_event.is_set() else "Hoàn tất",
                        "download",
                        session_name,
                    )
                if not stop_event.is_set() and output_handle is not None:
                    _show_output_location(session_id, output_handle, output_folder)

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True}

    def fotello_download(self, listing_ids, savedir, session_label=None):
        if not self._check_service_auth("fotello"):
            return {"ok": False, "msg": "Bản quyền hết hạn"}

        stop_event = threading.Event()
        active_download_events.append(stop_event)
        session_id = f"dl_f_{int(time.time() * 1000)}"
        session_name = str(session_label).strip() if session_label else f"DL Fotello ({len(listing_ids)})"
        register_task_session(session_id, stop_event, "download", name=session_name)
        js_log(f"📥 Đang khởi chạy luồng tải Fotello song song ({len(listing_ids)} dự án)...", "success", session_id, session_name)
        js_status("running", "Đang tải...", "download")

        def _run():
            output_handle = None
            output_folder = savedir
            try:
                output_handle = _prepare_storage_output(savedir, session_id)
                output_folder = output_handle.local_path
                fotello.fotello_batch_download(
                    listing_ids,
                    output_folder,
                    log=lambda msg, type="info": js_log(msg, type, session_id, session_name),
                    progress_fn=lambda cur, total, sid=session_id: js_progress(cur, total, sid),
                    is_cancelled=lambda: stop_event.is_set(),
                )
                _finalize_storage_output(output_handle, output_folder, session_id, session_name, stop_event)
            except Exception as e:
                js_log(f"❌ Fotello lỗi: {e}", "error", session_id, session_name)
                err_str = str(e).lower()
                if any(k in err_str for k in ("unauthorized", "401", "403", "auth", "token", "expired")):
                    js_log("⚠️ Phiên đăng nhập Fotello đã hết hạn. Vui lòng đăng nhập Fotello lại.", "warn", session_id, session_name)
                    fotello._fotello_state["connected"] = False
            finally:
                if stop_event in active_download_events:
                    active_download_events.remove(stop_event)
                unregister_task_session(session_id)
                if not active_download_events:
                    js_status(
                        "idle",
                        "Đã dừng" if stop_event.is_set() else "Hoàn tất",
                        "download",
                        session_name,
                    )
                if not stop_event.is_set() and output_handle is not None:
                    _show_output_location(session_id, output_handle, output_folder)

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True}

    def fotello_analyze_brackets(self, input_dir, bracket_size=3, grouping_mode="order_fixed"):
        if not self._check_service_auth("fotello"):
            return {"ok": False, "msg": "Bản quyền hết hạn"}
        try:
            js_log("🔎 Đang phân tích ảnh để gom nhóm...", "info")
            session_id = f"analyze_f_{int(time.time() * 1000)}"
            prepared_input = _prepare_storage_input(input_dir, session_id, "Phân tích Fotello")
            brackets = fotello.fotello_analyze_brackets(
                prepared_input,
                bracket_size,
                log=lambda msg, type="info": js_log(msg, type),
                grouping_mode=grouping_mode,
            )
            return {"ok": True, "brackets": brackets}
        except Exception as e:
            js_log(f"❌ Lỗi phân tích: {e}", "error")
            return {"ok": False, "msg": str(e)}

    def get_thumbnail_base64(self, filepath):
        try:
            from pathlib import Path
            import base64
            from PIL import Image
            import io

            p = Path(filepath)
            if not p.exists():
                return None
            ext = p.suffix.lower()
            if ext in (".cr2", ".cr3", ".nef", ".arw", ".dng", ".raw"):
                try:
                    import rawpy
                    with rawpy.imread(str(p)) as raw:
                        thumb = raw.extract_thumb()
                    if thumb.format == rawpy.ThumbFormat.JPEG:
                        return "data:image/jpeg;base64," + base64.b64encode(thumb.data).decode("utf-8")
                    elif thumb.format == rawpy.ThumbFormat.BITMAP:
                        img = Image.fromarray(thumb.data)
                        img.thumbnail((80, 80))
                        buf = io.BytesIO()
                        img.save(buf, format="JPEG")
                        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("utf-8")
                except Exception as e:
                    print(f"Rawpy thumb error for {p}: {e}")
                    return None
            else:
                with Image.open(p) as img:
                    img.thumbnail((80, 80))
                    buf = io.BytesIO()
                    if img.mode in ("RGBA", "P"):
                        img = img.convert("RGB")
                    img.save(buf, format="JPEG")
                    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("utf-8")
        except Exception as e:
            print(f"Thumb error: {e}")
            return None

    def fotello_upload(self, input_dir, savedir, preferences, predefined_brackets=None):
        if not self._check_service_auth("fotello"):
            return {"ok": False, "msg": "Bản quyền hết hạn"}

        stop_event = threading.Event()
        active_upload_events.append(stop_event)
        session_id = f"up_f_{int(time.time() * 1000)}"
        project_name = (preferences or {}).get("project_name", "").strip() if isinstance(preferences, dict) else ""
        if not project_name:
            project_name = input_label(input_dir, "Upload")
        session_name = f"Fotello - {project_name}"
        register_task_session(session_id, stop_event, "upload", preferences, name=session_name)
        js_log("📤 Đang khởi chạy luồng Upload Fotello song song...", "success", session_id, session_name)
        js_status("running", "Đang xử lý...", "upload")

        def _run():
            output_handle = None
            output_folder = savedir
            try:
                output_handle = _prepare_storage_output(savedir, session_id)
                output_folder = output_handle.local_path
                prepared_input = _prepare_storage_input(input_dir, session_id, session_name, stop_event)
                result = fotello.fotello_upload_and_enhance(
                    prepared_input,
                    output_folder,
                    preferences=preferences,
                    predefined_brackets=predefined_brackets,
                    log=lambda msg, type="info": js_log(msg, type, session_id, session_name),
                    progress_fn=lambda cur, total, sid=session_id: js_progress(cur, total, sid),
                    status_fn=lambda text, sid=session_id: js_session_status(text, sid),
                    is_cancelled=lambda: stop_event.is_set(),
                )
                if isinstance(result, dict) and result.get("output_dir"):
                    output_folder = result["output_dir"]
                _finalize_storage_output(output_handle, output_folder, session_id, session_name, stop_event)
            except Exception as e:
                js_log(f"❌ Fotello lỗi: {e}", "error", session_id, session_name)
                err_str = str(e).lower()
                if any(k in err_str for k in ("unauthorized", "401", "403", "auth", "token", "expired")):
                    js_log("⚠️ Phiên đăng nhập Fotello đã hết hạn. Vui lòng đăng nhập Fotello lại.", "warn", session_id, session_name)
                    fotello._fotello_state["connected"] = False
            finally:
                if stop_event in active_upload_events:
                    active_upload_events.remove(stop_event)
                unregister_task_session(session_id)
                if not active_upload_events:
                    js_status(
                        "idle",
                        "Đã dừng" if stop_event.is_set() else "Hoàn tất",
                        "upload",
                        session_name,
                    )
                if not stop_event.is_set() and output_handle is not None:
                    _show_output_location(session_id, output_handle, output_folder)

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True}

    def autohdr_upload(self, input_dir, savedir, address, options=None):
        global autohdr_connected, autohdr_cookie_val
        processing_mode = (options or {}).get("processing_mode", "human_edit") if isinstance(options, dict) else "human_edit"
        if processing_mode != "donor":
            if not self._check_service_auth("autohdr"):
                return {"ok": False, "msg": "Bản quyền hết hạn"}

        if not autohdr_cookie_val and processing_mode != "donor":
            js_log("❌ Chưa đăng nhập AutoHDR. Hãy đăng nhập AutoHDR trước!", "error")
            return {"ok": False, "msg": "Chưa đăng nhập AutoHDR"}

        if processing_mode != "donor":
            try:
                cookie_ok, cookie_msg, refreshed_cookie = autohdr._validate_autohdr_cookie(autohdr_cookie_val)
                if not cookie_ok:
                    autohdr_connected = False
                    js_log("❌ Phiên đăng nhập AutoHDR đã hết hạn. Hãy đăng nhập AutoHDR lại.", "error")
                    return {"ok": False, "msg": "Phiên đăng nhập AutoHDR đã hết hạn"}
                autohdr_connected = True
                if refreshed_cookie and refreshed_cookie != autohdr_cookie_val:
                    autohdr_cookie_val = refreshed_cookie
                    _save_autohdr_cookie(autohdr_cookie_val)
            except Exception as e:
                if _is_explicit_autohdr_auth_error(e):
                    autohdr_connected = False
                    js_log("❌ Phiên đăng nhập AutoHDR đã hết hạn. Hãy đăng nhập AutoHDR lại.", "error")
                    return {"ok": False, "msg": "Phiên đăng nhập AutoHDR đã hết hạn"}
                js_log("❌ Không xác minh được phiên AutoHDR. Hãy kiểm tra mạng hoặc đăng nhập lại.", "error")
                return {"ok": False, "msg": "Không xác minh được phiên AutoHDR"}

        stop_event = threading.Event()
        active_upload_events.append(stop_event)
        session_id = f"up_a_{int(time.time() * 1000)}"
        project_name = str(address).strip() if address and str(address).strip() else input_label(input_dir, "Upload AutoHDR")
        session_name = f"AutoHDR - {project_name}"
        register_task_session(session_id, stop_event, "upload", options, name=session_name)
        js_log("📤 Đang khởi chạy luồng Upload AutoHDR song song...", "success", session_id, session_name)
        js_status("running", "Đang xử lý AutoHDR...", "upload")

        def _run():
            global autohdr_connected
            output_handle = None
            output_folder = savedir
            completed_successfully = False
            session_lock_acquired = False
            try:
                while not stop_event.is_set():
                    has_active_multi_style = False
                    waiting_for_names = []
                    with active_task_lock:
                        for sid, task in active_task_events.items():
                            if sid == session_id:
                                break
                            if task.get("type") == "upload":
                                opts = task.get("options") or {}
                                if opts.get("multi_style"):
                                    has_active_multi_style = True
                                    tname = task.get("name") or sid
                                    waiting_for_names.append(tname)
                    if not has_active_multi_style:
                        break
                    waiting_names_str = ", ".join(waiting_for_names)
                    js_log(
                        f"⏳ Đang xếp hàng (Queue): Đợi các job nhiều style trước đó ({waiting_names_str}) hoàn tất xử lý & tải ảnh về máy...",
                        "info",
                        session_id,
                        session_name,
                    )
                    for _ in range(15):
                        if stop_event.is_set():
                            break
                        time.sleep(1)

                if stop_event.is_set():
                    return

                profile_id = (options or {}).get("style_profile_id", "") if isinstance(options, dict) else ""
                if profile_id and profile_id in deferred_profile_updates:
                    deferred_profile = deferred_profile_updates.pop(profile_id, None)
                    if deferred_profile:
                        js_log(
                            f"📝 Đang tự động lưu cấu hình style profile '{(options or {}).get('style_profile_name', 'Mặc định')}' cho dự án này...",
                            "info",
                            session_id,
                            session_name,
                        )
                        res = self.autohdr_update_style_profile(profile_id, deferred_profile)
                        if res and res.get("ok"):
                            js_log("✅ Tự động lưu cấu hình style profile thành công!", "success", session_id, session_name)
                        else:
                            err_msg = (res or {}).get("msg") or "Lỗi không xác định"
                            js_log(
                                f"⚠️ Cảnh báo: Tự động lưu cấu hình style profile thất bại ({err_msg}). Vẫn tiếp tục xử lý...",
                                "warn",
                                session_id,
                                session_name,
                            )

                output_handle = _prepare_storage_output(savedir, session_id)
                output_folder = output_handle.local_path
                prepared_input = _prepare_storage_input(input_dir, session_id, session_name, stop_event)
                if options is None or not isinstance(options, dict):
                    pass
                else:
                    options["hwid"] = self.get_hwid()



                wait_started = time.time()
                last_queue_log = 0
                while not autohdr_session_lock.acquire(timeout=1):
                    if stop_event.is_set():
                        return
                    waited = int(time.time() - wait_started)
                    if waited - last_queue_log >= 15:
                        last_queue_log = waited
                        js_log(
                            f"⏳ Đang chạy tối đa {MAX_CONCURRENT_AUTOHDR_JOBS} luồng song song, dự án này đang chờ tới lượt ({waited}s)...",
                            "info",
                            session_id,
                            session_name,
                        )
                session_lock_acquired = True

                completed_successfully = bool(
                    autohdr.autohdr_upload_and_enhance(
                        cookie=autohdr_cookie_val,
                        input_dir=prepared_input,
                        savedir=output_folder,
                        address=address,
                        options=options,
                        log_fn=lambda msg, type="info": js_log(msg, type, session_id, session_name),
                        progress_fn=lambda cur, total, sid=session_id: js_progress(cur, total, sid),
                        stop_event=stop_event,
                    )
                )
                if completed_successfully:
                    _finalize_storage_output(output_handle, output_folder, session_id, session_name, stop_event)
            except Exception as e:
                js_log(f"❌ AutoHDR lỗi: {e}", "error", session_id, session_name)
                if _is_explicit_autohdr_auth_error(e):
                    js_log("⚠️ Phiên đăng nhập AutoHDR đã hết hạn. Vui lòng đăng nhập AutoHDR lại.", "warn", session_id, session_name)
                    autohdr_connected = False
            finally:
                if session_lock_acquired:
                    autohdr_session_lock.release()
                if stop_event in active_upload_events:
                    active_upload_events.remove(stop_event)
                unregister_task_session(session_id)
                if not active_upload_events:
                    final_status = "Hoàn tất" if completed_successfully else ("Đã dừng" if stop_event.is_set() else "Lỗi")
                    js_status("idle", final_status, "upload", session_name)
                if completed_successfully and not stop_event.is_set() and output_handle is not None:
                    _show_output_location(session_id, output_handle, output_folder)

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True}

    def fotello_stop(self):
        for ev in list(active_download_events):
            ev.set()
        active_download_events.clear()
        for ev in list(active_upload_events):
            ev.set()
        active_upload_events.clear()
        with active_task_lock:
            active_task_events.clear()
        js_log("■ Đã dừng tất cả tác vụ đang chạy.", "warn")
        js_status("idle", "Đã dừng", "download")
        js_status("idle", "Đã dừng", "upload")

    def stop_all_tasks(self):
        return self.fotello_stop()

    def stop_session(self, session_id):
        task_type = stop_task_session(session_id)
        if not task_type:
            return {"ok": False, "msg": "Không tìm thấy tác vụ đang chạy"}
        if task_type == "download":
            js_status("running" if active_download_events else "idle", "Đang dừng luồng tải...", "download")
        elif task_type == "upload":
            js_status("running" if active_upload_events else "idle", "Đang dừng luồng xử lý...", "upload")
        return {"ok": True, "type": task_type}

    def upscale_local_status(self):
        try:
            return {"ok": True, **upscale.local_engine_status(hwid=self.get_hwid())}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def upscale_install_local_engine(self, engine_id=None):
        session_id = f"engine_{int(time.time() * 1000)}"
        session_name = "Local Engine"
        js_log("Dang tai engine upscale local...", "info", session_id, session_name)

        def _run():
            try:
                result = upscale.install_local_engine(
                    hwid=self.get_hwid(),
                    engine_id=engine_id,
                    log=lambda msg, type="info": js_log(msg, type, session_id, session_name),
                    progress_fn=lambda cur, total, sid=session_id: js_progress(cur, total, sid),
                )
                engine = result.get("engine") or {}
                js_log(f"Local engine da san sang: {engine.get('name') or engine.get('id')}", "success", session_id, session_name)
            except Exception as e:
                js_log(f"Tai local engine loi: {e}", "error", session_id, session_name)
            finally:
                js_status("idle", "Hoàn tất", "upload", session_name)

        js_status("running", "Dang tai local engine...", "upload")
        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "session_id": session_id}

    def start_free_upscale(self, files, output_dir):
        session_id = f"free_upscale_{int(time.time())}"

        def _run():
            try:
                if not files:
                    js_log("Vui lòng chọn ảnh trước.", "error", session_id, "Free Upscale")
                    return
                output_path = Path(output_dir) if output_dir else (Path.home() / "Downloads" / "Upscale_Output")
                output_path.mkdir(parents=True, exist_ok=True)
                js_log(f"Bắt đầu Free Upscale {len(files)} ảnh (Giới hạn mỗi ảnh ~15MB)...", "info", session_id, "Free Upscale")

                def _log(msg, level="info"):
                    js_log(msg, level, session_id, "Free Upscale")

                success_count = 0
                for i, f_path_str in enumerate(files):
                    f_path = Path(f_path_str)
                    if free_tools_engine.run_free_upscale(f_path, output_path, log_fn=_log):
                        success_count += 1
                    js_progress(i + 1, len(files), session_id)

                js_log(f"Hoàn tất Upscale Miễn phí {success_count}/{len(files)} ảnh.", "success", session_id, "Free Upscale")
                js_show_folder_btn(session_id, str(output_path))
            except Exception as e:
                js_log(f"Lỗi Upscale Miễn phí: {e}", "error", session_id, "Free Upscale")

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True}

    def start_dte(self, files, output_dir, options):
        session_id = f"free_dte_{int(time.time())}"

        def _run():
            try:
                if not files:
                    js_log("Vui lòng chọn ảnh trước.", "error", session_id, "Day to Evening")
                    return
                output_path = Path(output_dir) if output_dir else Path(files[0]).parent
                output_path.mkdir(parents=True, exist_ok=True)
                js_log(f"Bắt đầu tạo Virtual Twilight (DTE) cho {len(files)} ảnh...", "info", session_id, "Day to Evening")

                def _log(msg, level="info"):
                    js_log(msg, level, session_id, "Day to Evening")

                success_count = 0
                for i, f_path_str in enumerate(files):
                    f_path = Path(f_path_str)
                    if free_tools_engine.run_free_twilight(f_path, output_path, options, log_fn=_log):
                        success_count += 1
                    js_progress(i + 1, len(files), session_id)

                js_log(f"Hoàn tất tạo DTE {success_count}/{len(files)} ảnh.", "success", session_id, "Day to Evening")
                js_show_folder_btn(session_id, str(output_path))
            except Exception as e:
                js_log(f"Lỗi tạo DTE: {e}", "error", session_id, "Day to Evening")

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True}

    def runpod_upscale(
        self,
        files,
        output_dir,
        mode="upscale_2x",
        strength=0.75,
        provider="runpod",
        keep_original_size=True,
        blend_original=True,
        true_original_dir=None,
    ):
        stop_event = threading.Event()
        active_upload_events.append(stop_event)
        session_id = f"upscale_{int(time.time() * 1000)}"
        provider = str(provider or "runpod").strip().lower()
        if provider not in ("runpod", "local"):
            provider = "runpod"
        mode = str(mode or "upscale_2x").strip().lower()
        try:
            strength = float(strength)
        except Exception:
            strength = 0.75

        provider_name = "Local" if provider == "local" else "RunPod"
        if keep_original_size:
            mode_name = "Làm nét giữ size"
        elif mode == "upscale_4x":
            mode_name = "Upscale 4x"
        else:
            mode_name = "Upscale 2x"

        session_name = f"{provider_name} {mode_name} - {input_label(files, mode_name)}"
        register_task_session(session_id, stop_event, "upload", name=session_name)
        js_log(f"Dang khoi chay {mode_name} bang {provider_name}...", "success", session_id, session_name)
        js_status("running", "Dang xu ly anh...", "upload")

        def _run():
            result = None
            output_handle = None
            output_folder = output_dir
            try:
                output_handle = _prepare_storage_output(output_dir, session_id)
                output_folder = output_handle.local_path
                prepared_files = _prepare_storage_input(files, session_id, session_name, stop_event)
                prepared_true_original_dir = (
                    _prepare_storage_input(true_original_dir, f"{session_id}_true", session_name, stop_event)
                    if true_original_dir
                    else None
                )
                result = upscale.process_images(
                    prepared_files,
                    output_folder,
                    mode=mode,
                    strength=strength,
                    provider=provider,
                    keep_original_size=bool(keep_original_size),
                    blend_original=bool(blend_original),
                    true_original_dir=prepared_true_original_dir,
                    hwid=self.get_hwid(),
                    stop_event=stop_event,
                    log=lambda msg, type="info": js_log(msg, type, session_id, session_name),
                    progress_fn=lambda cur, total, sid=session_id: js_progress(cur, total, sid),
                    credit_callback=None,
                )
                js_log(
                    f"Hoan tat: da luu {result.get('saved', 0)}/{result.get('total', 0)} anh. "
                    f"Bo qua {result.get('skipped', 0)}, huy {result.get('cancelled', 0)}, loi {result.get('failed', 0)}.",
                    "success" if not result.get("failed") else "warn",
                    session_id,
                    session_name,
                )
                actual_output = (result or {}).get("output_dir") or output_folder
                if output_handle is not None and actual_output:
                    _finalize_storage_output(output_handle, actual_output, session_id, session_name, stop_event)
            except Exception as e:
                js_log(f"{provider_name} xu ly anh loi: {e}", "error", session_id, session_name)
            finally:
                if stop_event in active_upload_events:
                    active_upload_events.remove(stop_event)
                unregister_task_session(session_id)
                if not active_upload_events:
                    js_status(
                        "idle",
                        "Da dung" if stop_event.is_set() else "Hoàn tất",
                        "upload",
                        session_name,
                    )
                if not stop_event.is_set() and output_handle is not None:
                    _show_output_location(session_id, output_handle, (result or {}).get("output_dir") or output_folder)

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "session_id": session_id}

    def detail_merge(
        self,
        original_dir,
        ai_dir,
        output_dir,
        strength=0.38,
        denoise=True,
        align=True,
        restore=True,
        detail_mode="normal",
        white_balance=True,
        highlight=0.0,
        shadow=0.0,
    ):
        if not original_dir or not ai_dir or not output_dir:
            return {"ok": False, "msg": "Vui lòng chọn đủ thư mục ảnh gốc, ảnh AI và thư mục lưu."}

        session_id = f"merge_{int(time.time() * 1000)}"
        session_name = "Merge chi tiết"
        stop_event = threading.Event()
        register_task_session(session_id, stop_event, "merge", name=session_name)
        js_log("Dang khoi chay merge chi tiet...", "success", session_id, session_name)
        js_status("running", "Dang merge chi tiet...", "merge", session_name)

        def _run():
            result = None
            output_handle = None
            output_folder = output_dir
            try:
                output_handle = _prepare_storage_output(output_dir, session_id)
                output_folder = output_handle.local_path
                prepared_original = _prepare_storage_input(original_dir, f"{session_id}_original", session_name, stop_event)
                prepared_ai = _prepare_storage_input(ai_dir, f"{session_id}_ai", session_name, stop_event)
                result = detail_merge.merge_folders(
                    original_dir=prepared_original,
                    ai_dir=prepared_ai,
                    output_dir=output_folder,
                    strength=float(strength),
                    denoise=bool(denoise),
                    align=bool(align),
                    restore=bool(restore),
                    detail_mode=str(detail_mode or "normal"),
                    white_balance=bool(white_balance),
                    stop_event=stop_event,
                    log=lambda msg, type="info": js_log(msg, type, session_id, session_name),
                    progress_fn=lambda cur, total, sid=session_id: js_progress(cur, total, sid),
                    highlight=float(highlight),
                    shadow=float(shadow),
                )
                js_log(
                    f"Hoan tat merge: {result.get('merged', 0)}/{result.get('total', 0)} anh. "
                    f"Bo qua {result.get('skipped', 0)}, loi {result.get('failed', 0)}.",
                    "success" if not result.get("failed") else "warn",
                    session_id,
                    session_name,
                )
                actual_output = (result or {}).get("output_dir") or output_folder
                if output_handle is not None and actual_output:
                    _finalize_storage_output(output_handle, actual_output, session_id, session_name, stop_event)
            except Exception as e:
                js_log(f"Merge chi tiet loi: {e}", "error", session_id, session_name)
            finally:
                unregister_task_session(session_id)
                if has_active_task_type("merge"):
                    js_status("running", "Dang merge chi tiet...", "merge", session_name)
                else:
                    js_status(
                        "idle",
                        "Da dung" if stop_event.is_set() else "Hoàn tất",
                        "merge",
                        session_name,
                    )
                if not stop_event.is_set() and output_handle is not None:
                    _show_output_location(session_id, output_handle, (result or {}).get("output_dir") or output_folder)
                try:
                    import shutil
                    from pathlib import Path
                    if output_folder:
                        temp_dir = Path(output_folder) / "Original_HDR_Temp"
                        if temp_dir.exists():
                            shutil.rmtree(temp_dir, ignore_errors=True)
                            js_log("Đã dọn dẹp thư mục HDR tạm thời.", "info", session_id, session_name)
                except Exception:
                    pass

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "session_id": session_id}

    def detail_merge_analyze_brackets(self, original_dir, bracket_size=3, grouping_mode="order_fixed"):
        if not original_dir:
            return {"ok": False, "msg": "Vui lòng chọn thư mục hoặc danh sách ảnh gốc."}
        try:
            session_id = f"analyze_m_{int(time.time() * 1000)}"
            prepared_original = _prepare_storage_input(original_dir, session_id, "Phân tích Merge")
            brackets = detail_merge.analyze_brackets(
                prepared_original,
                bracket_size=bracket_size,
                grouping_mode=grouping_mode,
            )
            return {"ok": True, "brackets": brackets}
        except Exception as e:
            js_log(f"❌ Lỗi phân tích bracket merge: {e}", "error")
            return {"ok": False, "msg": str(e)}

    def pre_fuse_brackets(self, brackets, output_dir=None):
        if not brackets or not isinstance(brackets, list):
            return {"ok": False, "msg": "Không có dữ liệu bracket để gộp."}
        try:
            session_id = f"prefuse_{int(time.time() * 1000)}"
            session_name = "Gộp Bracket HDR tạm thời"
            stop_event = threading.Event()
            register_task_session(session_id, stop_event, "merge", name=session_name)
            js_status("running", "Đang gộp HDR...", "merge", session_name)

            def _run():
                from storage_manager import _run_cache_dir
                import shutil
                from pathlib import Path

                try:
                    if output_dir and str(output_dir).strip():
                        temp_dir = Path(output_dir) / "Original_HDR_Temp"
                    else:
                        temp_dir = _run_cache_dir(session_id) / "fused_hdr"

                    if temp_dir.exists():
                        shutil.rmtree(temp_dir, ignore_errors=True)
                    temp_dir.mkdir(parents=True, exist_ok=True)

                    fused_paths = []
                    total = len(brackets)
                    for idx, group in enumerate(brackets):
                        if stop_event.is_set():
                            break
                        if not group:
                            continue
                        js_progress(idx, total, session_id)
                        js_log(f"Đang gộp HDR nhóm {idx + 1}/{total}...", "info", session_id, session_name)
                        original, _ = detail_merge._load_original_entry(
                            group,
                            log=lambda msg, type="info": js_log(msg, type, session_id, session_name),
                        )
                        if original is not None:
                            lead_path = Path(group[0])
                            output_path = detail_merge._output_path_for_ai(lead_path, temp_dir)
                            detail_merge._save_image(output_path, original, lead_path)
                            fused_paths.append(str(output_path))

                    js_progress(total, total, session_id)
                    js_log("Đã hoàn tất gộp HDR tạm thời.", "success", session_id, session_name)
                    js_status("idle", "Hoàn tất", "merge", session_name)
                    if window:
                        import json
                        window.evaluate_js(
                            f"onPreFuseComplete({json.dumps({'ok': True, 'temp_dir': str(temp_dir), 'fused_paths': fused_paths})})"
                        )
                except Exception as e:
                    js_log(f"❌ Lỗi gộp HDR tạm thời: {e}", "error", session_id, session_name)
                    js_status("idle", "Lỗi", "merge", session_name)
                    if window:
                        import json
                        window.evaluate_js(
                            f"onPreFuseComplete({json.dumps({'ok': False, 'msg': str(e)})})"
                        )

            threading.Thread(target=_run, daemon=True).start()
            return {"ok": True, "session_id": session_id}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def autohdr_test_cookie(self, cookie=None):
        cookie_to_test = cookie or autohdr_cookie_val
        try:
            ok, msg, new_cookie = autohdr._validate_autohdr_cookie(cookie_to_test)
            return {"ok": ok, "msg": msg}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def autohdr_reconnect(self):
        global autohdr_connected, autohdr_cookie_val
        if autohdr_cookie_val:
            try:
                ok, msg, new_cookie = autohdr._validate_autohdr_cookie(autohdr_cookie_val)
                if ok:
                    autohdr_connected = True
                    if new_cookie and new_cookie != autohdr_cookie_val:
                        autohdr_cookie_val = new_cookie
                        _save_autohdr_cookie(autohdr_cookie_val)
                    return {"ok": True}
            except Exception as e:
                response = getattr(e, "response", None)
                status_code = getattr(response, "status_code", None)
                if status_code not in (401, 403):
                    return {
                        "ok": False,
                        "transient_error": True,
                        "msg": f"Chưa kiểm tra được phiên AutoHDR: {e}",
                    }
        autohdr_connected = False
        return {"ok": False, "auth_expired": bool(autohdr_cookie_val)}

    def autoenhance_status(self):
        global autoenhance_connected, autoenhance_token_val
        if not autoenhance_token_val:
            autoenhance_token_val = _load_autoenhance_token()
        if not autoenhance_token_val:
            autoenhance_connected = False
            return {
                "connected": False,
                "has_saved_cookie": False,
                "msg": "Chưa đăng nhập Autoenhance",
            }
        try:
            ok, msg, new_token = autoenhance.validate_token(autoenhance_token_val)
            autoenhance_connected = ok
            if ok and isinstance(new_token, str) and new_token and new_token != autoenhance_token_val:
                autoenhance_token_val = new_token
                _save_autoenhance_token(autoenhance_token_val)
            return {
                "connected": ok,
                "has_saved_cookie": True,
                "msg": msg if ok else f"Phiên đăng nhập Autoenhance đã hết hạn: {msg}",
                "auth_expired": not ok,
                "transient_error": False,
            }
        except Exception as e:
            return {
                "connected": autoenhance_connected,
                "has_saved_cookie": True,
                "auth_expired": False,
                "transient_error": True,
                "msg": f"Autoenhance tạm thời chưa phản hồi, giữ nguyên phiên đăng nhập: {e}",
            }

    def autoenhance_reconnect(self):
        global autoenhance_connected, autoenhance_token_val
        if not autoenhance_token_val or not isinstance(autoenhance_token_val, str):
            autoenhance_token_val = _load_autoenhance_token()
        if autoenhance_token_val:
            try:
                ok, msg, new_token = autoenhance.validate_token(autoenhance_token_val)
                if ok:
                    autoenhance_connected = True
                    if isinstance(new_token, str) and new_token and new_token != autoenhance_token_val:
                        autoenhance_token_val = new_token
                        _save_autoenhance_token(autoenhance_token_val)
                    return {"ok": True}
                return {"ok": False, "auth_expired": True}
            except Exception as e:
                autoenhance_connected = False
                js_log(f"⚠ Chưa kết nối được Autoenhance (sẽ tự động khôi phục khi có mạng): {e}", "warn")
                return {
                    "ok": False,
                    "transient_error": True,
                    "msg": f"Chưa kiểm tra được phiên Autoenhance: {e}",
                }
        autoenhance_connected = False
        return {"ok": False, "auth_expired": False}

    def autoenhance_logout(self):
        global autoenhance_token_val, autoenhance_connected
        autoenhance_token_val = ""
        autoenhance_connected = False
        _clear_autoenhance_token()
        return {"ok": True}

    def autoenhance_list_listings(self):
        if not self._check_service_auth("autoenhance"):
            return {"ok": False, "msg": "Bản quyền không hợp lệ hoặc hết hạn."}
        if not autoenhance_token_val:
            return {"ok": False, "msg": "Vui lòng đăng nhập trước"}
        try:
            listings = autoenhance.list_orders(autoenhance_token_val, log_fn=js_log)
            return {"ok": True, "data": listings}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def autoenhance_get_listing_details(self, order_id):
        if not autoenhance_token_val:
            return {"ok": False, "msg": "Chưa đăng nhập"}
        try:
            details = autoenhance.get_order_details(autoenhance_token_val, order_id, log_fn=js_log)
            images = details.get("images", [])
            photos = []
            for img in images:
                photos.append({
                    "id": img.get("image_id"),
                    "name": img.get("image_name"),
                    "url": img.get("url"),
                    "thumb_url": img.get("url"),
                    "status": img.get("status", "processed"),
                })
            return {
                "ok": True,
                "photos": photos,
                "photoshoot_id": order_id,
                "user_id": "",
            }
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def autoenhance_get_image_thumbnail_base64(self, image_id):
        if not autoenhance_token_val:
            return {"ok": False, "msg": "Chưa đăng nhập"}
        try:
            import base64
            url = f"https://api.autoenhance.ai/v3/images/{image_id}/enhanced?format=jpeg&watermark=false&preview=true&quality=60&max_width=300"
            headers = {
                "x-api-key": autoenhance_token_val,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            }
            resp = autoenhance._SHARED_HTTP_SESSION.get(url, headers=headers, timeout=15)
            if resp.status_code == 200:
                b64_data = base64.b64encode(resp.content).decode("utf-8")
                return {"ok": True, "base64": f"data:image/jpeg;base64,{b64_data}"}
            return {"ok": False, "msg": f"Lỗi HTTP {resp.status_code}"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def autoenhance_upload(self, input_dir, output_dir, name="Upload", options=None):
        if not self._check_service_auth("autoenhance"):
            return {"ok": False, "msg": "Bản quyền hết hạn."}
        if not autoenhance_token_val:
            js_log("❌ Chưa đăng nhập Autoenhance. Hãy đăng nhập Autoenhance trước!", "error")
            return {"ok": False, "msg": "Chưa đăng nhập"}

        stop_event = threading.Event()
        active_upload_events.append(stop_event)
        session_id = f"up_ae_{int(time.time() * 1000)}"
        project_name = str(name).strip() if name and str(name).strip() else input_label(input_dir, "Upload Autoenhance")
        session_name = f"Autoenhance - {project_name}"
        register_task_session(session_id, stop_event, "upload", options, name=session_name)
        js_log("📤 Đang khởi chạy luồng Upload Autoenhance song song...", "success", session_id, session_name)
        js_status("running", "Đang xử lý Autoenhance...", "upload")

        def _run():
            global autoenhance_connected
            output_handle = None
            output_folder = output_dir
            try:
                output_handle = _prepare_storage_output(output_dir, session_id)
                output_folder = output_handle.local_path
                prepared_input = _prepare_storage_input(input_dir, session_id, session_name, stop_event)
                autoenhance.upload_and_process(
                    token=autoenhance_token_val,
                    input_dir=prepared_input,
                    savedir=output_folder,
                    order_name=project_name,
                    options=options,
                    log_fn=lambda msg, type="info": js_log(msg, type, session_id, session_name),
                    progress_fn=lambda cur, total, sid=session_id: js_progress(cur, total, sid),
                    stop_event=stop_event,
                )
                _finalize_storage_output(output_handle, output_folder, session_id, session_name, stop_event)
            except Exception as e:
                js_log(f"❌ Lỗi Autoenhance: {e}", "error", session_id, session_name)
                err_str = str(e).lower()
                if any(k in err_str for k in ("unauthorized", "401", "403", "token", "expired")):
                    js_log("⚠️ Phiên đăng nhập Autoenhance đã hết hạn. Vui lòng đăng nhập lại.", "warn", session_id, session_name)
                    autoenhance_connected = False
            finally:
                if stop_event in active_upload_events:
                    active_upload_events.remove(stop_event)
                unregister_task_session(session_id)
                if not active_upload_events:
                    js_status(
                        "idle",
                        "Đã dừng" if stop_event.is_set() else "Hoàn tất",
                        "upload",
                        session_name,
                    )
                if not stop_event.is_set() and output_handle is not None:
                    _show_output_location(session_id, output_handle, output_folder)

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True}

    def autoenhance_download(self, order_ids, output_dir):
        if not self._check_service_auth("autoenhance"):
            return {"ok": False, "msg": "Bản quyền hết hạn."}
        if not autoenhance_token_val:
            js_log("❌ Chưa đăng nhập Autoenhance. Hãy đăng nhập Autoenhance trước!", "error")
            return {"ok": False, "msg": "Chưa đăng nhập"}
        if not order_ids:
            return {"ok": False}

        stop_event = threading.Event()
        active_download_events.append(stop_event)
        session_id = f"dl_ae_{int(time.time() * 1000)}"
        session_name = f"DL Autoenhance ({len(order_ids)})"
        register_task_session(session_id, stop_event, "download", name=session_name)
        js_log(f"📥 Đang khởi chạy luồng tải Autoenhance song song ({len(order_ids)} dự án)...", "success", session_id, session_name)
        js_status("running", "Đang tải Autoenhance...", "download")

        def _run():
            global autoenhance_connected
            output_handle = None
            output_folder = output_dir
            try:
                output_handle = _prepare_storage_output(output_dir, session_id)
                output_folder = output_handle.local_path
                autoenhance.batch_download(
                    token=autoenhance_token_val,
                    order_ids=order_ids,
                    savedir=output_folder,
                    log_fn=lambda msg, type="info": js_log(msg, type, session_id, session_name),
                    progress_fn=lambda cur, total, sid=session_id: js_progress(cur, total, sid),
                    stop_event=stop_event,
                )
                _finalize_storage_output(output_handle, output_folder, session_id, session_name, stop_event)
            except Exception as e:
                js_log(f"❌ Lỗi Tải xuống Autoenhance: {e}", "error", session_id, session_name)
                err_str = str(e).lower()
                if any(k in err_str for k in ("unauthorized", "401", "403", "token", "expired")):
                    js_log("⚠️ Phiên đăng nhập Autoenhance đã hết hạn. Vui lòng đăng nhập lại.", "warn", session_id, session_name)
                    autoenhance_connected = False
            finally:
                if stop_event in active_download_events:
                    active_download_events.remove(stop_event)
                unregister_task_session(session_id)
                if not active_download_events:
                    js_status(
                        "idle",
                        "Đã dừng" if stop_event.is_set() else "Hoàn tất",
                        "download",
                        session_name,
                    )
                if not stop_event.is_set() and output_handle is not None:
                    _show_output_location(session_id, output_handle, output_folder)

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True}

    def autoenhance_download_selected_photos(self, order_id, photo_ids, output_dir):
        if not self._check_service_auth("autoenhance"):
            return {"ok": False, "msg": "Bản quyền hết hạn."}
        if not autoenhance_token_val:
            js_log("❌ Chưa đăng nhập Autoenhance. Hãy đăng nhập Autoenhance trước!", "error")
            return {"ok": False, "msg": "Chưa đăng nhập"}

        stop_event = threading.Event()
        active_download_events.append(stop_event)
        session_id = f"dl_ae_{int(time.time() * 1000)}"
        session_name = f"DL Autoenhance ({len(photo_ids)} ảnh)"
        register_task_session(session_id, stop_event, "download", name=session_name)
        js_log(f"📥 Đang khởi chạy luồng tải Autoenhance song song ({len(photo_ids)} ảnh được chọn)...", "success", session_id, session_name)
        js_status("running", "Đang tải Autoenhance...", "download")

        def _run():
            global autoenhance_connected
            output_handle = None
            output_folder = output_dir
            try:
                output_handle = _prepare_storage_output(output_dir, session_id)
                output_folder = output_handle.local_path
                autoenhance.batch_download(
                    token=autoenhance_token_val,
                    order_ids=[order_id],
                    savedir=output_folder,
                    log_fn=lambda msg, type="info": js_log(msg, type, session_id, session_name),
                    progress_fn=lambda cur, total, sid=session_id: js_progress(cur, total, sid),
                    stop_event=stop_event,
                    photo_ids=photo_ids,
                )
                _finalize_storage_output(output_handle, output_folder, session_id, session_name, stop_event)
            except Exception as e:
                js_log(f"❌ Lỗi Tải xuống Autoenhance: {e}", "error", session_id, session_name)
                err_str = str(e).lower()
                if any(k in err_str for k in ("unauthorized", "401", "403", "token", "expired")):
                    js_log("⚠️ Phiên đăng nhập Autoenhance đã hết hạn. Vui lòng đăng nhập lại.", "warn", session_id, session_name)
                    autoenhance_connected = False
            finally:
                if stop_event in active_download_events:
                    active_download_events.remove(stop_event)
                unregister_task_session(session_id)
                if not active_download_events:
                    js_status(
                        "idle",
                        "Đã dừng" if stop_event.is_set() else "Hoàn tất",
                        "download",
                        session_name,
                    )
                if not stop_event.is_set() and output_handle is not None:
                    _show_output_location(session_id, output_handle, output_folder)

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True}

    def autoenhance_stop(self):
        for ev in list(active_download_events):
            ev.set()
        active_download_events.clear()
        for ev in list(active_upload_events):
            ev.set()
        active_upload_events.clear()
        with active_task_lock:
            active_task_events.clear()
        js_log("■ Đã dừng tất cả tác vụ Autoenhance đang chạy.", "warn")
        return {"ok": True}

    def autohdr_status(self):
        global autohdr_connected, autohdr_cookie_val
        if not autohdr_cookie_val:
            autohdr_connected = False
            return {
                "connected": False,
                "has_saved_cookie": False,
                "msg": "Chưa đăng nhập AutoHDR",
            }
        try:
            ok, msg, new_cookie = autohdr._validate_autohdr_cookie(autohdr_cookie_val)
            autohdr_connected = ok
            if ok and new_cookie and new_cookie != autohdr_cookie_val:
                autohdr_cookie_val = new_cookie
                _save_autohdr_cookie(autohdr_cookie_val)
            import re
            user_id = None
            claimed_free_credits = False
            if ok and msg:
                match = re.search(r"\(id:\s*(\d+)\)", str(msg))
                if match:
                    user_id = match.group(1)
                    claimed_free_credits = user_id in _load_autohdr_claims()
            return {
                "connected": ok,
                "has_saved_cookie": True,
                "msg": msg if ok else f"Phiên đăng nhập AutoHDR đã hết hạn: {msg}",
                "auth_expired": not ok,
                "transient_error": False,
                "user_id": user_id,
                "claimed_free_credits": claimed_free_credits,
            }
        except Exception as e:
            response = getattr(e, "response", None)
            status_code = getattr(response, "status_code", None)
            auth_expired = status_code in (401, 403)
            if auth_expired:
                autohdr_connected = False
            return {
                "connected": False if auth_expired else autohdr_connected,
                "has_saved_cookie": True,
                "auth_expired": auth_expired,
                "transient_error": not auth_expired,
                "msg": (
                    f"Phiên đăng nhập AutoHDR đã hết hạn (HTTP {status_code})"
                    if auth_expired
                    else f"AutoHDR tạm thời chưa phản hồi, giữ nguyên phiên đăng nhập: {e}"
                ),
            }

    def autohdr_get_style_profiles(self):
        try:
            sess = requests.Session()
            auth = autohdr._api_get(sess, autohdr_cookie_val, "/api/auth/get-session")
            uid = autohdr._get_active_uid(auth)
            if not uid:
                return {"ok": False, "msg": "Chưa đăng nhập AutoHDR"}
            headers = {
                "Cookie": autohdr_cookie_val,
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            }
            resp = requests.get(f"https://www.autohdr.com/api/proxy/users/{uid}/style-profiles", headers=headers, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                return {"ok": True, "data": data}
            return {"ok": False, "msg": f"Lỗi lấy danh sách (HTTP {resp.status_code})"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def autohdr_get_available_models(self):
        try:
            sess = requests.Session()
            auth = autohdr._api_get(sess, autohdr_cookie_val, "/api/auth/get-session")
            uid = autohdr._get_active_uid(auth)
            if not uid:
                return {"ok": False, "msg": "Chưa đăng nhập AutoHDR"}
            headers = {
                "Cookie": autohdr_cookie_val,
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            }
            resp = requests.get(f"https://www.autohdr.com/api/proxy/users/{uid}/available-models", headers=headers, timeout=15)
            if resp.status_code == 200:
                return {"ok": True, "data": resp.json()}
            return {"ok": False, "msg": f"Lỗi lấy danh sách model (HTTP {resp.status_code}): {resp.text}"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def autohdr_get_style_settings(self):
        try:
            headers = {
                "Cookie": autohdr_cookie_val,
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            }
            resp = requests.get("https://www.autohdr.com/api/rpc/styleSettings/get", headers=headers, timeout=15)
            if resp.status_code == 200:
                return {"ok": True, "data": resp.json()}
            return {"ok": False, "msg": f"Lỗi lấy cấu hình style settings (HTTP {resp.status_code}): {resp.text}"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def autohdr_update_style_settings(self, payload):
        try:
            sess = requests.Session()
            auth = autohdr._api_get(sess, autohdr_cookie_val, "/api/auth/get-session")
            uid = autohdr._get_active_uid(auth)
            if not uid:
                return {"ok": False, "msg": "Chưa đăng nhập AutoHDR"}
            headers = {
                "Cookie": autohdr_cookie_val,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            }
            wrap_payload = {"json": {"userId": int(uid), "payload": payload}}
            resp = requests.post(
                "https://www.autohdr.com/api/rpc/styleSettings/update",
                json=wrap_payload,
                headers=headers,
                timeout=15,
            )
            if resp.status_code == 200:
                return {"ok": True, "data": resp.json()}
            return {"ok": False, "msg": f"Lỗi cập nhật cấu hình style settings (HTTP {resp.status_code}): {resp.text}"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def autohdr_create_style_profile(self, payload):
        try:
            sess = requests.Session()
            auth = autohdr._api_get(sess, autohdr_cookie_val, "/api/auth/get-session")
            uid = autohdr._get_active_uid(auth)
            if not uid:
                return {"ok": False, "msg": "Chưa đăng nhập AutoHDR"}
            headers = {
                "Cookie": autohdr_cookie_val,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            }
            resp = requests.post(
                f"https://www.autohdr.com/api/proxy/users/{uid}/style-profiles",
                json=payload,
                headers=headers,
                timeout=15,
            )
            if resp.status_code == 200:
                return {"ok": True, "data": resp.json()}
            return {"ok": False, "msg": f"Lỗi tạo profile (HTTP {resp.status_code}): {resp.text}"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def has_active_multi_style_job(self):
        with active_task_lock:
            for sid, task in active_task_events.items():
                if task.get("type") == "upload":
                    opts = task.get("options") or {}
                    if opts.get("multi_style"):
                        return True
        return False

    def has_active_autohdr_job(self):
        with active_task_lock:
            for sid, task in active_task_events.items():
                if task.get("type") == "upload":
                    return True
        return False

    def get_active_session_ids(self):
        with active_task_lock:
            return list(active_task_events.keys())

    def autohdr_defer_style_profile_update(self, profile_id, payload):
        deferred_profile_updates[str(profile_id)] = payload
        return {"ok": True}

    def autohdr_update_style_profile(self, profile_id, payload):
        try:
            sess = requests.Session()
            auth = autohdr._api_get(sess, autohdr_cookie_val, "/api/auth/get-session")
            uid = autohdr._get_active_uid(auth)
            if not uid:
                return {"ok": False, "msg": "Chưa đăng nhập AutoHDR"}
            headers = {
                "Cookie": autohdr_cookie_val,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            }
            resp = requests.put(
                f"https://www.autohdr.com/api/proxy/users/{uid}/style-profiles/{profile_id}",
                json=payload,
                headers=headers,
                timeout=15,
            )
            if resp.status_code == 200:
                return {"ok": True, "data": resp.json()}
            return {"ok": False, "msg": f"Lỗi cập nhật profile (HTTP {resp.status_code}): {resp.text}"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def autohdr_delete_style_profile(self, profile_id):
        try:
            sess = requests.Session()
            auth = autohdr._api_get(sess, autohdr_cookie_val, "/api/auth/get-session")
            uid = autohdr._get_active_uid(auth)
            if not uid:
                return {"ok": False, "msg": "Chưa đăng nhập AutoHDR"}
            headers = {
                "Cookie": autohdr_cookie_val,
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            }
            resp = requests.delete(
                f"https://www.autohdr.com/api/proxy/users/{uid}/style-profiles/{profile_id}",
                headers=headers,
                timeout=15,
            )
            if resp.status_code == 200:
                return {"ok": True, "data": resp.json()}
            return {"ok": False, "msg": f"Lỗi xoá profile (HTTP {resp.status_code}): {resp.text}"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def autohdr_logout(self):
        global autohdr_cookie_val, autohdr_connected
        try:
            _listings_cache["autohdr"] = {"timestamp": 0, "data": None}
            autohdr_cookie_val = ""
            autohdr_connected = False
            _clear_autohdr_cookie()
            js_log("Đã đăng xuất AutoHDR.", "success")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def autohdr_list_listings(self, force=False):
        global autohdr_connected
        if not self._check_service_auth("autohdr"):
            return {"ok": False, "msg": "Bản quyền hết hạn"}
        if not autohdr_cookie_val:
            return {"ok": False, "msg": "Chưa đăng nhập AutoHDR"}
        now = time.time()
        cache = _listings_cache["autohdr"]
        if not force and cache["data"] is not None and (now - cache["timestamp"] < 300):
            js_log("✔ Sử dụng danh sách photoshoot AutoHDR từ cache.", "info")
            return {"ok": True, "listings": cache["data"]}
        try:
            shoots = autohdr.autohdr_list_listings(autohdr_cookie_val, js_log)
            autohdr_connected = True
            cache["data"] = shoots
            cache["timestamp"] = now
            return {"ok": True, "listings": shoots}
        except Exception as e:
            if _is_explicit_autohdr_auth_error(e):
                js_log("⚠️ Phiên đăng nhập AutoHDR đã hết hạn khi tải danh sách. Vui lòng đăng nhập AutoHDR lại.", "warn")
                autohdr_connected = False
            return {"ok": False, "msg": str(e)}

    def autohdr_get_listing_details(self, listing_id):
        if not self._check_service_auth("autohdr"):
            return {"ok": False, "msg": "Bản quyền hết hạn"}
        if not autohdr_cookie_val:
            return {"ok": False, "msg": "Chưa đăng nhập AutoHDR"}
        try:
            details = autohdr.autohdr_get_listing_details(autohdr_cookie_val, listing_id, js_log)
            return {"ok": True, **details}
        except Exception as e:
            js_log(f"Không mở được listing AutoHDR: {e}", "error")
            return {"ok": False, "msg": str(e)}

    def autohdr_download_selected_photos(self, listing_id, photoshoot_id, user_id, selected_photos, savedir):
        if not self._check_service_auth("autohdr"):
            return {"ok": False, "msg": "Bản quyền hết hạn"}
        if not autohdr_cookie_val:
            js_log("❌ Chưa đăng nhập AutoHDR. Hãy đăng nhập AutoHDR trước!", "error")
            return {"ok": False, "msg": "Chưa đăng nhập AutoHDR"}
        if not selected_photos:
            return {"ok": False, "msg": "Không có ảnh nào được chọn"}

        stop_event = threading.Event()
        active_download_events.append(stop_event)
        session_id = f"dl_sel_{int(time.time() * 1000)}"
        session_name = f"Tải {len(selected_photos)} ảnh AutoHDR"
        register_task_session(session_id, stop_event, "download", name=session_name)
        js_log(f"📥 Đang khởi chạy luồng tải {len(selected_photos)} ảnh AutoHDR...", "success", session_id, session_name)
        js_status("running", "Đang tải...", "download")

        def _run():
            output_handle = None
            output_folder = savedir
            try:
                output_handle = _prepare_storage_output(savedir, session_id)
                output_folder = output_handle.local_path
                photo_ids = [
                    p.get("id") if isinstance(p, dict) else p
                    for p in selected_photos
                    if (p.get("id") if isinstance(p, dict) else p) is not None
                ]
                guid = autohdr._extract_guid(str(listing_id or ""))
                if not guid:
                    raise RuntimeError("Không lấy được UUID của photoshoot AutoHDR.")
                photoshoot_url = f"{autohdr._AUTOHDR_BASE}/photoshoots/uuid/{guid}?id={urllib.parse.quote(str(photoshoot_id or ''))}"
                js_log(f"🔓 Đang tải {len(photo_ids)} ảnh đã chọn bằng enterprise service...", "info", session_id, session_name)
                done = autohdr.run_clean_download(
                    cookie=autohdr_cookie_val,
                    photoshoot_url=photoshoot_url,
                    output_dir=Path(output_folder),
                    log_fn=lambda msg, type="info": js_log(msg, type, session_id, session_name),
                    progress_fn=lambda cur, total: js_progress(cur, total, session_id),
                    stop_event=stop_event,
                    create_subfolder=True,
                    options={"selected_photo_ids": photo_ids, "hwid": self.get_hwid()},
                )
                js_log(f"🎉 Hoàn tất: Đã tải xong {done}/{len(photo_ids)} ảnh về thư mục: {output_folder}", "success", session_id, session_name)
                _finalize_storage_output(output_handle, output_folder, session_id, session_name, stop_event)
            except Exception as e:
                import traceback
                js_log(f"❌ Lỗi tải ảnh AutoHDR: {e}\n{traceback.format_exc()}", "error", session_id, session_name)
            finally:
                if stop_event in active_download_events:
                    active_download_events.remove(stop_event)
                unregister_task_session(session_id)
                if not active_download_events:
                    js_status(
                        "idle",
                        "Đã dừng" if stop_event.is_set() else "Hoàn tất",
                        "download",
                        session_name,
                    )
                if not stop_event.is_set() and output_handle is not None:
                    _show_output_location(session_id, output_handle, output_folder)

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True}

    def autohdr_download(self, photoshoot_urls, savedir, session_label=None):
        if not self._check_service_auth("autohdr"):
            return {"ok": False, "msg": "Bản quyền hết hạn"}
        if not autohdr_cookie_val:
            js_log("❌ Chưa đăng nhập AutoHDR. Hãy đăng nhập AutoHDR trước!", "error")
            return {"ok": False, "msg": "Chưa đăng nhập AutoHDR"}
        if not photoshoot_urls:
            return {"ok": False}

        stop_event = threading.Event()
        active_download_events.append(stop_event)
        session_id = f"dl_a_{int(time.time() * 1000)}"
        session_name = str(session_label).strip() if session_label else f"DL AutoHDR ({len(photoshoot_urls)})"
        register_task_session(session_id, stop_event, "download", name=session_name)
        js_log(f"📥 Đang khởi chạy luồng tải AutoHDR song song ({len(photoshoot_urls)} dự án)...", "success", session_id, session_name)
        js_status("running", "Đang tải AutoHDR...", "download")

        def _run():
            global autohdr_connected
            output_handle = None
            output_folder = savedir
            try:
                output_handle = _prepare_storage_output(savedir, session_id)
                output_folder = output_handle.local_path
                total = len(photoshoot_urls)
                for idx, url in enumerate(photoshoot_urls):
                    if stop_event.is_set():
                        break
                    js_log(f"--- Tiến trình: Dự án {idx + 1}/{total} ---", "info", session_id, session_name)
                    autohdr.run_clean_download(
                        cookie=autohdr_cookie_val,
                        photoshoot_url=url,
                        output_dir=Path(output_folder),
                        log_fn=lambda msg, type="info": js_log(msg, type, session_id, session_name),
                        progress_fn=lambda cur, tot, sid=session_id: js_progress(cur, tot, sid),
                        stop_event=stop_event,
                        options={"hwid": self.get_hwid()},
                    )
                _finalize_storage_output(output_handle, output_folder, session_id, session_name, stop_event)
            except Exception as e:
                import traceback
                js_log(f"❌ AutoHDR lỗi: {e}\n{traceback.format_exc()}", "error", session_id, session_name)
                if _is_explicit_autohdr_auth_error(e):
                    js_log("⚠️ Phiên đăng nhập AutoHDR đã hết hạn. Vui lòng đăng nhập AutoHDR lại.", "warn", session_id, session_name)
                    autohdr_connected = False
            finally:
                if stop_event in active_download_events:
                    active_download_events.remove(stop_event)
                unregister_task_session(session_id)
                if not active_download_events:
                    js_status(
                        "idle",
                        "Đã dừng" if stop_event.is_set() else "Hoàn tất",
                        "download",
                        session_name,
                    )
                if not stop_event.is_set() and output_handle is not None:
                    _show_output_location(session_id, output_handle, output_folder)

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True}

    def autohdr_stop(self):
        for ev in list(active_download_events):
            ev.set()
        active_download_events.clear()
        for ev in list(active_upload_events):
            ev.set()
        active_upload_events.clear()
        with active_task_lock:
            active_task_events.clear()
        js_log("■ Đã dừng thao tác AutoHDR.", "warn")
        js_status("idle", "Đã dừng", "download")
        js_status("idle", "Đã dừng", "upload")


def check_sessions_loop(api_instance):
    global autohdr_connected, autohdr_cookie_val
    time.sleep(5)
    while True:
        time.sleep(30)
        if fotello.fotello_is_connected():
            try:
                fotello.fotello_get_tokens()
            except Exception as e:
                print(f"[SESSION_CHECK] Fotello token verification failed: {e}")
                fotello._fotello_state["connected"] = False
                js_log("⚠️ Phiên đăng nhập Fotello đã hết hạn hoặc bị hủy. Vui lòng đăng nhập Fotello lại.", "warn")

        if autohdr_connected and autohdr_cookie_val:
            try:
                ok, msg, new_cookie = autohdr._validate_autohdr_cookie(autohdr_cookie_val)
                if not ok:
                    autohdr_connected = False
                    js_log("⚠️ Phiên đăng nhập AutoHDR đã hết hạn. Vui lòng đăng nhập AutoHDR lại.", "warn")
                elif new_cookie and new_cookie != autohdr_cookie_val:
                    autohdr_cookie_val = new_cookie
                    _save_autohdr_cookie(autohdr_cookie_val)
            except Exception as e:
                print(f"[SESSION_CHECK] AutoHDR cookie verification failed: {e}")
                import requests
                if isinstance(e, requests.exceptions.HTTPError) and e.response is not None and e.response.status_code in (401, 403):
                    autohdr_connected = False
                    js_log("⚠️ Phiên đăng nhập AutoHDR đã hết hạn (401/403). Vui lòng đăng nhập AutoHDR lại.", "warn")


def main():
    global window
    api = Api()
    os.makedirs(UI_DIR, exist_ok=True)
    window = webview.create_window(
        "T&T",
        os.path.join(UI_DIR, "index_v2.html"),
        width=850,
        height=600,
        resizable=True,
        maximized=True,
        background_color="#140a10",
        js_api=api,
    )

    def _paths_from_drop_event(event):
        dropped = (event or {}).get("dataTransfer", {}).get("files", [])
        paths = []
        for item in dropped:
            path = item.get("pywebviewFullPath") if isinstance(item, dict) else None
            if path and os.path.isfile(path):
                paths.append(os.path.abspath(path))
        return paths

    def on_files_dropped(event):
        paths = _paths_from_drop_event(event)
        if paths and window:
            window.evaluate_js(f"handleNativeDroppedFiles({json.dumps(paths, ensure_ascii=False)})")

    def on_upscale_files_dropped(event):
        paths = _paths_from_drop_event(event)
        if paths and window:
            window.evaluate_js(f"handleNativeUpscaleDroppedFiles({json.dumps(paths, ensure_ascii=False)})")

    def on_dte_files_dropped(event):
        paths = _paths_from_drop_event(event)
        if paths and window:
            window.evaluate_js(f"handleNativeDteDroppedFiles({json.dumps(paths, ensure_ascii=False)})")

    def on_merge_files_dropped(kind):
        def _handler(event):
            paths = _paths_from_drop_event(event)
            if paths and window:
                window.evaluate_js(
                    f"handleNativeMergeDroppedFiles({json.dumps(kind)}, {json.dumps(paths, ensure_ascii=False)})"
                )

        return _handler

    def bind_drop_handler():
        try:
            if not hasattr(window, "dom"):
                print("[DROP] pywebview DOM API is not available; using JS fallback.")
                return
            dropzone = window.dom.get_element(".upload-dropzone")
            if dropzone:
                dropzone.on("drop", on_files_dropped)
            upscale_dropzone = window.dom.get_element(".upscale-dropzone")
            if upscale_dropzone:
                upscale_dropzone.on("drop", on_upscale_files_dropped)
            dte_dropzone = window.dom.get_element(".dte-dropzone")
            if dte_dropzone:
                dte_dropzone.on("drop", on_dte_files_dropped)
            merge_original_dropzone = window.dom.get_element("#merge-original-drop")
            if merge_original_dropzone:
                merge_original_dropzone.on("drop", on_merge_files_dropped("original"))
            merge_ai_dropzone = window.dom.get_element("#merge-ai-drop")
            if merge_ai_dropzone:
                merge_ai_dropzone.on("drop", on_merge_files_dropped("ai"))
        except Exception as e:
            print(f"[DROP] Native drop binding disabled: {e}")

    window.events.loaded += bind_drop_handler

    def run_check_loop():
        check_sessions_loop(api)

    threading.Thread(target=run_check_loop, daemon=True).start()

    def set_app_icon(win):
        if sys.platform == "darwin":
            try:
                from AppKit import NSApplication, NSImage
                logo_path = os.path.join(BASE_DIR, "assets", "autofotello-app-logo.png")
                if os.path.exists(logo_path):
                    ns_image = NSImage.alloc().initByReferencingFile_(logo_path)
                    NSApplication.sharedApplication().setApplicationIconImage_(ns_image)
                    print("[ICON] macOS Dock Icon set successfully.")
            except Exception as e:
                print(f"[ICON] Failed to set macOS dock icon: {e}")
        elif sys.platform == "win32":
            try:
                import ctypes
                myappid = "tt.realestate.tool.1"
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
            except Exception as e:
                print(f"[ICON] Failed to set AppUserModelID: {e}")
            try:
                import clr
                clr.AddReference("System.Windows.Forms")
                clr.AddReference("System.Drawing")
                from System.Drawing import Icon
                logo_path = os.path.join(BASE_DIR, "assets", "autofotello-app-logo.ico")
                if os.path.exists(logo_path):
                    win.native.Icon = Icon(logo_path)
                    print("[ICON] Windows Form Icon set successfully.")
            except Exception as e:
                print(f"[ICON] Failed to set Windows Form Icon: {e}")

    webview.start(set_app_icon, window, debug=False)


if __name__ == "__main__":
    main()
