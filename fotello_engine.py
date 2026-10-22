"""
Fotello Engine — Download enhanced images from Fotello.co
Integrates with AutoHDR Pro Client via Selenium token extraction.
"""

import base64
import hashlib
import json
import logging
import os
import time
import urllib.request
import urllib.error
import urllib.parse
import ssl
import certifi
import zipfile
import io
import re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime
from PIL import Image, ExifTags
import imagehash
import subprocess
import sys
import tempfile
import threading
import shutil
from image_upload_preprocessing import convert_image_to_srgb

_XOR_KEY = b"Ft2026Obf"


def _dec(blob: str) -> str:
    raw = base64.b64decode(blob)
    return bytes(b ^ _XOR_KEY[i % len(_XOR_KEY)] for i, b in enumerate(raw)).decode()


FIREBASE_API_KEY = "AIzaSyA9NOX3S33RaSfpMp00rBHxRrC8n7rKA1o"
FIREBASE_PROJECT = "real-estate-firebase-4109e"
FIREBASE_AUTH_URL = f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}"
FIRESTORE_URL = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}/databases/(default)/documents"

_FLD_IS_WM = "isWatermarked"
_FLD_BV = "booleanValue"
_FLD_SV = "stringValue"
_FLD_ENHANCES = "enhances"
_FLD_EDITED_UPSIZED = "editedImageUpsized"
_FLD_EDITED = "editedImage"
_FLD_STATUS = "status"

_EP_CREATE_LISTING = "create-listing"
_EP_CREATE_UPLOAD = "create-upload"
_EP_CREATE_ENHANCE = "create-enhance"

FOTELLO_API = "https://api.fotello.co/v1"

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif", ".bmp",
    ".cr3", ".cr2", ".nef", ".arw", ".dng",
}

CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".bmp": "image/bmp",
    ".cr3": "image/x-canon-cr3",
    ".cr2": "image/x-canon-cr2",
    ".nef": "image/x-nikon-nef",
    ".arw": "image/x-sony-arw",
    ".dng": "image/x-adobe-dng",
}

MAX_RETRIES = 5
POLL_INTERVAL = 5
POLL_TIMEOUT = 1800

_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

_fotello_state = {
    "refresh_token": "",
    "id_token": "",
    "access_token": "",
    "user_id": "",
    "email": "",
    "team_id": "",
    "connected": False,
    "token_refreshed_at": 0.0,
}
_fotello_token_lock = threading.Lock()

FOTELLO_TOKEN_FILE = Path.home() / ".fotello_tokens_autohdr.json"

TEAM_ID_CLAIM_KEYS = ("teamId", "team_id", "team", "defaultTeamId", "teamID")
TEAM_ID_USER_DOC_PATHS = (
    "users/{uid}",
    "users_public/{uid}",
    "user_profiles/{uid}",
    "profiles/{uid}",
    "memberships/{uid}",
    "team_members/{uid}",
)


def _natural_key(path):
    text = str(path).lower()
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


def _resolve_input_images(input_source):
    if isinstance(input_source, (list, tuple, set)):
        images = []
        seen = set()
        for raw_path in input_source:
            if not raw_path:
                continue
            p = Path(raw_path)
            if not p.exists():
                continue
            key = str(p.resolve())
            if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS and key not in seen:
                seen.add(key)
                images.append(p)
        if not images:
            raise ValueError("Không tìm thấy ảnh hợp lệ trong danh sách đã chọn")
        return images, "danh sách đã chọn"

    input_path = Path(input_source)
    if not input_path.exists():
        raise ValueError(f"Nguồn đầu vào không tồn tại: {input_source}")
    if input_path.is_file():
        images = [input_path] if input_path.suffix.lower() in IMAGE_EXTENSIONS else []
    elif input_path.is_dir():
        images = sorted(
            [
                p
                for p in input_path.iterdir()
                if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
            ],
            key=_natural_key,
        )
    else:
        raise ValueError(f"Nguồn đầu vào không hợp lệ: {input_source}")

    if not images:
        raise ValueError(f"Không tìm thấy ảnh hợp lệ trong {input_path}")
    return images, str(input_path.parent if input_path.is_file() else input_path)


def _urlopen(request, timeout=None):
    return urllib.request.urlopen(request, timeout=timeout, context=_SSL_CONTEXT)


def _field_string(fields, key: str) -> str:
    return str((fields or {}).get(key, {}).get(_FLD_SV, "") or "")


def _decode_firestore_value(value):
    if not isinstance(value, dict):
        return value
    if "nullValue" in value:
        return None
    if "booleanValue" in value:
        return bool(value["booleanValue"])
    for key in ("stringValue", "timestampValue", "referenceValue"):
        if key in value:
            return value[key]
    for key in ("integerValue", "doubleValue"):
        if key in value:
            try:
                return float(value[key]) if key == "doubleValue" else int(value[key])
            except Exception:
                return value[key]
    if "arrayValue" in value:
        return [
            _decode_firestore_value(item)
            for item in (value.get("arrayValue") or {}).get("values", [])
        ]
    if "mapValue" in value:
        return {
            key: _decode_firestore_value(item)
            for key, item in (value.get("mapValue") or {}).get("fields", {}).items()
        }
    return value


def _variant_filename(source_name: str, variant_type: str) -> str:
    path = Path(source_name or "image.jpg")
    return f"{path.stem}{path.suffix or '.jpg'}"


def _active_variant_render(variant: dict):
    renders = variant.get("renders")
    if not isinstance(renders, dict) or not renders:
        return "", None
    render_id = str(variant.get("activeRenderId") or "")
    render = renders.get(render_id) if render_id else None
    if isinstance(render, dict) and _variant_render_is_ready(render, variant):
        return render_id, render

    usable = [
        (rid, candidate)
        for rid, candidate in renders.items()
        if isinstance(candidate, dict) and _variant_render_is_ready(candidate, variant)
    ]
    if usable:
        return usable[-1]

    for rid, candidate in reversed(list(renders.items())):
        if isinstance(candidate, dict):
            return str(rid), candidate
    return "", None


def _variant_output_uri(render: dict, variant: dict | None = None) -> str:
    if not isinstance(render, dict):
        return ""

    def find_ai_revision_output(value, in_revision=False):
        if isinstance(value, dict):
            if in_revision:
                for key in ("upsizedUri", "uri", "outputUri", "output_uri"):
                    uri = value.get(key)
                    if uri:
                        return str(uri)
            for key, child in value.items():
                key_is_revision = in_revision or "revision" in str(key).lower()
                found = find_ai_revision_output(child, key_is_revision)
                if found:
                    return found
        elif isinstance(value, list):
            for child in reversed(value):
                found = find_ai_revision_output(child, in_revision)
                if found:
                    return found
        return ""

    revision_uri = find_ai_revision_output(render) or (
        find_ai_revision_output(variant) if isinstance(variant, dict) else ""
    )
    if revision_uri:
        return revision_uri

    selected = render.get("selectedOutput")
    if isinstance(selected, dict):
        uri = selected.get("upsizedUri") or selected.get("uri")
        if uri:
            return str(uri)
    outputs = render.get("outputs")
    if isinstance(outputs, list):
        for output in outputs:
            if isinstance(output, dict):
                uri = output.get("upsizedUri") or output.get("uri")
                if uri:
                    return str(uri)
    elif isinstance(outputs, dict):
        for output in outputs.values():
            if isinstance(output, dict):
                uri = output.get("upsizedUri") or output.get("uri")
                if uri:
                    return str(uri)
    return ""


def _variant_render_is_ready(render: dict, variant: dict | None = None) -> bool:
    """Accept completed outputs despite service-specific terminal labels."""
    if not isinstance(render, dict) or not _variant_output_uri(render, variant):
        return False
    status = str(render.get("status") or "").lower()
    return status not in (
        "failed",
        "error",
        "cancelled",
        "canceled",
        "pending",
        "queued",
        "processing",
    )


def _patch_variant_watermark(variant_id: str, render_id: str, access_token: str, log=None):
    if not variant_id or not render_id:
        return None
    render_field = {
        "mapValue": {
            "fields": {
                render_id: {
                    "mapValue": {
                        "fields": {
                            "isWatermarked": {_FLD_BV: False},
                        }
                    }
                }
            }
        }
    }
    return _firestore_patch(
        f"variants/{variant_id}",
        {"renders": render_field},
        access_token,
        [f"renders.{render_id}.isWatermarked"],
        log=log,
    )


def _download_media_uri(uri: str, access_token: str) -> bytes:
    if uri.startswith("gs://"):
        return _storage_download(uri, access_token)
    req = urllib.request.Request(uri, headers={"Authorization": f"Bearer {access_token}"})
    with _urlopen(req, timeout=120) as response:
        return response.read()


def _download_variant_item(item: dict, access_token: str, output_dir: Path, log=None) -> Path:
    if item.get("is_watermarked"):
        try:
            _patch_variant_watermark(
                str(item.get("variant_id") or ""),
                str(item.get("render_id") or ""),
                access_token,
                log=log,
            )
        except Exception as exc:
            if log:
                log(f"  ⚠ Không tắt được watermark của variant: {exc}", "warn")

    output_path = output_dir / Path(item.get("name") or "variant.jpg").name
    output_path.write_bytes(_download_media_uri(item["image_uri"], access_token))
    input_width = int(item.get("input_width") or 0)
    input_height = int(item.get("input_height") or 0)
    _upscale_to_original(output_path, input_width, input_height, log or (lambda *_: None))
    if log:
        log(f"  ✔ {output_path.name} ({output_path.stat().st_size / 1048576:.1f}MB)", "success")
    return output_path


def _enhance_download_candidates(fields) -> list:
    candidates = [
        ("upsized", _field_string(fields, _FLD_EDITED_UPSIZED)),
        ("edited", _field_string(fields, _FLD_EDITED)),
        ("output", _field_string(fields, "outputImage")),
        ("merged", _field_string(fields, "mergedImage")),
        ("perspective", _field_string(fields, "mergedImagePerspectiveOn")),
    ]
    result = []
    seen = set()
    for label, uri in candidates:
        if uri and uri not in seen:
            seen.add(uri)
            result.append((label, uri))
    return result


def _looks_like_uploaded_source_uri(uri: str, source_upload_ids=None, source_filenames=None) -> bool:
    text = urllib.parse.unquote(str(uri or "")).lower()
    if not text:
        return False
    upload_ids = [str(value).lower() for value in (source_upload_ids or []) if value]
    filenames = [Path(str(value)).name.lower() for value in (source_filenames or []) if value]
    for upload_id in upload_ids:
        if upload_id in text:
            return True
    if "fotello-uploads" in text:
        return True
    for filename in filenames:
        if filename and f"/{filename}" in text and "upload" in text:
            return True
    return False


def _bytes_match_source_file(data: bytes, source_paths=None) -> bool:
    if not data or not source_paths:
        return False
    digest = None
    for source_path in source_paths:
        if not source_path:
            continue
        path = Path(source_path)
        if not path.is_file():
            continue
        try:
            if path.stat().st_size != len(data):
                continue
            if digest is None:
                digest = hashlib.sha256(data).digest()
            with path.open("rb") as src:
                if hashlib.sha256(src.read()).digest() == digest:
                    return True
        except Exception:
            continue
    return False


def _save_fotello_tokens():
    try:
        FOTELLO_TOKEN_FILE.write_text(
            json.dumps({"refresh_token": _fotello_state["refresh_token"]}, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _load_fotello_tokens():
    try:
        if FOTELLO_TOKEN_FILE.exists():
            data = json.loads(FOTELLO_TOKEN_FILE.read_text(encoding="utf-8"))
            if data.get("refresh_token"):
                _fotello_state["refresh_token"] = data["refresh_token"]
                return True
    except Exception:
        pass
    return False


def fotello_logout():
    _fotello_state.update({
        "refresh_token": "",
        "id_token": "",
        "access_token": "",
        "user_id": "",
        "email": "",
        "team_id": "",
        "connected": False,
    })
    try:
        FOTELLO_TOKEN_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _retry(fn, max_retries=MAX_RETRIES):
    """Retry with exponential backoff on 429/5xx and network errors."""
    for attempt in range(max_retries):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="ignore")
            except Exception:
                pass
            print(f"[HTTP Error] {e.code} response body: {err_body}")
            if e.code == 429 and attempt < max_retries - 1:
                wait = 2 ** (attempt + 2)
                time.sleep(wait)
            elif e.code >= 500 and attempt < max_retries - 1:
                wait = 2 ** attempt
                time.sleep(wait)
            else:
                raise
        except urllib.error.URLError as e:
            if attempt < max_retries - 1:
                print(f"[Network Error] {e}. Retrying {attempt + 1}/{max_retries}...")
                wait = 2 ** (attempt + 1)
                time.sleep(wait)
            else:
                raise
        except Exception as e:
            import http.client
            if isinstance(e, http.client.HTTPException) and attempt < max_retries - 1:
                print(f"[Network Error] {e}. Retrying {attempt + 1}/{max_retries}...")
                wait = 2 ** (attempt + 1)
                time.sleep(wait)
            else:
                raise


def _firestore_get(doc_path: str, access_token: str):
    url = f"{FIRESTORE_URL}/{doc_path}"

    def _do():
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        with _urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    return _retry(_do)


def _firestore_patch(doc_path: str, fields, access_token: str, mask: list, log=None):
    mask_str = "&".join(f"updateMask.fieldPaths={m}" for m in mask)
    url = f"{FIRESTORE_URL}/{doc_path}?{mask_str}"
    body = json.dumps({"fields": fields}).encode()

    def _do():
        try:
            req = urllib.request.Request(
                url,
                data=body,
                method="PATCH",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
            )
            with _urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err = e.read().decode() if hasattr(e, "read") else str(e)
            if log:
                log(f"PATCH Error {e.code}: {err}", "error")
            raise

    return _retry(_do)


def _firestore_run_query(access_token: str, structured_query: dict, log=None) -> list:
    url = f"{FIRESTORE_URL}:runQuery"
    body = json.dumps({"structuredQuery": structured_query}).encode()

    def _do():
        try:
            req = urllib.request.Request(
                url,
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
            )
            with _urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err = e.read().decode() if hasattr(e, "read") else str(e)
            if log:
                log(f"Query Error {e.code}: {err}", "error")
            raise

    return _retry(_do)


def _storage_download(gs_uri: str, access_token: str) -> bytes:
    parts = gs_uri.replace("gs://", "").split("/", 1)
    bucket = parts[0]
    obj_path = urllib.parse.quote(parts[1], safe="")
    url = f"https://firebasestorage.googleapis.com/v0/b/{bucket}/o/{obj_path}?alt=media"

    def _do():
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        with _urlopen(req, timeout=60) as resp:
            return resp.read()

    return _retry(_do)


def _api_post(endpoint: str, body, id_token: str):
    data = json.dumps(body).encode()

    def _do():
        req = urllib.request.Request(
            f"{FOTELLO_API}/{endpoint}",
            data=data,
            headers={
                "Content-Type": "application/json",
                "authorization": id_token,
                "Origin": "https://app.fotello.co",
                "Referer": "https://app.fotello.co/",
            },
        )
        with _urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    return _retry(_do)


def _get_content_type(filepath: Path) -> str:
    return CONTENT_TYPES.get(filepath.suffix.lower(), "application/octet-stream")


def _upload_image_resumable(filepath: Path, id_token: str, team_id: str) -> str:
    filename = filepath.name
    result = _api_post(_EP_CREATE_UPLOAD, {"filename": filename, "teamId": team_id}, id_token)
    upload_id = result["id"]
    object_name = f"{upload_id}/{filename}"
    content_type = _get_content_type(filepath)
    file_size = filepath.stat().st_size
    file_data = filepath.read_bytes()
    start_url = (
        f"https://firebasestorage.googleapis.com/v0/b/fotello-uploads/o"
        f"?name={urllib.parse.quote(object_name, safe='')}"
    )

    def _start():
        start_body = json.dumps({"contentType": content_type}).encode()
        start_req = urllib.request.Request(
            start_url,
            data=start_body,
            method="POST",
            headers={
                "Content-Type": "application/json; charset=UTF-8",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(file_size),
                "X-Goog-Upload-Header-Content-Type": content_type,
                "X-Goog-Upload-Protocol": "resumable",
                "Authorization": f"Firebase {id_token}",
            },
        )
        return _urlopen(start_req, timeout=15)

    resp = _retry(_start)
    upload_url = resp.headers.get("x-goog-upload-url") or resp.headers.get("X-Goog-Upload-URL")
    if not upload_url:
        raise RuntimeError("Resumable start didn't return upload URL")

    def _upload():
        upload_req = urllib.request.Request(
            upload_url,
            data=file_data,
            method="POST",
            headers={
                "Content-Type": content_type,
                "X-Goog-Upload-Command": "upload, finalize",
                "X-Goog-Upload-Offset": "0",
            },
        )
        return _urlopen(upload_req, timeout=120)

    _retry(_upload)
    return upload_id


def _refresh_firebase_token(refresh_token: str):
    """Call Firebase securetoken API to get new id_token + access_token."""
    body = f"grant_type=refresh_token&refresh_token={refresh_token}".encode()
    req = urllib.request.Request(
        FIREBASE_AUTH_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with _urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    return {
        "id_token": data["id_token"],
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", refresh_token),
    }


def fotello_get_tokens(force_refresh: bool = False):
    """Refresh and return current tokens (thread-safe with short-term cache)."""
    with _fotello_token_lock:
        if not _fotello_state["refresh_token"]:
            raise RuntimeError('Chưa kết nối Fotello. Bấm "Kết nối Fotello" trước.')
        now = time.time()
        if (
            not force_refresh
            and _fotello_state.get("id_token")
            and _fotello_state.get("access_token")
            and (now - float(_fotello_state.get("token_refreshed_at") or 0.0)) < 120.0
        ):
            return {
                "id_token": _fotello_state["id_token"],
                "access_token": _fotello_state["access_token"],
                "refresh_token": _fotello_state["refresh_token"],
            }
        tokens = _refresh_firebase_token(_fotello_state["refresh_token"])
        _fotello_state["id_token"] = tokens["id_token"]
        _fotello_state["access_token"] = tokens["access_token"]
        _fotello_state["refresh_token"] = tokens["refresh_token"]
        _fotello_state["token_refreshed_at"] = time.time()
        _sync_fotello_identity(tokens["id_token"])
        _save_fotello_tokens()
        return tokens


def fotello_grab_tokens_from_browser(driver, log=None):
    """
    Use Selenium to extract Firebase refresh token from browser localStorage.
    User must be logged into app.fotello.co.
    Returns True if successful.
    """
    if log is None:
        log = lambda msg, t="": None

    current_url = driver.current_url or ""
    if "fotello.co" not in current_url:
        log("Đang mở app.fotello.co...", "info")
        driver.get("https://app.fotello.co")
        time.sleep(3)

    log("Đang tìm Firebase token...", "info")
    try:
        driver.set_script_timeout(10)
        token_data = driver.execute_async_script("""
        const done = arguments[arguments.length - 1];

        // Method 1: Firebase Auth persistence in localStorage
        for (let i = 0; i < localStorage.length; i++) {
            const key = localStorage.key(i);
            if (key && key.startsWith('firebase:authUser:')) {
                try {
                    const data = JSON.parse(localStorage.getItem(key));
                    if (data && data.stsTokenManager) {
                        return done({
                            refresh_token: data.stsTokenManager.refreshToken || '',
                            access_token: data.stsTokenManager.accessToken || '',
                            uid: data.uid || '',
                            email: data.email || '',
                        });
                    }
                } catch(e) {}
            }
        }

        // Method 2: IndexedDB (Firebase Auth default)
        try {
            const req = indexedDB.open('firebaseLocalStorageDb');
            req.onsuccess = function(e) {
                const db = e.target.result;
                try {
                    const tx = db.transaction('firebaseLocalStorage', 'readonly');
                    const store = tx.objectStore('firebaseLocalStorage');
                    const getAllReq = store.getAll();
                    
                    getAllReq.onsuccess = function(ev) {
                        const items = ev.target.result;
                        if (items) {
                            for (let item of items) {
                                const data = item.value;
                                if (data && data.stsTokenManager) {
                                    return done({
                                        refresh_token: data.stsTokenManager.refreshToken || '',
                                        access_token: data.stsTokenManager.accessToken || '',
                                        uid: data.uid || '',
                                        email: data.email || '',
                                    });
                                }
                            }
                        }
                        // Not found in DB either
                        done(null);
                    };
                    getAllReq.onerror = () => done(null);
                } catch(err) {
                    done(null);
                }
            };
            req.onerror = () => done(null);
        } catch(err) {
            done(null);
        }
        """)
    except Exception as e:
        log(f"❌ Lỗi trích xuất (Selenium): {e}", "error")
        return False

    log(f"Debug token_data type: {type(token_data)}", "info")
    if token_data:
        log(
            f"Debug token_data keys: {list(token_data.keys()) if isinstance(token_data, dict) else 'not a dict'}",
            "info",
        )

    if not token_data or not token_data.get("refresh_token"):
        log("❌ Không tìm thấy token. Hãy login app.fotello.co trước!", "error")
        return False

    if token_data["refresh_token"] != _fotello_state.get("refresh_token"):
        _fotello_state.update({
            "id_token": "",
            "access_token": "",
            "user_id": "",
            "email": "",
            "team_id": "",
            "connected": False,
        })
    _fotello_state["refresh_token"] = token_data["refresh_token"]
    if token_data.get("access_token"):
        _fotello_state["access_token"] = token_data["access_token"]

    try:
        tokens = fotello_get_tokens()
        _fotello_state["connected"] = True
        _save_fotello_tokens()
        email = token_data.get("email", "unknown")
        log(f"✔ Kết nối Fotello thành công! (email: {email})", "success")

        try:
            team_id = _detect_team_id(tokens["id_token"], tokens["access_token"])
            if team_id:
                _fotello_state["team_id"] = team_id
                log(f"✔ Team ID: {team_id[:12]}...", "success")
            else:
                log("⚠ Không detect được Team ID, sẽ dùng default", "warn")
        except Exception as e:
            log(f"⚠ Detect Team ID lỗi: {e}", "warn")

        return True
    except Exception as e:
        log(f"❌ Token lỗi: {e}", "error")
        return False


def fotello_reconnect_saved(log=None):
    """Try to reconnect using saved refresh token."""
    if log is None:
        log = lambda msg, t="": None
    if not _load_fotello_tokens():
        return False
    if not _fotello_state.get("refresh_token"):
        return False
    try:
        tokens = fotello_get_tokens()
        _fotello_state["connected"] = True
        team_id = _detect_team_id(tokens["id_token"], tokens["access_token"])
        if team_id:
            _fotello_state["team_id"] = team_id
        log("✔ Đã kết nối lại Fotello từ token đã lưu.", "success")
        return True
    except Exception:
        return False


def fotello_validate_session(log=None):
    """Validate the saved/current Fotello refresh token against Firebase."""
    if log is None:
        log = lambda msg, t="": None
    has_saved_token = bool(_fotello_state.get("refresh_token") or _load_fotello_tokens())
    if not _fotello_state.get("refresh_token"):
        _fotello_state["connected"] = False
        return {
            "connected": False,
            "team_id": "",
            "has_saved_token": has_saved_token,
            "msg": "Chưa đăng nhập Fotello",
        }
    try:
        tokens = fotello_get_tokens()
        _fotello_state["connected"] = True
        team_id = _fotello_state.get("team_id", "")
        if not team_id:
            try:
                team_id = _detect_team_id(tokens["id_token"], tokens["access_token"])
                if team_id:
                    _fotello_state["team_id"] = team_id
            except Exception as e:
                log(f"⚠ Validate Fotello OK nhưng detect Team ID lỗi: {e}", "warn")
        return {
            "connected": True,
            "team_id": _fotello_state.get("team_id", "")[:12],
            "email": _fotello_state.get("email", ""),
            "has_saved_token": True,
            "msg": "Đăng nhập Fotello còn hiệu lực",
        }
    except Exception as e:
        _fotello_state["connected"] = False
        return {
            "connected": False,
            "team_id": "",
            "has_saved_token": has_saved_token,
            "msg": f"Phiên đăng nhập Fotello đã hết hạn: {e}",
        }


def _decode_jwt_payload(id_token: str):
    try:
        parts = id_token.split(".")
        if len(parts) < 2:
            return {}
        payload_b64 = parts[1]
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _sync_fotello_identity(id_token: str) -> str:
    """Track the token owner and discard account-specific state on user change."""
    payload = _decode_jwt_payload(id_token)
    user_id = str(payload.get("user_id") or payload.get("sub") or "").strip()
    email = str(payload.get("email") or "").strip()
    previous_user_id = str(_fotello_state.get("user_id") or "").strip()
    if user_id and previous_user_id and user_id != previous_user_id:
        _fotello_state["team_id"] = ""
    if user_id:
        _fotello_state["user_id"] = user_id
    if email:
        _fotello_state["email"] = email
    return user_id


def fotello_get_account_key() -> str:
    """Return a stable, non-secret key used to isolate per-account caches."""
    user_id = str(_fotello_state.get("user_id") or "").strip()
    if user_id:
        return f"uid:{user_id}"
    refresh_token = str(_fotello_state.get("refresh_token") or "").strip()
    if refresh_token:
        return f"token:{hashlib.sha256(refresh_token.encode('utf-8')).hexdigest()[:16]}"
    return ""


def _detect_team_id(id_token: str, access_token: str) -> str:
    """Auto-detect team ID from JWT claims then Firestore user docs."""
    payload = _decode_jwt_payload(id_token)
    for key in TEAM_ID_CLAIM_KEYS:
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    teams_dict = payload.get("teams")
    if isinstance(teams_dict, dict) and teams_dict:
        for t_id in teams_dict.keys():
            if isinstance(t_id, str) and t_id.strip():
                return t_id.strip()

    uid = str(payload.get("user_id") or payload.get("sub") or "").strip()
    if uid:
        for template in TEAM_ID_USER_DOC_PATHS:
            doc_path = template.format(uid=uid)
            try:
                doc = _firestore_get(doc_path, access_token)
                fields = doc.get("fields", {})
                for key in TEAM_ID_CLAIM_KEYS:
                    field = fields.get(key, {})
                    val = field.get(_FLD_SV, "")
                    if val and val.strip():
                        return val.strip()
            except Exception:
                continue

    try:
        query = {
            "from": [{"collectionId": "listings"}],
            "limit": 5,
        }
        rows = _firestore_run_query(access_token, query)
        for row in rows:
            doc = row.get("document", {})
            fields = doc.get("fields", {})
            for key in TEAM_ID_CLAIM_KEYS:
                val = fields.get(key, {}).get(_FLD_SV, "")
                if val and val.strip():
                    return val.strip()
    except Exception:
        pass

    return ""


def fotello_list_listings(log=None):
    """
    Query Firestore for all listings accessible by this user.
    Returns list of dicts: [{id, name, created_at, enhance_count}, ...]
    """
    if log is None:
        log = lambda msg, t="": None
    if not _fotello_state.get("connected"):
        raise RuntimeError("Chưa kết nối Fotello")

    tokens = fotello_get_tokens()
    access_token = tokens["access_token"]
    team_id = _fotello_state.get("team_id", "")

    query = {
        "from": [{"collectionId": "listings"}],
        "limit": 100,
    }
    if team_id:
        query["where"] = {
            "fieldFilter": {
                "field": {"fieldPath": "teamId"},
                "op": "EQUAL",
                "value": {"stringValue": team_id},
            }
        }

    log("Đang tải danh sách listings...", "info")
    rows = _firestore_run_query(access_token, query, log=log)
    listings = []
    for row in rows:
        doc = row.get("document")
        if not doc:
            continue
        doc_name = doc.get("name", "")
        doc_id = doc_name.split("/")[-1] if doc_name else ""
        fields = doc.get("fields", {})
        name = (
            fields.get("name", {}).get(_FLD_SV, "")
            or fields.get("address", {}).get(_FLD_SV, "")
        )
        created = (
            fields.get("createdAt", {}).get("timestampValue", "")
            or doc.get("createTime", "")
        )
        num_brackets = int(fields.get("num_total_brackets", {}).get("integerValue", "0"))
        listings.append({
            "id": doc_id,
            "name": name or f"Listing {doc_id[:8]}",
            "created_at": created[:16].replace("T", " ") if created else "—",
            "brackets": num_brackets,
        })

    listings.sort(key=lambda x: x["created_at"], reverse=True)
    log(f"✔ Tìm thấy {len(listings)} listings", "success")
    return listings


def fotello_list_enhances_for_listing(listing_id: str, log=None):
    """Get all enhances for a specific listing."""
    if log is None:
        log = lambda msg, t="": None
    tokens = fotello_get_tokens()
    access_token = tokens["access_token"]

    query = {
        "from": [{"collectionId": _FLD_ENHANCES}],
        "where": {
            "fieldFilter": {
                "field": {"fieldPath": "listingId"},
                "op": "EQUAL",
                "value": {_FLD_SV: listing_id},
            }
        },
        "limit": 200,
    }
    rows = _firestore_run_query(access_token, query, log=log)
    enhances = []
    for row in rows:
        doc = row.get("document")
        if not doc:
            continue
        doc_name = doc.get("name", "")
        eid = doc_name.split("/")[-1] if doc_name else ""
        fields = doc.get("fields", {})
        status = fields.get(_FLD_STATUS, {}).get(_FLD_SV, "unknown")
        candidates = _enhance_download_candidates(fields)
        image_uri = candidates[0][1] if candidates else ""
        upsized_uri = _field_string(fields, _FLD_EDITED_UPSIZED)
        image_name = ""
        for key in ("filename", "fileName", "originalFilename", "originalFileName", "sourceFilename", "name"):
            image_name = fields.get(key, {}).get(_FLD_SV, "")
            if image_name:
                break
        if not image_name:
            for key in ("filenames", "sourceFilenames"):
                values = fields.get(key, {}).get("arrayValue", {}).get("values", [])
                for item in values:
                    value = item.get(_FLD_SV, "")
                    if value:
                        image_name = value
                        break
                if image_name:
                    break
        created_at = (
            fields.get("createdAt", {}).get("timestampValue", "")
            or doc.get("createTime", "")
        )
        enhances.append({
            "id": eid,
            "status": status,
            "has_image": bool(image_uri),
            "upsized": bool(upsized_uri),
            "image_uri": image_uri,
            "name": image_name,
            "created_at": created_at,
            "input_width": int(fields.get("inputWidth", {}).get("integerValue", 0) or 0),
            "input_height": int(fields.get("inputHeight", {}).get("integerValue", 0) or 0),
        })

    missing_names = any(not item.get("name") for item in enhances)
    if enhances and missing_names:
        try:
            listing_doc = _firestore_get(f"listings/{listing_id}", access_token)
            listing_fields = listing_doc.get("fields", {})
            filename_values = listing_fields.get("filenames", {}).get("arrayValue", {}).get("values", [])
            original_names = [
                item.get(_FLD_SV, "")
                for item in filename_values
                if item.get(_FLD_SV, "")
            ]
            enhances.sort(key=lambda item: (item.get("created_at") or "", item.get("id") or ""))
            group_size = (
                max(1, len(original_names) // len(enhances))
                if original_names and len(original_names) >= len(enhances)
                else 1
            )
            for index, item in enumerate(enhances):
                if item.get("name"):
                    continue
                source_index = index * group_size
                if source_index < len(original_names):
                    item["name"] = original_names[source_index]
                elif index < len(original_names):
                    item["name"] = original_names[index]
        except Exception as exc:
            log(f"Không khôi phục được tên ảnh gốc của listing: {exc}", "warn")

    for index, item in enumerate(enhances, start=1):
        if not item.get("name"):
            item["name"] = f"{index:03d}.jpg"

    try:
        variant_query = {
            "from": [{"collectionId": "variants"}],
            "where": {
                "fieldFilter": {
                    "field": {"fieldPath": "listingId"},
                    "op": "EQUAL",
                    "value": {_FLD_SV: listing_id},
                }
            },
            "limit": 500,
        }
        variant_rows = _firestore_run_query(access_token, variant_query, log=log)
        enhances_by_id = {item["id"]: item for item in enhances if item.get("id")}
        variant_items = []
        for row in variant_rows:
            doc = row.get("document")
            if not doc:
                continue
            variant = {
                key: _decode_firestore_value(value)
                for key, value in doc.get("fields", {}).items()
            }
            doc_name = doc.get("name", "")
            variant_id = doc_name.split("/")[-1] if doc_name else ""
            parent_id = str(variant.get("parentId") or "")
            parent_type = str(variant.get("parentType") or "").lower()
            if parent_type and parent_type != "enhance":
                continue
            parent = enhances_by_id.get(parent_id, {})
            render_id, render = _active_variant_render(variant)
            if not isinstance(render, dict):
                continue
            image_uri = _variant_output_uri(render, variant)
            render_status = str(render.get("status") or "").lower()
            if not _variant_render_is_ready(render, variant):
                continue
            selected_output = (
                render.get("selectedOutput")
                if isinstance(render.get("selectedOutput"), dict)
                else {}
            )
            variant_items.append({
                "id": f"{variant_id}_{render_id}" if render_id else variant_id,
                "enhance_id": parent_id,
                "variant_id": variant_id,
                "render_id": render_id,
                "status": render_status or "success",
                "has_image": True,
                "upsized": bool(selected_output.get("upsizedUri")),
                "image_uri": image_uri,
                "name": _variant_filename(
                    parent.get("name") or f"{variant_id}.jpg",
                    str(variant.get("type") or ""),
                ),
                "created_at": str(
                    render.get("creationTime") or parent.get("created_at") or ""
                ),
                "is_variant": True,
                "is_watermarked": bool(render.get("isWatermarked")),
                "input_width": int(parent.get("input_width") or 0),
                "input_height": int(parent.get("input_height") or 0),
            })
        if variant_items:
            variant_parent_ids = {item["enhance_id"] for item in variant_items if item.get("enhance_id")}
            enhances = [item for item in enhances if item.get("id") not in variant_parent_ids]
            enhances.extend(variant_items)
            log(f"✔ Dùng {len(variant_items)} ảnh AI variant thay cho ảnh gốc Fotello", "success")
    except Exception as exc:
        log(f"Không tải được ảnh biến thể của listing: {exc}", "warn")

    return enhances


def fotello_get_listing_photos(listing_id: str, log=None):
    """Return lightweight data-URL previews for all completed listing images."""
    if log is None:
        log = lambda msg, t="": None
    tokens = fotello_get_tokens()
    access_token = tokens["access_token"]
    enhances = [
        item
        for item in fotello_list_enhances_for_listing(listing_id, log=log)
        if item.get("has_image") and item.get("image_uri")
    ]

    def build_preview(item):
        try:
            data = _download_media_uri(item["image_uri"], access_token)
            with Image.open(io.BytesIO(data)) as image:
                image = image.convert("RGB")
                image.thumbnail((360, 260))
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=78, optimize=True)
            preview = base64.b64encode(buffer.getvalue()).decode("ascii")
            return {
                "id": item["id"],
                "name": item["name"],
                "url": f"data:image/jpeg;base64,{preview}",
            }
        except Exception as exc:
            log(f"Không tạo được ảnh xem trước {item.get('name', '')}: {exc}", "warn")
            return None

    photos = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(build_preview, item) for item in enhances]
        for future in futures:
            photo = future.result()
            if photo:
                photos.append(photo)
    return photos


def fotello_download_selected_enhances(
    listing_id: str,
    enhance_ids: list,
    output_dir: str,
    log=None,
    progress_fn=None,
    is_cancelled=None,
):
    """Download only the selected completed enhances from a listing."""
    if log is None:
        log = lambda msg, t="": None
    if progress_fn is None:
        progress_fn = lambda c, t: None
    if is_cancelled is None:
        is_cancelled = lambda: False

    tokens = fotello_get_tokens()
    access_token = tokens["access_token"]
    selected_ids = {str(value) for value in (enhance_ids or []) if value}
    enhances = [
        item
        for item in fotello_list_enhances_for_listing(listing_id, log=log)
        if item.get("id") in selected_ids and item.get("has_image")
    ]
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    results = []
    progress_done = [0]
    dl_lock = threading.Lock()

    def _dl_selected(pair):
        index, item = pair
        if is_cancelled():
            return None
        try:
            if item.get("is_variant"):
                downloaded = _download_variant_item(item, access_token, output_path, log=log)
            else:
                try:
                    _firestore_patch(
                        f"{_FLD_ENHANCES}/{item['id']}",
                        {_FLD_IS_WM: {_FLD_BV: False}},
                        access_token,
                        [_FLD_IS_WM],
                        log=log,
                    )
                except Exception:
                    pass
                stem = Path(item.get("name") or f"{index:03d}.jpg").stem
                downloaded = _download_single_enhance(
                    item["id"],
                    access_token,
                    output_path,
                    stem,
                    log,
                    is_cancelled=is_cancelled,
                    preferred_uri=item.get("image_uri"),
                )
            return str(downloaded) if downloaded else None
        except Exception as exc:
            log(f"Lỗi tải {item.get('name', '')}: {exc}", "error")
            return None
        finally:
            with dl_lock:
                progress_done[0] += 1
                progress_fn(progress_done[0], len(enhances))

    if enhances:
        with ThreadPoolExecutor(max_workers=min(6, len(enhances))) as pool:
            for item_res in pool.map(_dl_selected, enumerate(enhances, start=1)):
                if item_res:
                    results.append(item_res)
    return results


def _prepare_download(listing_id: str, id_token: str, log=None):
    """Call prepareDownload Cloud Function for ZIP download.
    HTTP 400 means server is still preparing upsized images — retry."""
    if log is None:
        log = lambda msg, t="": None
    url = "https://us-central1-real-estate-firebase-4109e.cloudfunctions.net/prepareDownload"
    body = json.dumps({
        "listing_id": listing_id,
        "sections": ["photos"],
        "photo_formats": ["original"],
    }).encode()

    def _do():
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "accept": "*/*",
                "authorization": id_token,
                "content-type": "application/json",
                "origin": "https://app.fotello.co",
                "referer": "https://app.fotello.co/",
            },
        )
        with _urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())

    MAX_PD_RETRIES = 12
    for attempt in range(MAX_PD_RETRIES):
        try:
            result = _do()
            if result.get("download_url"):
                return result
            wait = 20
            log(
                f"    prepareDownload: no download_url yet, waiting {wait}s... ({attempt + 1}/{MAX_PD_RETRIES})",
                "info",
            )
            time.sleep(wait)
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="ignore")
            except Exception:
                pass
            if e.code in (400, 429) or e.code >= 500:
                wait = 10 if e.code == 400 else 20
                log(
                    f"    prepareDownload HTTP {e.code} ({err_body}), retrying in {wait}s... ({attempt + 1}/{MAX_PD_RETRIES})",
                    "info",
                )
                time.sleep(wait)
            else:
                log(f"    prepareDownload HTTP {e.code} Error: {err_body}", "error")
                raise
    return {}


def _download_single_enhance(
    enhance_id: str,
    access_token: str,
    output_dir: Path,
    src_name: str,
    log=None,
    fullsize=True,
    is_cancelled=None,
    source_upload_ids=None,
    source_paths=None,
    preferred_uri=None,
) -> Path:
    """Download a single enhance result from Firestore doc.
    If the downloaded image is smaller than the original input, upscale to match."""
    if log is None:
        log = lambda msg, t="": None
    input_width = 0
    input_height = 0
    source_width, source_height = _source_image_dimensions(source_paths)
    last_wait_reason = ""
    for attempt in range(POLL_TIMEOUT // POLL_INTERVAL):
        if is_cancelled and is_cancelled():
            raise RuntimeError("Đã dừng thao tác.")
        try:
            doc = _firestore_get(f"{_FLD_ENHANCES}/{enhance_id}", access_token)
            fields = doc.get("fields", {})
        except urllib.error.HTTPError as e:
            if e.code == 403:
                tokens = fotello_get_tokens()
                access_token = tokens["access_token"]
                continue
            raise

        input_width = int(fields.get("inputWidth", {}).get("integerValue", 0) or 0) or source_width
        input_height = int(fields.get("inputHeight", {}).get("integerValue", 0) or 0) or source_height
        source_filenames = [Path(path).name for path in (source_paths or []) if path]
        candidates = []
        if preferred_uri:
            candidates.append(("selected", str(preferred_uri)))
        for candidate in _enhance_download_candidates(fields):
            if candidate not in candidates:
                candidates.append(candidate)

        for label, download_uri in candidates:
            if _looks_like_uploaded_source_uri(download_uri, source_upload_ids, source_filenames):
                last_wait_reason = f"{label} vẫn đang trỏ về ảnh gốc"
                continue
            data = _storage_download(download_uri, access_token)
            if _bytes_match_source_file(data, source_paths):
                last_wait_reason = f"{label} tải về trùng byte ảnh gốc"
                continue
            out_path = output_dir / f"{src_name}.jpg"
            out_path.write_bytes(data)
            _upscale_to_original(out_path, input_width, input_height, log)
            size_mb = out_path.stat().st_size / 1048576
            log(f"  ✔ {out_path.name} ({size_mb:.1f}MB)", "success")
            return out_path

        status = fields.get(_FLD_STATUS, {}).get(_FLD_SV, "unknown")
        if status in ("enhance_failed", "failed"):
            raise RuntimeError(f"Enhance {enhance_id} failed: {status}")
        reason = f" ({last_wait_reason})" if last_wait_reason else ""
        if attempt % 2 == 0:
            log(
                f"  ⏳ Đang chờ kết quả xử lý {src_name}{reason}... ({(attempt + 1) * POLL_INTERVAL}s)",
                "info",
            )
        time.sleep(POLL_INTERVAL)

    raise RuntimeError(f"Timeout chờ enhance {enhance_id}")


def _source_image_dimensions(source_paths) -> tuple[int, int]:
    """Read original dimensions from the first usable uploaded source image."""
    for raw_path in source_paths or []:
        if not raw_path:
            continue
        path = Path(raw_path)
        if not path.is_file():
            continue
        try:
            with Image.open(path) as image:
                width, height = image.size
            if width > 0 and height > 0:
                return int(width), int(height)
        except Exception:
            pass
        if path.suffix.lower() in (".cr2", ".cr3", ".nef", ".arw", ".dng", ".raw"):
            try:
                import rawpy
                with rawpy.imread(str(path)) as raw:
                    width = int(
                        getattr(raw.sizes, "width", 0)
                        or getattr(raw.sizes, "raw_width", 0)
                        or 0
                    )
                    height = int(
                        getattr(raw.sizes, "height", 0)
                        or getattr(raw.sizes, "raw_height", 0)
                        or 0
                    )
                if width > 0 and height > 0:
                    return width, height
            except Exception:
                continue
    return 0, 0


def _upscale_to_original(image_path: Path, target_w: int, target_h: int, log):
    """Restore source dimensions when known and always write 300 DPI metadata."""
    try:
        with Image.open(image_path) as source:
            source.load()
            img = source.copy()
            exif = source.info.get("exif")
            icc_profile = source.info.get("icc_profile")
        cur_w, cur_h = img.size
        has_target_size = target_w > 0 and target_h > 0
        kwargs = {"quality": 100, "subsampling": 0, "dpi": (300, 300)}
        if exif:
            kwargs["exif"] = exif
        if icc_profile:
            kwargs["icc_profile"] = icc_profile
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        if not has_target_size:
            img.save(image_path, "JPEG", **kwargs)
            log(f"  🧾 Đã đặt metadata 300 DPI cho {image_path.name}", "info")
            return

        if cur_w == target_w and cur_h == target_h:
            img.save(image_path, "JPEG", **kwargs)
            log(f"  🧾 Giữ kích thước gốc {cur_w}x{cur_h} và đặt 300 DPI: {image_path.name}", "info")
            return

        resample = getattr(Image, "LANCZOS", getattr(Image, "ANTIALIAS", Image.BICUBIC))
        img_resized = img.resize((target_w, target_h), resample)
        try:
            from PIL import ImageFilter
            img_resized = img_resized.filter(
                ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=3)
            )
        except Exception:
            pass
        img_resized.save(image_path, "JPEG", **kwargs)
    except ImportError:
        log("  ⚠ Pillow not installed, skip upscaling", "warn")
    except Exception as e:
        log(f"  ⚠ Upscale error: {e}", "warn")


def fotello_download_listing(listing_id: str, output_dir: str, log=None, is_cancelled=None):
    """
    Download all enhanced images for a listing.
    Tries ZIP first, falls back to per-enhance download.
    Returns list of downloaded file paths.
    """
    if log is None:
        log = lambda msg, t="": None
    if is_cancelled is None:
        is_cancelled = lambda: False
    if not _fotello_state.get("connected"):
        raise RuntimeError("Chưa kết nối Fotello")

    tokens = fotello_get_tokens()
    id_token = tokens["id_token"]
    access_token = tokens["access_token"]
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []

    log("Đang chuẩn bị bẻ khóa logo...", "info")
    enhances = fotello_list_enhances_for_listing(listing_id, log)
    successful = [enh for enh in enhances if enh["has_image"]]
    if successful:
        log(f"  Tìm thấy {len(successful)} enhances có ảnh để patch", "info")
        def _patch_wm(enh):
            if is_cancelled() or enh.get("is_variant"):
                return
            try:
                _firestore_patch(
                    f"{_FLD_ENHANCES}/{enh['id']}",
                    {_FLD_IS_WM: {_FLD_BV: False}},
                    access_token,
                    [_FLD_IS_WM],
                    log=log,
                )
                log(f"  ✔ Đã patch watermark cho {enh['id'][:8]}", "success")
            except Exception as e:
                log(f"  ❌ Lỗi patch watermark cho {enh['id'][:8]}: {e}", "error")

        with ThreadPoolExecutor(max_workers=min(8, len(successful))) as pool:
            list(pool.map(_patch_wm, successful))
        time.sleep(0.5)

    log("Tải từng ảnh trực tiếp qua Firestore...", "info")
    if not successful:
        log("⚠ Không có ảnh nào sẵn sàng trong listing này", "warn")
        return results

    log(f"  Tìm thấy {len(successful)} ảnh sẵn sàng (tải song song)", "info")
    def _dl_enh(pair):
        i, enh = pair
        if is_cancelled():
            return None
        try:
            if enh.get("is_variant"):
                out = _download_variant_item(enh, access_token, out_dir, log=log)
            else:
                output_stem = Path(enh.get("name") or f"{i + 1:03d}").stem
                out = _download_single_enhance(
                    enh["id"],
                    access_token,
                    out_dir,
                    output_stem,
                    log,
                    is_cancelled=is_cancelled,
                    preferred_uri=enh.get("image_uri"),
                )
            return str(out) if out else None
        except Exception as e:
            log(f"  ❌ Lỗi tải enhance {enh['id'][:8]}: {e}", "error")
            return None

    with ThreadPoolExecutor(max_workers=min(6, len(successful))) as pool:
        for res_path in pool.map(_dl_enh, enumerate(successful)):
            if res_path:
                results.append(res_path)

    return results


def fotello_batch_download(
    listing_ids: list,
    output_dir: str,
    log=None,
    progress_fn=None,
    is_cancelled=None,
):
    """
    Download multiple listings.
    Returns total count of downloaded images.
    """
    if log is None:
        log = lambda msg, t="": None
    if progress_fn is None:
        progress_fn = lambda cur, tot: None
    if is_cancelled is None:
        is_cancelled = lambda: False

    total_results = []
    for idx, lid in enumerate(listing_ids):
        if is_cancelled():
            break
        log("\n========================================", "info")
        log(f"📂 Listing {idx + 1}/{len(listing_ids)}: {lid[:12]}...", "info")
        log("========================================", "info")
        listing_dir = os.path.join(output_dir, f"listing_{lid[:12]}")
        try:
            results = fotello_download_listing(lid, listing_dir, log, is_cancelled)
            total_results.extend(results)
        except Exception as e:
            log(f"❌ Lỗi listing {lid[:12]}: {e}", "error")
        progress_fn(idx + 1, len(listing_ids))

    log(f"\n🏁 Hoàn tất: {len(total_results)} ảnh đã tải", "success")
    return len(total_results)


def fotello_analyze_brackets(
    input_dir,
    bracket_size: int = 3,
    log=None,
    grouping_mode: str = "order_fixed",
):
    """
    Phân tích ảnh, gom các ảnh thành nhóm dựa trên thời gian chụp (EXIF) và độ tương đồng ảnh (pHash).
    Hỗ trợ đầy đủ file RAW (CR2, NEF, ARW, DNG) bằng cách trích xuất thumbnail nhúng qua rawpy.
    """
    if log is None:
        log = lambda msg, t="": None
    grouping_mode = (grouping_mode or "order_fixed").strip().lower()
    if grouping_mode not in ("time", "order", "order_fixed"):
        grouping_mode = "order_fixed"

    images, input_label = _resolve_input_images(input_dir)
    if bracket_size <= 0:
        bracket_size = 3
    log(f"📂 Đang phân tích {len(images)} ảnh từ {input_label} để tự động gom nhóm...", "info")

    if grouping_mode == "order_fixed":
        ordered_paths = [str(path) for path in images]
        brackets = [
            ordered_paths[index : index + bracket_size]
            for index in range(0, len(ordered_paths), bracket_size)
        ]
        log(f"🔢 Đã gom {len(ordered_paths)} ảnh theo thứ tự, {bracket_size} ảnh mỗi nhóm.", "info")
        log(f"✔ Đã tạo {len(brackets)} nhóm ảnh.", "success")
        return brackets

    RAW_EXTENSIONS = {".cr2", ".cr3", ".nef", ".arw", ".dng"}

    def _extract_from_raw(img_path):
        try:
            import rawpy
            with rawpy.imread(str(img_path)) as raw:
                thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                return Image.open(io.BytesIO(thumb.data))
            elif thumb.format == rawpy.ThumbFormat.BITMAP:
                return Image.fromarray(thumb.data)
        except Exception as e:
            log(f"  ⚠ Không trích được thumb RAW {img_path.name}: {e}", "warn")
        return None

    def _read_image_data(img_path):
        dt = None
        hashes = None
        pil_img = None
        has_real_time = False
        is_raw = img_path.suffix.lower() in RAW_EXTENSIONS
        try:
            if is_raw:
                pil_img = _extract_from_raw(img_path)
            else:
                pil_img = Image.open(img_path)
            if pil_img is not None:
                exif = pil_img.getexif()
                if exif:
                    dt_str = exif.get(36867)
                    if dt_str:
                        try:
                            dt = datetime.datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S")
                            has_real_time = True
                        except Exception:
                            pass
                    if not dt:
                        for tag_id in (36868, 306):
                            dt_str = exif.get(tag_id)
                            if dt_str:
                                try:
                                    dt = datetime.datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S")
                                    has_real_time = True
                                    break
                                except Exception:
                                    pass
                hash_img = pil_img.convert("RGB")
                hash_img.thumbnail((512, 512))
                hashes = {
                    "phash": imagehash.phash(hash_img),
                    "dhash": imagehash.dhash(hash_img),
                    "ahash": imagehash.average_hash(hash_img),
                    "whash": imagehash.whash(hash_img),
                }
        except Exception as e:
            log(f"  ⚠ Lỗi đọc {img_path.name}: {e}", "warn")
        finally:
            if pil_img is not None and not is_raw:
                try:
                    pil_img.close()
                except Exception:
                    pass
        if dt is None:
            dt = datetime.datetime.fromtimestamp(img_path.stat().st_mtime)
            log(f"  ℹ {img_path.name}: dùng thời gian file (không có EXIF)", "info")
        return dt, hashes, has_real_time

    img_data = [None] * len(images)
    analyzed_count = [0]
    analyze_lock = threading.Lock()

    def _analyze_one(pair):
        idx, img_path = pair
        dt, hashes, has_real_time = _read_image_data(img_path)
        item = {
            "path": str(img_path),
            "name": img_path.name,
            "order": idx,
            "dt": dt,
            "hashes": hashes,
            "has_real_time": has_real_time,
        }
        with analyze_lock:
            analyzed_count[0] += 1
            cnt = analyzed_count[0]
        if cnt % 10 == 0 or cnt == len(images):
            log(f"  Đã phân tích {cnt}/{len(images)} ảnh...", "info")
        return idx, item

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(images)))) as pool:
        for idx, item in pool.map(_analyze_one, enumerate(images)):
            img_data[idx] = item

    log(f"  ✔ Phân tích xong {len(images)} ảnh. Đang gộp nhóm...", "success")
    real_time_count = sum(1 for item in img_data if item["has_real_time"])
    use_capture_time = grouping_mode == "time" and real_time_count >= max(2, len(img_data) // 2)
    if grouping_mode == "order":
        img_data.sort(key=lambda x: x["order"])
        log("  🔢 Gom nhóm theo thứ tự ảnh đã chọn/up lên.", "info")
    elif use_capture_time:
        img_data.sort(key=lambda x: (x["dt"], x["order"]))
        log(
            f"  ⏱ Gom nhóm theo EXIF capture time ({real_time_count}/{len(img_data)} ảnh có timestamp thật).",
            "info",
        )
    else:
        img_data.sort(key=lambda x: x["order"])
        log("  ⏱ EXIF không đủ tin cậy, giữ thứ tự ảnh đầu vào để gom nhóm.", "warn")

    def _scene_metrics(a, b):
        hashes_a = a.get("hashes") or {}
        hashes_b = b.get("hashes") or {}
        if not hashes_a or not hashes_b:
            return True, 4, 0.0
        distances = {
            key: hashes_a[key] - hashes_b[key]
            for key in ("phash", "dhash", "ahash", "whash")
            if key in hashes_a and key in hashes_b
        }
        if not distances:
            return True, 4, 0.0
        votes = (
            (1 if distances.get("phash", 999) <= 24 else 0)
            + (1 if distances.get("dhash", 999) <= 16 else 0)
            + (1 if distances.get("ahash", 999) <= 16 else 0)
            + (1 if distances.get("whash", 999) <= 18 else 0)
        )
        score = min(
            distances.get("phash", 999),
            distances.get("dhash", 999) * 1.35,
            distances.get("ahash", 999) * 1.25,
            distances.get("whash", 999) * 1.2,
        )
        return (votes >= 2 or score <= 14), votes, score

    max_bracket_images = max(bracket_size, bracket_size + 2)
    brackets = []
    current_bracket = [img_data[0]] if img_data else []
    for i in range(1, len(img_data)):
        data = img_data[i]
        prev_data = current_bracket[-1]
        first_data = current_bracket[0]
        time_diff = abs((data["dt"] - prev_data["dt"]).total_seconds())
        time_from_first = abs((data["dt"] - first_data["dt"]).total_seconds())
        same_prev, score_prev, dist_prev = _scene_metrics(prev_data, data)
        same_first, score_first, dist_first = _scene_metrics(first_data, data)
        same_scene = same_prev and (same_first or len(current_bracket) < 2)
        if use_capture_time:
            time_ok = (time_diff <= 8.0 and time_from_first <= 20.0) or (
                same_scene and time_diff <= 10.0
            )
        else:
            time_ok = True
        is_same_bracket = same_scene and time_ok and len(current_bracket) < max_bracket_images
        if is_same_bracket:
            current_bracket.append(data)
        else:
            reason = []
            if not same_scene:
                reason.append(f"hash khác prev={dist_prev:.1f}, first={dist_first:.1f}")
            if not time_ok:
                reason.append(f"thời gian xa {time_diff:.1f}s/{time_from_first:.1f}s")
            if len(current_bracket) >= max_bracket_images:
                reason.append(f"đủ {len(current_bracket)} ảnh")
            log(f"  ↳ Tách nhóm trước {data['name']}: {', '.join(reason)}", "info")
            brackets.append([x["path"] for x in current_bracket])
            current_bracket = [data]

    if current_bracket:
        brackets.append([x["path"] for x in current_bracket])
    log(f"✔ Đã phân tích tự động thành {len(brackets)} nhóm.", "success")
    return brackets


def fotello_is_connected():
    return _fotello_state.get("connected", False)


def fotello_get_status():
    return {
        "connected": _fotello_state.get("connected", False),
        "team_id": _fotello_state.get("team_id", "")[:12],
        "email": _fotello_state.get("email", ""),
        "has_saved_token": bool(_fotello_state.get("refresh_token") or _load_fotello_tokens()),
    }


def _get_image_time(img_path: Path) -> datetime.datetime:
    try:
        if img_path.suffix.lower() in (".jpg", ".jpeg", ".tiff", ".tif"):
            with Image.open(img_path) as pil_img:
                exif = pil_img.getexif()
                if exif:
                    for tag_id in (36867, 36868, 306):
                        dt_str = exif.get(tag_id)
                        if dt_str:
                            return datetime.datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass
    return datetime.datetime.fromtimestamp(img_path.stat().st_mtime)


def _convert_to_jpg_sips(src_path: Path, dest_path: Path) -> bool:
    if sys.platform != "darwin":
        return False
    try:
        subprocess.run(
            ["sips", "-s", "format", "jpeg", str(src_path), "--out", str(dest_path)],
            capture_output=True,
            check=True,
        )
        return dest_path.exists()
    except Exception:
        return False


def _convert_to_jpg_pil(src_path: Path, dest_path: Path) -> bool:
    try:
        from PIL import Image
        ext = src_path.suffix.lower()
        if ext in (".cr2", ".cr3", ".nef", ".arw", ".dng", ".raw"):
            import rawpy
            with rawpy.imread(str(src_path)) as raw:
                rgb = raw.postprocess()
            img = Image.fromarray(rgb)
            img.save(dest_path, "JPEG", quality=92)
            return dest_path.exists()
        else:
            with Image.open(src_path) as img:
                if img.mode != "RGB":
                    img = img.convert("RGB")
                img.save(dest_path, "JPEG", quality=92)
            return dest_path.exists()
    except Exception:
        return False


def fotello_upload_and_enhance(
    input_dir,
    output_dir: str,
    log=None,
    progress_fn=None,
    status_fn=None,
    is_cancelled=None,
    preferences=None,
    predefined_brackets=None,
):
    """
    Upload local images to Fotello, trigger enhance, patch watermark immediately, and download results.
    Nếu có predefined_brackets, sử dụng mảng đó thay vì tự slice.
    """
    if log is None:
        log = lambda msg, t="": None
    if progress_fn is None:
        progress_fn = lambda cur, tot: None
    if status_fn is None:
        status_fn = lambda msg: None
    if is_cancelled is None:
        is_cancelled = lambda: False

    if not _fotello_state.get("connected"):
        raise RuntimeError("Chưa kết nối Fotello")

    tokens = fotello_get_tokens()
    id_token = tokens["id_token"]
    access_token = tokens["access_token"]
    team_id = _fotello_state.get("team_id", "")
    if not team_id:
        raise RuntimeError("Lỗi không tìm thấy team_id. Hãy kết nối lại.")

    raw_images, input_label = _resolve_input_images(input_dir)
    convert_to_jpg = preferences.get("convert_to_jpg", True) if preferences else True
    images = []
    temp_dir_obj = None
    raw_camera_exts = {".arw", ".cr2", ".cr3", ".nef", ".dng", ".raw"}
    try:
        needs_conv = any(
            rf.suffix.lower() in raw_camera_exts
            or (convert_to_jpg and rf.suffix.lower() not in (".jpg", ".jpeg"))
            for rf in raw_images
        )
        if needs_conv and temp_dir_obj is None:
            temp_dir_obj = tempfile.TemporaryDirectory(prefix="fotello_conv_")

        def _conv_item(rf: Path):
            if is_cancelled():
                return rf, None
            ext = rf.suffix.lower()
            if ext in raw_camera_exts or (convert_to_jpg and ext not in (".jpg", ".jpeg")):
                dest_path = Path(temp_dir_obj.name) / f"{rf.stem}.jpg"
                log(
                    f"🔄 Định dạng {rf.name} không phải JPG. Đang tự động chuyển đổi sang JPG...",
                    "info",
                )
                conv_ok = _convert_to_jpg_sips(rf, dest_path)
                if not conv_ok:
                    conv_ok = _convert_to_jpg_pil(rf, dest_path)
                if not conv_ok:
                    log(f"❌ Không thể chuyển đổi {rf.name}, bỏ qua file này.", "error")
                    return rf, None
                log(f"✅ Chuyển đổi thành công {rf.name} -> {dest_path.name}", "success")
                return rf, dest_path
            return rf, rf

        converted_pairs = []
        if needs_conv:
            with ThreadPoolExecutor(max_workers=min(8, max(1, len(raw_images)))) as conv_pool:
                converted_pairs = list(conv_pool.map(_conv_item, raw_images))
        else:
            converted_pairs = [(rf, rf) for rf in raw_images]

        if is_cancelled():
            log("⚠ Đã dừng bởi người dùng.", "warn")
            return 0

        img_dict = {}
        for orig_path, final_path in converted_pairs:
            if final_path is None:
                continue
            images.append(final_path)
            img_dict[orig_path.name] = final_path
            img_dict[final_path.name] = final_path
            img_dict[str(orig_path)] = final_path
            img_dict[str(final_path)] = final_path

        if not images:
            raise ValueError("Không có ảnh nào sẵn sàng để upload.")

        total_images = len(images)
        progress_total = total_images * 2
        progress_current = 0
        log(f"📂 Đã tìm thấy {total_images} ảnh từ {input_label}. Bắt đầu upload...", "info")
        status_fn(f"Đang upload ảnh: 0/{total_images} ảnh")
        progress_fn(0, progress_total)

        if preferences is None:
            preferences = {}
        raw_cloud_style = str(preferences.get("cloud_style", "original") or "original").strip()
        _valid_cloud_styles = {
            "original",
            "streaks_with_puffs",
            "sweep_streaks",
            "scatter_streaks",
            "crisp_streaks",
            "clear_fade",
        }
        if raw_cloud_style not in _valid_cloud_styles:
            raw_cloud_style = "streaks_with_puffs" if "puff" in raw_cloud_style else "original"
        api_preferences = {
            "contrast_style": preferences.get("contrast_style", "signature"),
            "exterior_sky_replacement": preferences.get("exterior_sky_replacement", "on"),
            "perspective_correction": preferences.get("perspective_correction", "off"),
            "custom_style_id": preferences.get("custom_style_id"),
            "cloud_style": raw_cloud_style,
        }
        project_name = str(preferences.get("project_name", "") or "").strip()

        if predefined_brackets:
            brackets = []
            for b_names in predefined_brackets:
                b_paths = [img_dict[p] for p in b_names if p in img_dict]
                if not b_paths:
                    b_paths = [
                        img_dict[Path(p).name]
                        for p in b_names
                        if Path(p).name in img_dict
                    ]
                if b_paths:
                    brackets.append(b_paths)
        else:
            brackets = [images[i : i + 3] for i in range(0, len(images), 3)]

        num_total_brackets = len(brackets)
        if num_total_brackets == 0:
            raise ValueError(
                "Không tìm thấy nhóm ảnh hợp lệ. Vui lòng chạy lại bước phân tích và kiểm tra các nhóm ảnh trước khi tiếp tục."
            )
        log(f"  Sử dụng {num_total_brackets} nhóm đã gom", "info")

        import threading
        progress_lock = threading.Lock()
        upload_map = {}
        uploaded = [0]
        upload_errors = []
        total_work = total_images + num_total_brackets * 2
        UPLOAD_WORKERS = min(8, total_images)

        def _upload_one(img_path):
            if is_cancelled():
                return
            try:
                uid = _upload_image_resumable(img_path, id_token, team_id)
                with progress_lock:
                    upload_map[img_path.name] = uid
                    uploaded[0] += 1
                    cnt = uploaded[0]
                log(f"  ↑ [{cnt}/{total_images}] {img_path.name} ✔", "success")
                status_fn(f"Đang upload ảnh: {cnt}/{total_images} ảnh")
                progress_fn(cnt, total_work)
            except Exception as e:
                with progress_lock:
                    upload_errors.append(img_path.name)
                log(f"  ❌ Upload lỗi: {img_path.name}: {e}", "error")

        log(f"  ⚡ Upload {total_images} ảnh ({UPLOAD_WORKERS} luồng song song)...", "info")
        with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as pool:
            futures = [pool.submit(_upload_one, img) for img in images]
            for future in as_completed(futures):
                if is_cancelled():
                    pool.shutdown(wait=False, cancel_futures=True)
                    log("⚠ Đã dừng bởi người dùng.", "warn")
                    return 0
                future.result()

        if is_cancelled():
            log("⚠ Đã dừng bởi người dùng.", "warn")
            return 0

        if upload_errors:
            log(
                f"  ⚠ {len(upload_errors)} ảnh upload lỗi, tiếp tục với {len(upload_map)} ảnh...",
                "warn",
            )
        if not upload_map:
            raise RuntimeError("Tất cả ảnh upload thất bại!")

        log(f"✔ Đã upload xong {len(upload_map)}/{total_images} ảnh. Đang tạo dự án...", "success")
        status_fn(f"Đã upload xong {len(upload_map)}/{total_images} ảnh. Đang tạo dự án...")

        listing_name_base = project_name or f"Listing - {time.strftime('%d %B, %Y %I:%M %p')}"
        uploaded_images = [p for p in images if p.name in upload_map]
        new_brackets = []
        for b in brackets:
            b_uploaded = [p for p in b if p.name in upload_map]
            if b_uploaded:
                new_brackets.append(b_uploaded)
        brackets = new_brackets
        num_total_brackets = len(brackets)
        if num_total_brackets == 0:
            raise RuntimeError(
                "Không tìm thấy nhóm ảnh hợp lệ sau khi upload. Vui lòng thử lại từ bước phân tích."
            )

        MAX_BRACKETS_PER_LISTING = 30
        brackets_by_size = {}
        for b in brackets:
            size = len(b)
            if size not in brackets_by_size:
                brackets_by_size[size] = []
            brackets_by_size[size].append(b)

        bracket_chunks = []
        for size, b_list in brackets_by_size.items():
            for i in range(0, len(b_list), MAX_BRACKETS_PER_LISTING):
                bracket_chunks.append(b_list[i : i + MAX_BRACKETS_PER_LISTING])

        jobs = []
        global_idx = 0
        for chunk_index, chunk in enumerate(bracket_chunks):
            if is_cancelled():
                log("⚠ Đã dừng bởi người dùng.", "warn")
                return 0
            chunk_listing_name = (
                f"{listing_name_base}-{chunk_index + 1}"
                if len(bracket_chunks) > 1
                else listing_name_base
            )
            chunk_filenames = [p.name for b in chunk for p in b]
            num_chunk_brackets = len(chunk)
            listing_result = _api_post(
                _EP_CREATE_LISTING,
                {
                    "name": chunk_listing_name,
                    "num_total_brackets": num_chunk_brackets,
                    "filenames": chunk_filenames,
                    "isDemoListing": False,
                    "teamId": team_id,
                },
                id_token,
            )
            listing_id = listing_result["id"]
            log(
                f"✔ Đã tạo Listing {chunk_listing_name} ({listing_id[:8]}) với {num_chunk_brackets} nhóm HDR",
                "success",
            )
            status_fn(
                f"Đã tạo dự án {chunk_listing_name}. Đang kích hoạt xử lý {num_chunk_brackets} nhóm..."
            )

            chunk_base_idx = global_idx
            global_idx += len(chunk)

            def _trigger_bracket(pair):
                idx_in_chunk, bracket_images = pair
                g_idx = chunk_base_idx + idx_in_chunk
                if is_cancelled():
                    return None
                bracket_upload_ids = [upload_map[p.name] for p in bracket_images]
                try:
                    status_fn(f"Đang kích hoạt xử lý nhóm {g_idx + 1}/{num_total_brackets}...")
                    try:
                        times = [_get_image_time(p) for p in bracket_images]
                        earliest_dt = min(times)
                        earliest_iso = earliest_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
                    except Exception:
                        earliest_iso = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")

                    middle_idx = len(bracket_images) // 2
                    preview_img = bracket_images[middle_idx]
                    preview_id = upload_map[preview_img.name]
                    enhance_result = _api_post(
                        _EP_CREATE_ENHANCE,
                        {
                            "upload_ids": bracket_upload_ids,
                            "listing_id": listing_id,
                            "earliest_captured_at": earliest_iso,
                            "preferences": api_preferences,
                            "input_preview_image_id": preview_id,
                            "teamId": team_id,
                        },
                        id_token,
                    )
                    eid = enhance_result["id"]
                    try:
                        source_names = [p.name for p in bracket_images]
                        if source_names:
                            _firestore_patch(
                                f"{_FLD_ENHANCES}/{eid}",
                                {
                                    "sourceFilename": {_FLD_SV: source_names[0]},
                                    "sourceFilenames": {
                                        "arrayValue": {
                                            "values": [{_FLD_SV: name} for name in source_names]
                                        }
                                    },
                                },
                                access_token,
                                ["sourceFilename", "sourceFilenames"],
                            )
                    except Exception as exc:
                        log(f"  ⚠ Không lưu được tên ảnh nguồn cho enhance: {exc}", "warn")

                    names = ", ".join(p.name for p in bracket_images)
                    log(f"  ✔ Đã kích hoạt xử lý cho [{names}] ({eid[:8]})", "success")
                    return {
                        "index": g_idx,
                        "images": bracket_images,
                        "source_upload_ids": bracket_upload_ids,
                        "enhance_id": eid,
                        "listing_id": listing_id,
                        "last_logged_status": "",
                    }
                except Exception as e:
                    log(f"  ❌ Lỗi xử lý nhóm {g_idx + 1}: {e}", "error")
                    return None

            with ThreadPoolExecutor(max_workers=min(6, max(1, len(chunk)))) as trig_pool:
                for job_item in trig_pool.map(_trigger_bracket, enumerate(chunk)):
                    if job_item:
                        jobs.append(job_item)

        if not jobs:
            raise RuntimeError("Tất cả nhóm đều thất bại!")

        log(f"⏳ PHASE 2: Đang chờ {len(jobs)} nhóm xử lý...", "info")
        status_fn(f"Đang chờ Fotello xử lý: 0/{len(jobs)} nhóm xong")
        start_time = time.time()
        done_ids = set()
        last_token_refresh = time.time()
        STALL_TIMEOUT = 300
        for job in jobs:
            job["last_status_change"] = time.time()

        while len(done_ids) < len(jobs):
            if is_cancelled():
                log("⚠ Đã dừng bởi người dùng.", "warn")
                return 0
            elapsed = int(time.time() - start_time)
            if elapsed > POLL_TIMEOUT:
                log("⚠ Hết thời gian chờ xử lý!", "warn")
                break
            if time.time() - last_token_refresh > 300:
                try:
                    new_tokens = fotello_get_tokens(force_refresh=True)
                    access_token = new_tokens["access_token"]
                    id_token = new_tokens.get("id_token", id_token)
                    last_token_refresh = time.time()
                    log(f"  [{elapsed}s] Tokens refreshed", "info")
                except Exception:
                    pass

            pending_jobs = [j for j in jobs if j.get("enhance_id") not in done_ids]

            def _poll_one(job):
                eid = job.get("enhance_id")
                try:
                    doc = _firestore_get(f"{_FLD_ENHANCES}/{eid}", access_token)
                    fields = doc.get("fields", {})
                    status = fields.get(_FLD_STATUS, {}).get(_FLD_SV, "unknown")
                    return job, status, None
                except Exception as e:
                    return job, None, e

            if pending_jobs:
                with ThreadPoolExecutor(max_workers=min(8, len(pending_jobs))) as poll_pool:
                    poll_results = list(poll_pool.map(_poll_one, pending_jobs))

                for job, status, err in poll_results:
                    eid = job.get("enhance_id")
                    if err is not None:
                        if isinstance(err, urllib.error.HTTPError) and err.code == 403:
                            try:
                                new_tokens = fotello_get_tokens(force_refresh=True)
                                access_token = new_tokens["access_token"]
                            except Exception:
                                pass
                        log(f"  [{elapsed}s] Poll error for {str(eid)[:8]}: {err}", "warn")
                        continue
                    prev = job.get("last_logged_status", "")
                    if status != prev:
                        label = f"[{job.get('index', 0) + 1}]"
                        log(f"  {label} [{elapsed}s] {status}", "info")
                        job["last_logged_status"] = status
                        job["last_status_change"] = time.time()
                        status_fn(
                            f"Fotello: nhóm {job.get('index', 0) + 1} đang ở trạng thái {status} ({len(done_ids)}/{len(jobs)} nhóm xong)"
                        )
                    if status in ("enhance_success", "merge_success", "enhance_failed", "failed"):
                        done_ids.add(eid)
                        status_fn(
                            f"Đang chờ Fotello xử lý: {len(done_ids)}/{len(jobs)} nhóm xong ({status})"
                        )
                        with progress_lock:
                            uploaded[0] += 1
                        progress_fn(uploaded[0], total_work)

            for job in jobs:
                eid = job.get("enhance_id")
                if eid in done_ids:
                    continue
                last_change = job.get("last_status_change", start_time)
                if time.time() - last_change > STALL_TIMEOUT:
                    stalled = f"[{job.get('index', 0) + 1}]"
                    log(
                        f"  ⚠ {stalled} Nhóm bị treo ({job.get('last_logged_status', 'unknown')}), bỏ qua...",
                        "warn",
                    )
                    job["last_logged_status"] = "skipped_stalled"
                    done_ids.add(eid)
                    status_fn(f"Đang chờ Fotello xử lý: {len(done_ids)}/{len(jobs)} nhóm xong")

            if len(done_ids) < len(jobs):
                remaining = len(jobs) - len(done_ids)
                if elapsed % 30 < POLL_INTERVAL:
                    log(f"  ⏳ [{elapsed}s] Đang chờ {remaining} nhóm còn lại...", "info")
                time.sleep(POLL_INTERVAL)

        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        jobs = sorted(jobs, key=lambda j: j.get("index", 0))
        skipped = 0
        download_jobs = []
        for job in jobs:
            status = job.get("last_logged_status", "")
            if status in ("enhance_success", "merge_success"):
                download_jobs.append(job)
            else:
                log(f"  [{job.get('index', 0) + 1}] SKIP ({status})", "warn")
                skipped += 1

        expected = len(download_jobs)
        log(f"📦 PHASE 3: Tải {expected} kết quả (song song)...", "info")
        status_fn(f"Đang tải kết quả: 0/{expected} ảnh")
        try:
            new_tokens = fotello_get_tokens()
            access_token = new_tokens["access_token"]
        except Exception:
            pass

        results = []
        downloaded_count = 0
        dl_counter = [0]

        def _dl_phase3(job):
            if is_cancelled():
                return None
            try:
                out = _download_single_enhance(
                    job["enhance_id"],
                    access_token,
                    out_dir,
                    job["images"][0].stem,
                    log,
                    fullsize=True,
                    is_cancelled=is_cancelled,
                    source_upload_ids=job.get("source_upload_ids"),
                    source_paths=[str(path) for path in job.get("images", [])],
                )
                if preferences.get("convert_to_srgb"):
                    try:
                        convert_image_to_srgb(out, target_dpi=300)
                        log(f"  🎨 Đã chuyển ảnh tải về sang sRGB: {out.name}", "info")
                    except Exception as color_error:
                        log(f"  ⚠ Không thể chuyển {out.name} sang sRGB: {color_error}", "warn")
                with progress_lock:
                    dl_counter[0] += 1
                    cur_dl = dl_counter[0]
                    uploaded[0] += 1
                    cur_up = uploaded[0]
                status_fn(f"Đang tải kết quả: {cur_dl}/{expected} ảnh")
                progress_fn(cur_up, total_work)
                return str(out)
            except Exception as e:
                log(f"  ❌ Download error: {e}", "error")
                return None

        if download_jobs:
            with ThreadPoolExecutor(max_workers=min(6, len(download_jobs))) as dl_pool:
                for res_path in dl_pool.map(_dl_phase3, download_jobs):
                    if res_path:
                        results.append(res_path)
        downloaded_count = len(results)

        log(f"\n🏁 Hoàn tất: Đã tải {downloaded_count} ảnh về máy.", "success")
        status_fn(f"Hoàn tất: đã tải {downloaded_count} ảnh")
        progress_fn(total_work, total_work)
        log(f"📁 Thư mục kết quả: {out_dir}", "info")
        return {
            "count": downloaded_count,
            "files": results,
            "output_dir": str(out_dir),
        }
    finally:
        if temp_dir_obj is not None:
            try:
                temp_dir_obj.cleanup()
            except Exception:
                pass
