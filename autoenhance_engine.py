import os
import sys
import time
import requests
import json
import subprocess
import re
import tempfile
import shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

_API_BASE = "https://api.autoenhance.ai/v3"
_BATCH = 10
_MAX_API_WORKERS = 16
_MAX_DOWNLOAD_WORKERS = 16

_SHARED_HTTP_SESSION = requests.Session()
try:
    from requests.adapters import HTTPAdapter
    _adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32)
    _SHARED_HTTP_SESSION.mount("https://", _adapter)
    _SHARED_HTTP_SESSION.mount("http://", _adapter)
except Exception:
    pass


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
            img.save(str(dest_path), "JPEG", quality=92)
            return dest_path.exists()

        with Image.open(str(src_path)) as img:
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.save(str(dest_path), "JPEG", quality=92)
        return dest_path.exists()
    except Exception:
        return False


def get_image_dimensions_sips(file_path: Path):
    if sys.platform != "darwin":
        return None
    try:
        res = subprocess.run(
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(file_path)],
            capture_output=True,
            text=True,
            check=True,
        )
        out = res.stdout
        w_match = re.search(r"pixelWidth:\s*(\d+)", out)
        h_match = re.search(r"pixelHeight:\s*(\d+)", out)
        if w_match and h_match:
            return int(w_match.group(1)), int(h_match.group(1))
    except Exception:
        pass
    return None


def get_image_dimensions(file_path: Path):
    dims = get_image_dimensions_sips(file_path)
    if dims:
        return dims
    try:
        from PIL import Image

        with Image.open(str(file_path)) as img:
            return img.size
    except Exception:
        return None


def _upscale_to_original(image_path: Path, target_w: int, target_h: int, log_fn=None):
    """Upscale image to match original input resolution using Lanczos resampling."""
    try:
        from PIL import Image

        img = Image.open(str(image_path))
        cur_w, cur_h = img.size
        kwargs = {"quality": 100, "subsampling": 0, "dpi": (300, 300)}
        exif = img.info.get("exif")
        if exif:
            kwargs["exif"] = exif

        if cur_w >= target_w * 0.8 and cur_h >= target_h * 0.8:
            img.save(str(image_path), "JPEG", **kwargs)
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

        fmt = img.format or "JPEG"
        if fmt == "PNG" or str(image_path).lower().endswith(".png"):
            img_resized.save(str(image_path), "PNG", dpi=(300, 300))
        else:
            img_resized.save(str(image_path), "JPEG", **kwargs)
    except ImportError:
        if log_fn:
            log_fn("  ⚠ Pillow not installed, skip upscaling", "warn")
    except Exception as e:
        if log_fn:
            log_fn(f"  ⚠ Upscale failed: {e}", "warn")


def _process_downloaded_png(
    temp_png_path: Path, final_jpg_path: Path, target_dim=None, log_fn=None
):
    try:
        from PIL import Image

        with Image.open(str(temp_png_path)) as img:
            if img.mode != "RGB":
                img = img.convert("RGB")
            cur_w, cur_h = img.size
            if not target_dim and max(cur_w, cur_h) <= 2048:
                target_dim = (cur_w * 2, cur_h * 2)
            if target_dim:
                target_w, target_h = target_dim
                if cur_w < target_w * 0.8 or cur_h < target_h * 0.8:
                    resample = getattr(
                        Image, "LANCZOS", getattr(Image, "ANTIALIAS", Image.BICUBIC)
                    )
                    img = img.resize((target_w, target_h), resample)
                    try:
                        from PIL import ImageFilter

                        img = img.filter(
                            ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=3)
                        )
                    except Exception:
                        pass

            kwargs = {"quality": 100, "subsampling": 0, "dpi": (300, 300)}
            exif = img.info.get("exif")
            if exif:
                kwargs["exif"] = exif
            img.save(str(final_jpg_path), "JPEG", **kwargs)

        temp_png_path.unlink(missing_ok=True)
    except Exception as e:
        if log_fn:
            log_fn(f"  Lỗi chuyển đổi/làm nét {final_jpg_path.name}: {e}", "error")
        try:
            temp_png_path.rename(final_jpg_path)
        except Exception:
            pass


def _api_get(session, endpoint, token):
    url = f"{_API_BASE}{endpoint}"
    headers = {
        "x-api-key": token,
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }
    max_retries = 3
    for attempt in range(max_retries):
        try:
            resp = session.get(url, headers=headers, timeout=180)
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt == max_retries - 1:
                raise e
            time.sleep(2 ** (attempt + 1))
            session.close()
            session = requests.Session()


def _api_post(session, endpoint, data, token):
    url = f"{_API_BASE}{endpoint}"
    headers = {
        "x-api-key": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }
    max_retries = 3
    for attempt in range(max_retries):
        try:
            resp = session.post(url, headers=headers, json=data, timeout=180)
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt == max_retries - 1:
                raise e
            time.sleep(2 ** (attempt + 1))
            session.close()
            session = requests.Session()


def validate_token(token, log_fn=None):
    if not token:
        return False, "Token trống", None
    sess = requests.Session()
    try:
        resp = _api_get(sess, "/orders/", token)
        if log_fn:
            log_fn("✔ Đăng nhập Autoenhance thành công.", "success")
        return True, "Thành công", token
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (401, 403):
            return False, "Phiên đăng nhập hết hạn", None
        return False, f"Lỗi kết nối Autoenhance: {e}", None


def list_orders(token, log_fn=None):
    sess = requests.Session()
    all_orders = []
    offset = None
    while True:
        try:
            endpoint = f"/orders/?per_page=15&offset={offset}" if offset else "/orders/?per_page=15"
            data = _api_get(sess, endpoint, token)
            if isinstance(data, list):
                all_orders.extend(data)
                break
            orders = data.get("orders") or data.get("data") or []
            all_orders.extend(orders)
            pagination = data.get("pagination") or {}
            next_offset = pagination.get("next_offset")
            if not next_offset or next_offset == offset:
                break
            offset = next_offset
        except Exception as e:
            if log_fn:
                log_fn(f"Lỗi tải danh sách order ở offset {offset}: {e}", "error")
            break
    return all_orders


def get_order_details(token, order_id, log_fn=None):
    sess = requests.Session()
    return _api_get(sess, f"/orders/{order_id}", token)


def _upload_one_file(file_path, s3_info, stop_event):
    if stop_event and stop_event.is_set():
        return False, "Bị hủy"
    url = s3_info["url"]
    headers = {
        k: v
        for k, v in (s3_info.get("headers") or {}).items()
        if k.lower() != "host"
    }
    last_err = None
    for attempt in range(3):
        if stop_event and stop_event.is_set():
            return False, "Bị hủy"
        try:
            with open(file_path, "rb") as f:
                resp = _SHARED_HTTP_SESSION.put(url, headers=headers, data=f, timeout=120)
            resp.raise_for_status()
            return True, "OK"
        except Exception as e:
            last_err = str(e)
            time.sleep(2)
    return False, last_err


def upload_and_process(
    token,
    input_dir,
    savedir,
    order_name,
    options=None,
    log_fn=None,
    progress_fn=None,
    stop_event=None,
):
    if not token:
        raise ValueError("Vui lòng đăng nhập Autoenhance trước.")

    AUTOENHANCE_NATIVE_EXTS = {".jpg", ".jpeg"}
    valid_exts = {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".tif",
        ".tiff",
        ".cr2",
        ".cr3",
        ".nef",
        ".arw",
        ".dng",
        ".raw",
        ".heic",
        ".heif",
        ".avif",
        ".bmp",
        ".gif",
        ".psd",
    }

    raw_files = []
    if isinstance(input_dir, (list, tuple)):
        for f in input_dir:
            p = Path(f)
            if p.is_file() and p.suffix.lower() in valid_exts:
                raw_files.append(p)
    else:
        input_path = Path(input_dir)
        for f in sorted(input_path.iterdir()):
            if f.is_file() and f.suffix.lower() in valid_exts:
                raw_files.append(f)

    if not raw_files:
        if log_fn:
            log_fn("⚠ Không tìm thấy ảnh hợp lệ trong thư mục.", "warn")
        return

    files = []
    temp_dir = None
    try:
        need_conv = [rf for rf in raw_files if rf.suffix.lower() not in AUTOENHANCE_NATIVE_EXTS]
        if need_conv:
            temp_dir = tempfile.mkdtemp(prefix="autoenhance_conv_")
            if log_fn:
                log_fn(
                    f"🔄 Đang chuyển đổi song song {len(need_conv)} ảnh RAW/khác sang JPG...",
                    "info",
                )

            def _conv_worker(rf):
                if stop_event and stop_event.is_set():
                    return rf, None
                dest_path = Path(temp_dir) / f"{rf.stem}.jpg"
                conv_ok = _convert_to_jpg_sips(rf, dest_path) or _convert_to_jpg_pil(
                    rf, dest_path
                )
                return rf, (dest_path if conv_ok else None)

            conv_map = {}
            with ThreadPoolExecutor(max_workers=min(8, len(need_conv))) as executor:
                for rf, res_path in executor.map(_conv_worker, need_conv):
                    conv_map[rf] = res_path

            for rf in raw_files:
                if rf.suffix.lower() in AUTOENHANCE_NATIVE_EXTS:
                    files.append(rf)
                else:
                    res_path = conv_map.get(rf)
                    if res_path is not None:
                        files.append(res_path)
                    elif log_fn:
                        log_fn(f"❌ Không thể chuyển đổi {rf.name}, bỏ qua file này.", "error")
        else:
            files = list(raw_files)

        total = len(files)
        if total == 0:
            if log_fn:
                log_fn("⚠ Không có ảnh nào sẵn sàng để upload.", "warn")
            return

        sess = requests.Session()
        dimensions = {}
        for f in files:
            dims = get_image_dimensions(f)
            if dims:
                dimensions[f.name] = dims

        if log_fn:
            log_fn("Đang tạo order trên Autoenhance...", "info")
        order_payload = {"name": order_name}
        order_data = _api_post(sess, "/orders/", order_payload, token)
        order_id = order_data.get("order_id") or order_data.get("id")
        if not order_id:
            raise RuntimeError("Không nhận được order_id từ API Autoenhance.")

        try:
            meta_dir = Path.home() / ".autoenhance_meta"
            meta_dir.mkdir(parents=True, exist_ok=True)
            with open(meta_dir / f"{order_id}.json", "w") as mf:
                json.dump(dimensions, mf)
        except Exception:
            pass

        if log_fn:
            log_fn(f"Đang lấy link upload cho {total} ảnh...", "info")

        s3_infos = {}

        def _fetch_s3_info(f):
            if stop_event and stop_event.is_set():
                return f.name, None
            payload = {
                "order_id": order_id,
                "upload_type": "AWS",
                "filename": f.name,
            }
            try:
                return f.name, _api_post(_SHARED_HTTP_SESSION, "/uploads", payload, token)
            except Exception as e:
                if log_fn:
                    log_fn(f"Lỗi lấy link upload cho {f.name}: {e}", "warn")
                return f.name, None

        with ThreadPoolExecutor(max_workers=min(10, max(1, total))) as executor:
            for fname, s3_info in executor.map(_fetch_s3_info, files):
                if s3_info:
                    s3_infos[fname] = s3_info

        if log_fn:
            log_fn(f"Bắt đầu upload {len(s3_infos)} ảnh...", "info")

        done = 0
        if progress_fn:
            progress_fn(0, total)

        with ThreadPoolExecutor(max_workers=_MAX_API_WORKERS) as executor:
            futures = {
                executor.submit(_upload_one_file, f, s3_infos[f.name], stop_event): f.name
                for f in files
                if f.name in s3_infos
            }
            for future in as_completed(futures):
                if stop_event and stop_event.is_set():
                    break
                fname = futures[future]
                try:
                    ok, msg = future.result()
                    done += 1
                    if progress_fn:
                        progress_fn(done, total)
                    if ok:
                        if log_fn:
                            log_fn(f"  [{done}/{total}] Tải lên {fname} thành công", "success")
                    else:
                        if log_fn:
                            log_fn(f"  Lỗi tải lên {fname}: {msg}", "warn")
                except Exception as e:
                    if log_fn:
                        log_fn(f"  Lỗi: {e}", "error")
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)

    if stop_event and stop_event.is_set():
        if log_fn:
            log_fn("Đã hủy upload.", "warn")
        return

    if log_fn:
        log_fn("Đang kích hoạt xử lý ảnh...", "info")

    options = options or {}
    ai_ver = options.get("ai_version", "5.x")
    if ai_ver == "latest":
        ai_ver = "5.x"

    process_payload = {
        "ai_version": ai_ver,
        "enhance": True,
        "vertical_correction": bool(options.get("perspective_correction", True)),
        "lens_correction": bool(options.get("lens_correction", True)),
        "privacy": bool(options.get("auto_privacy", False)),
        "upscale": False,
    }

    sky_opt = options.get("sky_replacement", True)
    if isinstance(sky_opt, str):
        sky_opt_lower = sky_opt.lower()
        if sky_opt_lower == "off":
            process_payload["sky_replacement"] = False
            sky_bool = False
        else:
            process_payload["sky_replacement"] = True
            sky_bool = True
            if sky_opt_lower == "clear":
                process_payload["cloud_type"] = "CLEAR"
            elif sky_opt_lower == "low_cloud":
                process_payload["cloud_type"] = "LOW_CLOUD"
            elif sky_opt_lower == "neutral":
                process_payload["cloud_type"] = "LOW_CLOUD_LOW_SAT"
            elif sky_opt_lower == "high_cloud":
                process_payload["cloud_type"] = "HIGH_CLOUD"
            else:
                process_payload["cloud_type"] = "LOW_CLOUD"
    else:
        sky_bool = bool(sky_opt)
        process_payload["sky_replacement"] = sky_bool
        if sky_bool:
            process_payload["cloud_type"] = "LOW_CLOUD"

    win_opt = options.get("window_pull", False)
    if isinstance(win_opt, str):
        win_opt_lower = win_opt.lower()
        if win_opt_lower == "off":
            process_payload["window_pull_type"] = "NONE"
        elif win_opt_lower == "only_windows":
            process_payload["window_pull_type"] = "ONLY_WINDOWS"
        elif win_opt_lower == "windows_with_skies":
            process_payload["window_pull_type"] = "WINDOWS_WITH_SKIES"
        else:
            process_payload["window_pull_type"] = "NONE"
    else:
        win = bool(win_opt)
        if win:
            process_payload["window_pull_type"] = (
                "WINDOWS_WITH_SKIES" if sky_bool else "ONLY_WINDOWS"
            )
        else:
            process_payload["window_pull_type"] = "NONE"

    grass_opt = options.get("grass", False)
    if isinstance(grass_opt, str):
        process_payload["grass"] = "GREEN" if grass_opt.lower() == "green" else "AS_SHOT"
    else:
        process_payload["grass"] = "GREEN" if grass_opt else "AS_SHOT"

    tvs_opt = options.get("tv_blackout", False)
    if isinstance(tvs_opt, str):
        process_payload["tvs"] = (
            "BLACK_OUT" if tvs_opt.lower() == "black_out" else "AS_SHOT"
        )
    else:
        process_payload["tvs"] = "BLACK_OUT" if tvs_opt else "AS_SHOT"

    fireplace_opt = options.get("fireplace", False)
    if isinstance(fireplace_opt, str):
        process_payload["fire_in_fireplaces"] = (
            "ALIGHT" if fireplace_opt.lower() == "alight" else "AS_SHOT"
        )
    else:
        process_payload["fire_in_fireplaces"] = "ALIGHT" if fireplace_opt else "AS_SHOT"

    photographer_opt = options.get("photographer", False)
    if isinstance(photographer_opt, str):
        process_payload["photographer"] = (
            "REMOVE" if photographer_opt.lower() == "remove" else "AS_SHOT"
        )
    else:
        process_payload["photographer"] = "REMOVE" if photographer_opt else "AS_SHOT"

    process_payload["environment"] = "AS_SHOT"
    restage = bool(options.get("restage", False))
    if restage:
        process_payload["ai_version"] = "beta"

    if options.get("ai_version"):
        ai_v = str(options.get("ai_version")).lower()
        if ai_v in ("stable", "1"):
            process_payload["ai_version"] = "stable"
        elif ai_v in ("latest", "2"):
            process_payload["ai_version"] = "latest"
        elif ai_v in ("beta", "3"):
            process_payload["ai_version"] = "beta"

    if options.get("preset"):
        preset_name = str(options.get("preset")).lower()
        preset_mapping = {
            "warm": "c1824178-a113-43f3-a9b5-15caa91d93df",
            "vivid": "d8a5ecac-202e-4d1f-8ed4-39669d4f81cb",
            "natural": "b02886eb-a8e8-4ffc-bcd4-e9c901dddb52",
        }
        if preset_name in preset_mapping:
            process_payload["preset_id"] = preset_mapping[preset_name]
            if log_fn:
                log_fn(f"Đã chọn preset: {preset_name.capitalize()}", "info")
        elif preset_name not in ("latest", "stable", "beta"):
            try:
                presets = _api_get(sess, "/presets/", token)
                matched_id = None
                if isinstance(presets, list):
                    for p in presets:
                        if p.get("name") and p.get("name").lower() == preset_name:
                            matched_id = p.get("preset_id") or p.get("id")
                            break
                elif isinstance(presets, dict) and "presets" in presets:
                    for p in presets["presets"]:
                        if p.get("name") and p.get("name").lower() == preset_name:
                            matched_id = p.get("preset_id") or p.get("id")
                            break
                if matched_id:
                    process_payload["preset_id"] = matched_id
                    if log_fn:
                        log_fn(f"Đã chọn preset: {options.get('preset')}", "info")
            except Exception as pe:
                if log_fn:
                    log_fn(f"Không lấy được preset (Lỗi: {pe}), tiếp tục với mặc định.", "warn")

    try:
        process_resp = _api_post(sess, f"/orders/{order_id}/process", process_payload, token)
        if log_fn:
            log_fn("🎉 Kích hoạt xử lý thành công!", "success")
            log_fn("Đang chờ hệ thống Autoenhance xử lý ảnh...", "info")

        start_time = time.time()
        poll_timeout = 1800
        is_success = False

        while time.time() - start_time < poll_timeout:
            if stop_event and stop_event.is_set():
                break
            time.sleep(5)
            try:
                order_data = _api_get(sess, f"/orders/{order_id}", token)
                images = order_data.get("images") or []
                if images:
                    processing_count = sum(
                        1 for img in images if img.get("status") == "processing"
                    )
                    completed_count = len(images) - processing_count
                    if log_fn:
                        log_fn(
                            f"Trạng thái xử lý: Đã xong {completed_count}/{len(images)} ảnh (Đang xử lý: {processing_count})",
                            "info",
                        )
                    if processing_count == 0:
                        is_success = True
                        break
            except Exception as e:
                if log_fn:
                    log_fn(f"Đang kiểm tra lại trạng thái... ({e})", "warn")
                time.sleep(5)

        if stop_event and stop_event.is_set():
            if log_fn:
                log_fn("Đã dừng tiến trình theo dõi xử lý.", "warn")
            return

        if is_success:
            if log_fn:
                log_fn("✔ Dự án đã xử lý thành công! Bắt đầu tải ảnh về máy...", "success")
            batch_download(
                token,
                [order_id],
                savedir,
                log_fn=log_fn,
                progress_fn=progress_fn,
                stop_event=stop_event,
            )
        else:
            if log_fn:
                log_fn(
                    "Hệ thống Autoenhance xử lý quá thời gian quy định (30 phút) hoặc thất bại.",
                    "error",
                )
    except Exception as e:
        if log_fn:
            log_fn(f"Lỗi xử lý Autoenhance: {e}", "error")


def _download_one_file(url, dest_path, token, stop_event):
    if stop_event and stop_event.is_set():
        return False, "Bị hủy"
    if not url:
        return False, "Không có link tải"

    max_retries = 3
    for attempt in range(max_retries):
        try:
            headers = {
                "x-api-key": token,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            }
            resp = _SHARED_HTTP_SESSION.get(url, stream=True, headers=headers, timeout=30)
            if resp.status_code >= 500:
                if attempt < max_retries - 1:
                    time.sleep(3)
                    continue
            resp.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=262144):
                    if stop_event and stop_event.is_set():
                        return False, "Bị hủy"
                    if chunk:
                        f.write(chunk)
            return True, "OK"
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(3)
            else:
                return False, str(e)
    return False, "Thử tải lại thất bại"


def batch_download(
    token,
    order_ids,
    savedir,
    log_fn=None,
    progress_fn=None,
    stop_event=None,
    photo_ids=None,
):
    if not token:
        raise ValueError("Vui lòng đăng nhập Autoenhance trước.")

    sess = _SHARED_HTTP_SESSION
    dest = Path(savedir)
    dest.mkdir(parents=True, exist_ok=True)

    for order_id in order_ids:
        if stop_event and stop_event.is_set():
            break

        dimensions = {}
        try:
            meta_file = Path.home() / ".autoenhance_meta" / f"{order_id}.json"
            if meta_file.exists():
                with open(meta_file, "r") as mf:
                    dimensions = json.load(mf)
        except Exception:
            pass
        dim_by_stem = {
            os.path.splitext(orig_name)[0].lower(): dim
            for orig_name, dim in dimensions.items()
        }

        if log_fn:
            log_fn(f"Đang kiểm tra Order ID: {order_id}...", "info")

        try:
            order_data = _api_get(sess, f"/orders/{order_id}", token)
            images = order_data.get("images") or []
            if photo_ids:
                photo_ids_set = {str(pid) for pid in photo_ids}
                images = [
                    img
                    for img in images
                    if str(img.get("image_id") or img.get("id")) in photo_ids_set
                ]

            if not images:
                if log_fn:
                    log_fn(f"Order {order_id} không có ảnh nào.", "warn")
                continue

            total = len(images)
            done = 0
            if progress_fn:
                progress_fn(0, total)

            download_targets = []
            for img in images:
                if img.get("status") == "processing":
                    if log_fn:
                        log_fn(f"Ảnh {img.get('image_name')} đang xử lý...", "warn")
                    continue

                img_id = img.get("image_id") or img.get("id")
                dl_url = f"{_API_BASE}/images/{img_id}/enhanced?format=png&watermark=false&preview=true&quality=90&scale=1&max_width=8192"
                img_name = img.get("image_name") or f"{img_id}.png"
                name_without_ext, _ = os.path.splitext(img_name)
                temp_png_path = dest / f"{name_without_ext}_temp.png"
                final_jpg_path = dest / f"{name_without_ext}.jpg"
                download_targets.append((dl_url, temp_png_path, final_jpg_path))

            if not download_targets:
                if log_fn:
                    log_fn(
                        f"Chưa có ảnh nào sẵn sàng tải xuống cho Order {order_id}",
                        "warn",
                    )
                continue

            if log_fn:
                log_fn(f"Bắt đầu tải {len(download_targets)} ảnh...", "info")

            def _dl_and_format_worker(url, temp_path, final_path):
                ok, msg = _download_one_file(url, temp_path, token, stop_event)
                if ok and not (stop_event and stop_event.is_set()):
                    stem = final_path.stem.lower()
                    orig_dim = dim_by_stem.get(stem)
                    _process_downloaded_png(
                        temp_path, final_path, target_dim=orig_dim, log_fn=log_fn
                    )
                return ok, msg

            with ThreadPoolExecutor(max_workers=_MAX_DOWNLOAD_WORKERS) as executor:
                futures = {
                    executor.submit(
                        _dl_and_format_worker, url, temp_path, final_path
                    ): (temp_path, final_path)
                    for url, temp_path, final_path in download_targets
                }
                for future in as_completed(futures):
                    if stop_event and stop_event.is_set():
                        break
                    temp_path, final_path = futures[future]
                    fname = final_path.name
                    try:
                        ok, msg = future.result()
                        done += 1
                        if progress_fn:
                            progress_fn(done, total)
                        if ok:
                            if log_fn:
                                log_fn(
                                    f"  [{done}/{len(download_targets)}] {fname} (Thành công)",
                                    "success",
                                )
                        else:
                            if log_fn:
                                log_fn(f"  Lỗi tải {fname}: {msg}", "warn")
                    except Exception as e:
                        if log_fn:
                            log_fn(f"  Lỗi: {e}", "error")
        except Exception as e:
            if log_fn:
                log_fn(f"Lỗi khi xử lý Order {order_id}: {e}", "error")

    if log_fn:
        log_fn(f"Hoàn tất tải xuống thư mục: {dest}", "success")
