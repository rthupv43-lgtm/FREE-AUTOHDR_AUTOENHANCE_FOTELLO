import json
import base64
import os
import time
import urllib.request
import urllib.error
import urllib.parse
import tempfile
import uuid
import shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from PIL import Image
except Exception:
    Image = None

FIREBASE_API_KEY = "AIzaSyAZ4jatuFnEEsEomk6JgrNm95XGZ2WXCoY"
FUNCTIONS_BASE = "https://us-central1-realty-edit.cloudfunctions.net"
MAX_API_IMAGE_SIZE = 15 * 1024 * 1024  # 15728640
_anon_token_cache = {"token": None, "expires": 0}


def get_anonymous_token(log_fn=None):
    """Create a fresh anonymous Firebase user to bypass credit checks."""
    now = time.time()
    if _anon_token_cache["token"] and now < _anon_token_cache["expires"]:
        return _anon_token_cache["token"]

    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={FIREBASE_API_KEY}"
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps({"returnSecureToken": True}).encode(),
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            token = data["idToken"]
            _anon_token_cache["token"] = token
            _anon_token_cache["expires"] = now + 3500
            if log_fn:
                log_fn("🔓 Đã tạo phiên ẩn danh (Miễn phí) thành công.", "success")
            return token
    except Exception as e:
        if log_fn:
            log_fn(f"❌ Lỗi khi khởi tạo phiên miễn phí: {e}", "error")
        return None


def api_request(url, data=None, token=None, method="POST", timeout=120):
    raw_data = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=raw_data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")[:300]
        return {"error": f"HTTP {e.code} {e.reason} - {err_body}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def call_function(name, data, token, timeout=120):
    url = f"{FUNCTIONS_BASE}/{name}"
    parsed = api_request(url, data={"data": data}, token=token, timeout=timeout)
    return parsed.get("result", parsed)


def resize_image_for_api(filepath, max_bytes=MAX_API_IMAGE_SIZE, log_fn=None):
    """Resize/compress image if larger than max_bytes to avoid HTTP 413 errors."""
    filepath = Path(filepath)
    size = filepath.stat().st_size
    if size <= max_bytes:
        return str(filepath)

    if Image is None:
        if log_fn:
            log_fn(f"⚠️ Pillow chưa cài, bỏ qua resize ({size / 1024 / 1024:.1f}MB). Có thể lỗi 413.", "warn")
        return str(filepath)

    try:
        ratio = (max_bytes / size) ** 0.5
        with Image.open(filepath) as img:
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            new_w = int(img.width * ratio)
            new_h = int(img.height * ratio)
            img_resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            tmp.close()
            img_resized.save(tmp.name, "JPEG", quality=90)
            if log_fn:
                log_fn(
                    f"📐 Resize {size / 1024 / 1024:.1f}MB → {Path(tmp.name).stat().st_size / 1024 / 1024:.1f}MB ({new_w}x{new_h}) để gửi API",
                    "info",
                )
            return tmp.name
    except Exception as e:
        if log_fn:
            log_fn(f"⚠️ Không thể resize ảnh: {e}", "warn")
        return str(filepath)


def file_to_data_url(filepath):
    ext = os.path.splitext(filepath)[1].lower()
    mime = "image/png" if ext == ".png" else "image/jpeg"
    with open(filepath, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    return f"data:{mime};base64,{data}"


def run_free_upscale(file_path: Path, output_dir: Path, log_fn=None) -> bool:
    """Runs the free upscale tool for a single file."""
    if log_fn:
        log_fn(f"Đang xử lý Upscale cho: {file_path.name}...", "info")

    anon_token = get_anonymous_token(log_fn)
    if not anon_token:
        return False

    resized_path = resize_image_for_api(file_path, log_fn=log_fn)
    data_url = file_to_data_url(resized_path)
    if str(resized_path) != str(file_path):
        try:
            os.unlink(resized_path)
        except Exception:
            pass

    if log_fn:
        log_fn("Đang gửi dữ liệu lên máy chủ xử lý...", "info")

    MAX_RETRIES = 2
    for attempt in range(1, MAX_RETRIES + 1):
        if attempt > 1:
            _anon_token_cache["token"] = None
            anon_token = get_anonymous_token(log_fn)
            if not anon_token:
                return False
            if log_fn:
                log_fn(f"🔄 Thử lại lần {attempt}/{MAX_RETRIES}...", "info")

        result = call_function("upscaleImage", {"imageDataUrl": data_url}, anon_token, timeout=240)
        if result.get("dataUrl"):
            out_name = f"{file_path.stem}_upscaled{file_path.suffix}"
            out_path = output_dir / out_name
            b64 = result["dataUrl"].split("base64,")[1]
            with open(out_path, "wb") as f:
                f.write(base64.b64decode(b64))
            if log_fn:
                log_fn(f"✅ Upscale thành công: {out_name}", "success")
            return True

        err = result.get("error", "Unknown error")
        if log_fn:
            log_fn(f"⚠️ Upscale {file_path.name} lần {attempt} thất bại: {err}", "warn")

    if log_fn:
        log_fn(f"❌ Upscale {file_path.name} thất bại sau {MAX_RETRIES} lần thử.", "error")
    return False


def run_free_twilight(file_path: Path, output_dir: Path, options: dict, log_fn=None) -> bool:
    """Runs the free DTE (Virtual Twilight) tool for a single file."""
    if log_fn:
        log_fn(f"Đang xử lý DTE (Virtual Twilight) cho: {file_path.name}...", "info")

    anon_token = get_anonymous_token(log_fn)
    if not anon_token:
        return False

    resized_path = resize_image_for_api(file_path, log_fn=log_fn)
    data_url = file_to_data_url(resized_path)
    if str(resized_path) != str(file_path):
        try:
            os.unlink(resized_path)
        except Exception:
            pass

    dte_dir = output_dir / f"{file_path.stem}_DTE"
    dte_dir.mkdir(parents=True, exist_ok=True)

    mode = options.get("twiMode", "exterior") or "exterior"
    sky = options.get("twiSky", "colorful") or "colorful"
    prompt = (options.get("twiPrompt", "") or "").strip()

    payload = {
        "imageDataUrl": data_url,
        "mode": mode,
        "skyOption": sky,
        "additionalPrompt": prompt,
    }

    if log_fn:
        log_fn(f"Đang tạo ảo hóa ánh sáng (Mode: {mode}, Sky: {sky}). Vui lòng chờ...", "info")

    result = call_function("generateVirtualTwilight", payload, anon_token, timeout=360)
    results = result.get("results")
    if not results:
        err = result.get("error", "No results returned")
        if log_fn:
            log_fn(f"❌ Lỗi tạo DTE {file_path.name}: {err}", "error")
        return False

    success_count = 0
    saved_dte_paths = []

    for i, r in enumerate(results):
        if r.get("success") and r.get("dataUrl"):
            suffix = f"_dte{i + 1}" if len(results) > 1 else "_dte"
            out_name = f"{file_path.stem}{suffix}{file_path.suffix}"
            out_path = dte_dir / out_name
            b64 = r["dataUrl"].split("base64,")[1]
            with open(out_path, "wb") as f:
                f.write(base64.b64decode(b64))
            if log_fn:
                log_fn(f"✅ Tạo DTE thành công: {out_name}", "success")
            success_count += 1
            saved_dte_paths.append((out_path, out_name))
        else:
            err = r.get("error", "Không rõ nguyên nhân")
            if log_fn:
                log_fn(f"❌ Kết quả {i + 1} lỗi: {err}", "error")

    if options.get("autoUpscale") and saved_dte_paths:
        if log_fn:
            log_fn(f"⚡ Đang Upscale {len(saved_dte_paths)} ảnh DTE song song (Parallel)...", "info")

        def _upscale_one(args):
            out_path, out_name = args
            if log_fn:
                log_fn(f"🔄 Đang Upscale lần 1 cho: {out_name}...", "info")
            ok1 = run_free_upscale(out_path, dte_dir, log_fn)
            upscaled_path1 = dte_dir / f"{out_path.stem}_upscaled{out_path.suffix}"
            if ok1 and upscaled_path1.exists():
                try:
                    shutil.move(str(upscaled_path1), str(out_path))
                    if log_fn:
                        log_fn(f"🔄 Đang Upscale lần 2 (x4) cho: {out_name}...", "info")
                    ok2 = run_free_upscale(out_path, dte_dir, log_fn)
                    upscaled_path2 = dte_dir / f"{out_path.stem}_upscaled{out_path.suffix}"
                    if ok2 and upscaled_path2.exists():
                        shutil.move(str(upscaled_path2), str(out_path))
                        if log_fn:
                            log_fn(f"✅ Đã Upscale x4 thành công: {out_name}", "success")
                    else:
                        if log_fn:
                            log_fn(f"⚠ Upscale lần 2 lỗi, giữ bản x2 cho: {out_name}", "warn")
                except Exception as e:
                    if log_fn:
                        log_fn(f"❌ Lỗi ghi đè Upscale: {e}", "error")
            else:
                if log_fn:
                    log_fn(f"⚠ Upscale lần 1 thất bại, giữ ảnh DTE gốc: {out_name}", "warn")

        with ThreadPoolExecutor(max_workers=len(saved_dte_paths)) as executor:
            futures = [executor.submit(_upscale_one, item) for item in saved_dte_paths]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    if log_fn:
                        log_fn(f"❌ Lỗi Upscale song song: {e}", "error")

    if success_count == 0:
        if log_fn:
            log_fn("❌ Lỗi: Server trả về thành công nhưng không có ảnh hợp lệ.", "error")
        return False

    return True
