import hashlib
import json
import mimetypes
import os
import platform
import random
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tempfile import NamedTemporaryFile

import requests
from PIL import Image, ImageOps

UPSCALE_API_URL = "https://autofotello-upscale-api.phans3806.workers.dev"
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
TERMINAL_FAILURES = {"FAILED", "TIMED_OUT", "ERROR", "CANCELLED"}
PROVIDERS = {"local", "replicate", "runpod"}
LOCAL_ENGINE_MARKER = "installed_engine.json"
IMAGE_FINALIZE_LOCK = threading.Lock()


class UpscaleError(Exception):
    pass


def _normalize_files(files):
    result = []
    for item in files or []:
        if not item or not isinstance(item, (str, os.PathLike)):
            continue
        path = Path(item).expanduser()
        if path.is_file():
            result.append(path.resolve())
    return result


def _content_type(path):
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed in {"image/jpeg", "image/png", "image/webp"}:
        return guessed
    return "application/octet-stream"


def _json_request(method, url, **kwargs):
    timeout = kwargs.pop("timeout", 60)
    max_retries = kwargs.pop("max_retries", 5)
    retry_log = kwargs.pop("retry_log", None)
    retry_label = kwargs.pop("retry_label", "request")

    for attempt in range(1, max_retries + 1):
        response = requests.request(method, url, timeout=timeout, **kwargs)
        try:
            payload = response.json()
        except Exception:
            payload = {}
        if response.ok and isinstance(payload, dict) and payload.get("ok") is not False:
            return payload

        message = payload.get("error") or payload.get("message") or response.text[:300]
        message_text = str(message).lower()
        retryable_429 = response.status_code == 429 or any(
            marker in message_text
            for marker in ("throttl", "rate limit", "too many requests", "resets in")
        )
        if retryable_429 and attempt < max_retries:
            retry_after = response.headers.get("Retry-After", "")
            try:
                base_delay = float(retry_after)
            except Exception:
                match = re.search(
                    r"(?:resets? in|retry after)\s*~?\s*(\d+(?:\.\d+)?)s",
                    str(message),
                    re.I,
                )
                base_delay = float(match.group(1)) if match else min(30, 2 ** attempt)
            delay = max(1.0, base_delay) + random.uniform(0.2, 1.2)
            if retry_log:
                retry_log(f"Rate limited; retrying {retry_label} in {delay:.0f}s...", "warn")
            time.sleep(delay)
            continue
        raise UpscaleError(f"HTTP {response.status_code}: {message}")

    raise UpscaleError("Request retry limit reached")


def _cancel_job(api_url, job_id, job_token, runpod_id):
    if not job_id or not job_token:
        return
    try:
        _json_request(
            "POST",
            f"{api_url}/v1/jobs/{job_id}/cancel",
            json={"job_token": job_token, "runpod_id": runpod_id},
            timeout=30,
        )
    except Exception:
        return


def _format_for_path(path):
    ext = path.suffix.lower()
    if ext == ".png":
        return "PNG"
    if ext == ".webp":
        return "WEBP"
    return "JPEG"


def _platform_key():
    sys_platform = platform.system().lower()
    if sys_platform == "darwin":
        return "macos"
    if sys_platform == "windows":
        return "windows"
    return "unknown"


def _app_data_dir():
    override = os.environ.get("AUTOFOTELLO_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if platform.system().lower() == "windows":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return base / "AutoFotello"
    if platform.system().lower() == "darwin":
        return Path.home() / "Library" / "Application Support" / "AutoFotello"
    return Path.home() / ".autofotello"


def _engines_dir():
    path = _app_data_dir() / "engines"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _engine_dir(engine_id):
    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "_", str(engine_id or "engine"))[:120]
    return _engines_dir() / safe_id


def _engine_marker_path(engine_id):
    return _engine_dir(engine_id) / LOCAL_ENGINE_MARKER


def detect_local_capability():
    system = _platform_key()
    gpu = {"vendor": None, "name": None, "vram_mb": None, "driver": None}
    if system == "windows":
        gpu = {**gpu, **(_detect_windows_nvidia() or _detect_windows_gpu() or {})}
    elif system == "macos":
        gpu = {**gpu, **(_detect_macos_gpu() or {})}

    return {
        "platform": system,
        "arch": platform.machine().lower(),
        "gpu": gpu,
        "supports_local": system in {"windows", "macos"},
    }


def _detect_windows_nvidia():
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return None
    try:
        proc = subprocess.run(
            [
                nvidia_smi,
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        row = proc.stdout.strip().splitlines()[0]
        parts = [part.strip() for part in row.split(",")]
        return {
            "vendor": "nvidia",
            "name": parts[0] if parts else "NVIDIA GPU",
            "vram_mb": int(float(parts[1])) if len(parts) > 1 and parts[1] else None,
            "driver": parts[2] if len(parts) > 2 else None,
        }
    except Exception:
        return None


def _detect_windows_gpu():
    try:
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_VideoController | Select-Object -First 1 Name,AdapterRAM,DriverVersion | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        data = json.loads(proc.stdout)
        name = str(data.get("Name") or "")
        adapter_ram = data.get("AdapterRAM")
        vendor = (
            "nvidia"
            if "nvidia" in name.lower()
            else "amd"
            if ("amd" in name.lower() or "radeon" in name.lower())
            else "intel"
            if "intel" in name.lower()
            else None
        )
        return {
            "vendor": vendor,
            "name": name or None,
            "vram_mb": int(adapter_ram // 1048576) if adapter_ram and int(adapter_ram) > 0 else None,
            "driver": data.get("DriverVersion"),
        }
    except Exception:
        return None


def _detect_macos_gpu():
    try:
        proc = subprocess.run(
            ["system_profiler", "SPDisplaysDataType", "-json"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        data = json.loads(proc.stdout)
        displays = data.get("SPDisplaysDataType") or []
        if not displays:
            return None
        first = displays[0]
        return {
            "vendor": "apple",
            "name": first.get("sppci_model") or first.get("_name") or "Apple GPU",
            "vram_mb": None,
            "driver": first.get("spdisplays_metal") or "metal",
        }
    except Exception:
        return None


def get_engine_manifest(hwid=None, api_url=UPSCALE_API_URL):
    if not hwid:
        raise UpscaleError("Missing device ID.")
    payload = _json_request(
        "POST",
        f"{api_url.rstrip('/')}/v1/engines",
        json={"hwid": hwid},
        timeout=30,
    )
    return payload.get("manifest") or {"version": 1, "engines": []}


def _choose_engine(manifest, capability, requested_id=None):
    engines = (manifest or {}).get("engines") or []
    platform_key = (capability or {}).get("platform")
    gpu = (capability or {}).get("gpu") or {}
    candidates = [
        engine
        for engine in engines
        if isinstance(engine, dict) and engine.get("platform") == platform_key
    ]
    if requested_id:
        return next((engine for engine in candidates if engine.get("id") == requested_id), None)
    if platform_key == "windows" and gpu.get("vendor") == "nvidia":
        vram_mb = int(gpu.get("vram_mb") or 0)
        pro = [
            engine
            for engine in candidates
            if engine.get("backend") == "pytorch-cuda"
            and (engine.get("requires_vendor") or "nvidia") == "nvidia"
            and vram_mb >= int(engine.get("min_vram_mb") or 0)
        ]
        if pro:
            return sorted(pro, key=lambda item: int(item.get("min_vram_mb") or 0), reverse=True)[0]
    compatible = [engine for engine in candidates if engine.get("backend") == "ncnn-vulkan"]
    return compatible[0] if compatible else (candidates[0] if candidates else None)


def local_engine_status(hwid=None, api_url=UPSCALE_API_URL):
    capability = detect_local_capability()
    manifest = {"engines": []}
    if hwid:
        try:
            manifest = get_engine_manifest(hwid=hwid, api_url=api_url)
        except Exception:
            manifest = {"engines": []}
    recommended = _choose_engine(manifest, capability)
    installed = []
    for marker in _engines_dir().glob(f"*/{LOCAL_ENGINE_MARKER}"):
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            if isinstance(data, dict) and _engine_executable(data).exists():
                installed.append(data)
        except Exception:
            continue
    ready = bool(installed)
    if recommended:
        ready = any(item.get("id") == recommended.get("id") for item in installed)
    return {
        "ok": True,
        "capability": capability,
        "recommended_engine": recommended,
        "installed_engines": installed,
        "ready": ready,
        "engines_dir": str(_engines_dir()),
    }


def install_local_engine(
    hwid=None,
    engine_id=None,
    api_url=UPSCALE_API_URL,
    log=None,
    progress_fn=None,
):
    if not hwid:
        raise UpscaleError("Missing device ID.")
    capability = detect_local_capability()
    manifest = get_engine_manifest(hwid=hwid, api_url=api_url)
    selected = _choose_engine(manifest, capability, engine_id)
    if not selected:
        raise UpscaleError("No compatible local upscale engine is available for this machine.")

    payload = _json_request(
        "POST",
        f"{api_url.rstrip('/')}/v1/engines/download",
        json={"hwid": hwid, "engine_id": selected.get("id") or engine_id},
        timeout=30,
    )
    engine = payload.get("engine") or selected
    download_url = str(engine.get("download_url") or "")
    if not download_url.startswith("https://"):
        raise UpscaleError("Engine download URL is invalid.")
    expected_sha = str(engine.get("sha256") or "").lower()
    if not re.fullmatch(r"[a-f0-9]{64}", expected_sha):
        raise UpscaleError("Engine package checksum is missing.")
    size_bytes = int(engine.get("size_bytes") or 0)
    if size_bytes <= 0 or size_bytes > 5000000000:
        raise UpscaleError("Engine package size is invalid.")

    if log:
        log(
            f"Downloading local engine: {engine.get('name') or engine.get('id')} ({size_bytes / 1048576:.0f} MB)...",
            "info",
        )
    tmp_zip = _download_to_temp(download_url, size_bytes, progress_fn=progress_fn)
    try:
        actual_sha = _sha256_file(tmp_zip).lower()
        if actual_sha != expected_sha:
            raise UpscaleError("Engine package checksum mismatch.")
        target_dir = _engine_dir(engine.get("id"))
        staging_dir = target_dir.with_name(f".{target_dir.name}.{uuid.uuid4().hex}.tmp")
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True, exist_ok=True)
        _safe_extract_zip(tmp_zip, staging_dir)
        _normalize_engine_root(staging_dir, engine)
        marker = {
            **engine,
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "install_dir": str(target_dir),
            "capability_at_install": capability,
        }
        (staging_dir / LOCAL_ENGINE_MARKER).write_text(
            json.dumps(marker, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if target_dir.exists():
            shutil.rmtree(target_dir)
        staging_dir.rename(target_dir)
        _make_engine_executable(marker)
        if log:
            log(f"Local engine ready: {marker.get('name') or marker.get('id')}", "success")
        return {
            "ok": True,
            "engine": marker,
            "status": local_engine_status(hwid=hwid, api_url=api_url),
        }
    finally:
        Path(tmp_zip).unlink(missing_ok=True)


def _download_to_temp(url, expected_size, progress_fn=None):
    with NamedTemporaryFile(prefix="autofotello-engine-", suffix=".zip", delete=False) as tmp:
        path = Path(tmp.name)
        with requests.get(url, stream=True, timeout=120) as response:
            response.raise_for_status()
            total = int(response.headers.get("Content-Length") or expected_size or 0)
            downloaded = 0
            for chunk in response.iter_content(1048576):
                if not chunk:
                    continue
                downloaded += len(chunk)
                if expected_size and downloaded > expected_size + 5242880:
                    raise UpscaleError("Engine download is larger than expected.")
                tmp.write(chunk)
                if progress_fn:
                    progress_fn(downloaded, total)
    return path


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1048576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extract_zip(zip_path, target_dir):
    target = Path(target_dir).resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            member_path = target / member.filename
            resolved = member_path.resolve()
            if resolved != target and not str(resolved).startswith(str(target) + os.sep):
                raise UpscaleError("Engine package contains unsafe paths.")
            if member.file_size > 5000000000:
                raise UpscaleError("Engine package contains an oversized file.")
        archive.extractall(target)


def _normalize_engine_root(staging_dir, engine):
    exe = engine.get("executable") or (
        "realesrgan-ncnn-vulkan.exe"
        if engine.get("platform") == "windows"
        else "realesrgan-ncnn-vulkan"
    )
    if (staging_dir / exe).exists():
        return
    matches = list(staging_dir.glob(f"**/{exe}"))
    if not matches:
        raise UpscaleError(f"Engine executable was not found: {exe}")
    root = matches[0].parent
    for item in root.iterdir():
        dest = staging_dir / item.name
        if not dest.exists():
            shutil.move(str(item), str(dest))


def _engine_executable(engine):
    install_dir = Path(engine.get("install_dir") or _engine_dir(engine.get("id")))
    executable = engine.get("executable")
    if not executable:
        if engine.get("backend") == "pytorch-cuda":
            executable = "run_local_upscale.exe"
        else:
            executable = (
                "realesrgan-ncnn-vulkan.exe"
                if engine.get("platform") == "windows"
                else "realesrgan-ncnn-vulkan"
            )
    return install_dir / executable


def _make_engine_executable(engine):
    exe = _engine_executable(engine)
    if exe.exists() and platform.system().lower() != "windows":
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _installed_engine(engine_id=None, hwid=None, api_url=UPSCALE_API_URL):
    status = local_engine_status(hwid=hwid, api_url=api_url)
    recommended = status.get("recommended_engine") or {}
    wanted = engine_id or recommended.get("id")
    for engine in status.get("installed_engines") or []:
        if not wanted or engine.get("id") == wanted:
            return engine
    return None


def _save_image(image, output_path, optimize_size=False):
    output_format = _format_for_path(output_path)
    options = {}
    if output_format == "JPEG":
        if optimize_size:
            options.update({"quality": 90, "subsampling": 2, "optimize": True, "progressive": True})
        else:
            options.update({"quality": 98, "subsampling": 0})
        image = image.convert("RGB")
    elif output_format == "WEBP":
        options.update(
            {"quality": 90 if optimize_size else 98, "method": 6 if optimize_size else 4}
        )
        image = image.convert("RGB")
    elif output_format == "PNG":
        options.update({"optimize": bool(optimize_size)})
    image.save(output_path, format=output_format, **options)


def _resample_lanczos():
    return getattr(getattr(Image, "Resampling", Image), "LANCZOS")


def _natural_strength(value):
    try:
        return max(0.0, min(1.0, float(0.65 if value is None else value)))
    except Exception:
        return 0.65


def _original_display_image(source_path):
    with Image.open(source_path) as source_image:
        return ImageOps.exif_transpose(source_image).convert("RGB")


def _apply_texture_blend(upscaled, original, alpha, use_high_pass=False):
    if use_high_pass:
        from PIL import ImageChops, ImageFilter

        Y_up, Cb, Cr = upscaled.convert("YCbCr").split()
        Y_orig, _, _ = original.convert("YCbCr").split()
        Y_blur = Y_orig.filter(ImageFilter.GaussianBlur(radius=1.5))
        texture_weight = 1.0 - alpha
        hf = ImageChops.subtract(Y_orig, Y_blur, scale=1.0, offset=128)
        gray128 = Image.new("L", Y_up.size, 128)
        hf_scaled = Image.blend(gray128, hf, texture_weight)
        Y_new = ImageChops.add(Y_up, hf_scaled, scale=1.0, offset=-128)
        return Image.merge("YCbCr", (Y_new, Cb, Cr)).convert("RGB")
    return Image.blend(original, upscaled, alpha)


def _finalize_upscale_output(
    temp_path,
    output_path,
    source_path=None,
    keep_original_size=False,
    natural_strength=1.0,
    requested_scale=None,
    blend_original=True,
    true_original_dir=None,
):
    try:
        with IMAGE_FINALIZE_LOCK:
            with Image.open(temp_path) as upscaled_image:
                upscaled = ImageOps.exif_transpose(upscaled_image).convert("RGB")

            true_original_path = None
            if true_original_dir and source_path:
                tod = Path(true_original_dir)
                if tod.is_dir():
                    candidate = tod / source_path.name
                    if candidate.is_file():
                        true_original_path = candidate
                    else:
                        for f in tod.iterdir():
                            if f.is_file() and source_path.stem.startswith(f.stem):
                                true_original_path = f
                                break

            texture_source = true_original_path or source_path
            if keep_original_size and source_path:
                original = _original_display_image(texture_source)
                original_size = original.size
                if upscaled.size != original_size:
                    upscaled = upscaled.resize(original_size, _resample_lanczos())
                alpha = _natural_strength(natural_strength)
                if blend_original and alpha < 1:
                    upscaled = _apply_texture_blend(
                        upscaled,
                        original,
                        alpha,
                        use_high_pass=bool(true_original_path),
                    )
            elif source_path:
                if requested_scale:
                    original_size = _original_display_image(source_path).size
                    target_size = (
                        original_size[0] * requested_scale,
                        original_size[1] * requested_scale,
                    )
                    if upscaled.size != target_size:
                        upscaled = upscaled.resize(target_size, _resample_lanczos())
                if blend_original:
                    original_resized = _original_display_image(texture_source).resize(
                        upscaled.size, _resample_lanczos()
                    )
                    alpha = _natural_strength(natural_strength)
                    if alpha < 1:
                        upscaled = _apply_texture_blend(
                            upscaled,
                            original_resized,
                            alpha,
                            use_high_pass=bool(true_original_path),
                        )

            _save_image(upscaled, output_path, optimize_size=keep_original_size)
            return output_path
    except MemoryError as exc:
        raise UpscaleError(
            "May khong du RAM de luu anh upscale. Hay thu chay it anh hon moi lan."
        ) from exc


def _process_one(
    path,
    output_path,
    mode,
    strength,
    provider,
    hwid,
    api_url,
    stop_event,
    log,
    keep_original_size=False,
    blend_original=True,
    true_original_dir=None,
    credit_callback=None,
):
    if stop_event and stop_event.is_set():
        return ("cancelled", None)

    created = _json_request(
        "POST",
        f"{api_url}/v1/jobs",
        json={
            "hwid": hwid,
            "filename": path.name,
            "content_type": _content_type(path),
            "size_bytes": path.stat().st_size,
            "provider": provider,
            "mode": mode,
            "strength": strength,
        },
        timeout=30,
        retry_log=log,
        retry_label=path.name,
    )
    job_id = created["job_id"]
    job_token = created["job_token"]
    runpod_id = None

    try:
        size_mb = path.stat().st_size / 1048576
        if log:
            log(f"Uploading {path.name} ({size_mb:.1f} MB)...", "info")
        with path.open("rb") as image_file:
            upload = requests.put(
                created["upload_url"],
                data=image_file,
                headers={
                    "Content-Type": _content_type(path),
                    "Content-Length": str(path.stat().st_size),
                },
                timeout=600,
            )
        if not upload.ok:
            raise UpscaleError(f"Upload failed ({upload.status_code}): {upload.text[:300]}")
        if log:
            log(f"Upload complete: {path.name}", "info")
        if stop_event and stop_event.is_set():
            return ("cancelled", None)

        provider_name = "Replicate" if provider == "replicate" else "RunPod"
        if log:
            log(f"Starting {provider_name} job: {path.name}", "info")
        started = _json_request(
            "POST",
            f"{api_url}/v1/jobs/{job_id}/start",
            json={"job_token": job_token},
            timeout=60,
            retry_log=log,
            retry_label=path.name,
        )
        runpod_id = started.get("runpod_id")
        if credit_callback and started.get("credit_balance") is not None:
            credit_callback(started.get("credit_balance"))

        last_state = None
        while True:
            if stop_event and stop_event.is_set():
                _cancel_job(api_url, job_id, job_token, runpod_id)
                return ("cancelled", None)
            status = _json_request(
                "POST",
                f"{api_url}/v1/jobs/{job_id}/status",
                json={"job_token": job_token, "runpod_id": runpod_id},
                timeout=30,
                retry_log=log,
                retry_label=path.name,
            )
            state = str(status.get("status", "UNKNOWN")).upper()
            if state != last_state:
                last_state = state
                if log:
                    log(f"{provider_name} {state}: {path.name}", "info")
            if state == "COMPLETED":
                break
            if state in TERMINAL_FAILURES:
                raise UpscaleError(status.get("error") or f"{provider_name} job {state}")
            if stop_event:
                stop_event.wait(2)
            else:
                time.sleep(2)

        download_url = status.get("download_url")
        if not download_url:
            raise UpscaleError("Backend completed without a download URL")

        temp_path = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.part")
        try:
            with requests.get(download_url, stream=True, timeout=600) as response:
                response.raise_for_status()
                with temp_path.open("wb") as output_file:
                    for chunk in response.iter_content(1048576):
                        if chunk:
                            output_file.write(chunk)
            if output_path.exists():
                temp_path.unlink(missing_ok=True)
                return ("skipped", None)
            _finalize_upscale_output(
                temp_path,
                output_path,
                source_path=path,
                keep_original_size=keep_original_size,
                natural_strength=strength,
                requested_scale=4 if mode == "upscale_4x" else 2,
                blend_original=blend_original,
                true_original_dir=true_original_dir,
            )
        finally:
            temp_path.unlink(missing_ok=True)
        return ("saved", output_path)
    finally:
        if stop_event and stop_event.is_set():
            _cancel_job(api_url, job_id, job_token, runpod_id)


def _local_output_format(path):
    output_format = _format_for_path(path)
    return "jpg" if output_format == "JPEG" else output_format.lower()


def _default_local_tile(engine):
    if engine.get("backend") == "ncnn-vulkan":
        return 512 if engine.get("platform") == "macos" else 256
    return 128


def _should_log_local_engine_line(text, progress_state):
    if not text:
        return False
    progress_match = re.fullmatch(r"(\d+(?:\.\d+)?)%", text)
    if not progress_match:
        if text.startswith("["):
            return False
        return (
            "error" in text.lower()
            or "warning" in text.lower()
            or "failed" in text.lower()
        )

    pct = float(progress_match.group(1))
    bucket = int(pct // 5) * 5
    if bucket >= 100:
        bucket = 100
    if bucket <= progress_state["last_bucket"]:
        return False
    progress_state["last_bucket"] = bucket
    return True


def _run_local_engine(
    path,
    output_path,
    engine,
    stop_event=None,
    log=None,
    keep_original_size=False,
    natural_strength=0.75,
    mode="upscale_2x",
    blend_original=True,
    true_original_dir=None,
):
    if stop_event and stop_event.is_set():
        return ("cancelled", None)
    exe = _engine_executable(engine)
    if not exe.exists():
        raise UpscaleError("Local engine is not installed.")

    temp_path = output_path.with_name(
        f".{output_path.name}.{uuid.uuid4().hex}.local.{output_path.suffix.lstrip('.') or 'jpg'}"
    )
    backend = engine.get("backend")
    tile = int(engine.get("tile") or _default_local_tile(engine))
    if backend == "ncnn-vulkan":
        model_dir = Path(engine.get("install_dir") or exe.parent) / "models"
        command = [
            str(exe),
            "-i",
            str(path),
            "-o",
            str(temp_path),
            "-n",
            str(engine.get("model") or "realesrgan-x4plus"),
            "-s",
            "4",
            "-t",
            str(tile),
            "-m",
            str(model_dir),
            "-f",
            _local_output_format(output_path),
        ]
    elif backend == "pytorch-cuda":
        command = [
            str(exe),
            "--input",
            str(path),
            "--output",
            str(temp_path),
            "--model",
            str(engine.get("model") or "RealESRGAN_x2plus"),
            "--scale",
            str(engine.get("scale") or 2),
            "--tile",
            str(tile),
        ]
    else:
        raise UpscaleError("Unsupported local engine backend.")

    try:
        if log:
            log(f"Local upscale: {path.name} (tile {tile})", "info")
        proc = subprocess.Popen(
            command,
            cwd=str(exe.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        progress_state = {"last_bucket": -5}
        while True:
            if stop_event and stop_event.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return ("cancelled", None)
            line = proc.stdout.readline() if proc.stdout else ""
            if line:
                text = line.strip()
                if log and _should_log_local_engine_line(text, progress_state):
                    log(text[:300], "info")
                continue
            if proc.poll() is not None:
                break
            time.sleep(0.1)

        remaining = proc.stdout.read() if proc.stdout else ""
        if proc.returncode != 0:
            details = (remaining or "").strip()
            raise UpscaleError(f"Local engine failed ({proc.returncode}): {details[:500]}")
        if not temp_path.exists():
            raise UpscaleError("Local engine did not produce an output.")
        if output_path.exists():
            return ("skipped", None)
        _finalize_upscale_output(
            temp_path,
            output_path,
            source_path=path,
            keep_original_size=keep_original_size,
            natural_strength=natural_strength,
            requested_scale=4
            if mode == "upscale_4x"
            else (2 if mode == "upscale_2x" else engine.get("scale")),
            blend_original=blend_original,
            true_original_dir=true_original_dir,
        )
        return ("saved", output_path)
    finally:
        temp_path.unlink(missing_ok=True)


def _process_images_local(
    input_files,
    output_dir,
    hwid,
    api_url,
    stop_event,
    log,
    progress_fn,
    keep_original_size=False,
    natural_strength=0.75,
    mode="upscale_2x",
    blend_original=True,
    true_original_dir=None,
):
    if hwid:
        get_engine_manifest(hwid=hwid, api_url=api_url)
    engine = _installed_engine(hwid=hwid, api_url=api_url)
    if not engine:
        raise UpscaleError(
            "Local engine is not installed. Download a compatible local engine first."
        )

    out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    saved_paths = []
    skipped = 0
    failed = 0
    cancelled = 0
    total = len(input_files)
    completed = 0
    if progress_fn:
        progress_fn(0, total)

    for path in input_files:
        output_path = out_dir / path.name
        if output_path.resolve() == path.resolve():
            raise UpscaleError(f"Output folder would overwrite the original file: {path.name}")
        if output_path.exists():
            skipped += 1
            completed += 1
            if log:
                log(f"Skipped existing file: {path.name}", "warn")
            if progress_fn:
                progress_fn(completed, total)
            continue

        try:
            state, saved_path = _run_local_engine(
                path,
                output_path,
                engine,
                stop_event=stop_event,
                log=log,
                keep_original_size=keep_original_size,
                natural_strength=natural_strength,
                mode=mode,
                blend_original=blend_original,
                true_original_dir=true_original_dir,
            )
            if state == "saved" and saved_path:
                saved_paths.append(str(saved_path))
                if log:
                    log(f"Saved: {saved_path.name}", "success")
            elif state == "skipped":
                skipped += 1
            else:
                cancelled += 1
        except Exception as exc:
            failed += 1
            if log:
                log(f"Failed {path.name}: {exc}", "error")

        completed += 1
        if progress_fn:
            progress_fn(completed, total)
        if stop_event and stop_event.is_set():
            break

    return {
        "total": total,
        "completed": completed,
        "saved": len(saved_paths),
        "skipped": skipped,
        "failed": failed,
        "cancelled": cancelled,
        "output_dir": str(out_dir),
        "saved_paths": saved_paths,
    }


def process_images(
    files,
    output_dir,
    mode="upscale_2x",
    strength=0.35,
    provider="runpod",
    hwid=None,
    api_url=UPSCALE_API_URL,
    stop_event=None,
    log=None,
    progress_fn=None,
    concurrency=5,
    keep_original_size=False,
    blend_original=True,
    true_original_dir=None,
    credit_callback=None,
):
    input_files = _normalize_files(files)
    if not input_files:
        raise UpscaleError("No input images were selected.")
    if mode not in ("upscale_2x", "upscale_4x"):
        raise UpscaleError("Only Upscale 2x or 4x modes are supported.")
    provider = str(provider or "runpod").strip().lower()
    if provider not in PROVIDERS:
        raise UpscaleError("Invalid upscale provider.")
    if not hwid:
        raise UpscaleError("Missing device ID.")
    strength = float(strength)
    if strength < 0 or strength > 1:
        raise UpscaleError("Strength must be between 0 and 1.")

    unsupported = [
        path.name for path in input_files if path.suffix.lower() not in SUPPORTED_EXTENSIONS
    ]
    if unsupported:
        raise UpscaleError("Only JPG, PNG and WebP are supported: " + ", ".join(unsupported))

    names = [path.name.lower() for path in input_files]
    duplicates = [name for name, count in Counter(names).items() if count > 1]
    if duplicates:
        raise UpscaleError(
            "Duplicate filenames cannot be saved without renaming: " + ", ".join(duplicates)
        )

    if provider == "local":
        return _process_images_local(
            input_files,
            output_dir,
            hwid,
            api_url.rstrip("/"),
            stop_event,
            log,
            progress_fn,
            keep_original_size=keep_original_size,
            natural_strength=strength,
            mode=mode,
            blend_original=blend_original,
            true_original_dir=true_original_dir,
        )

    out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    pending = []
    skipped = 0
    for path in input_files:
        output_path = out_dir / path.name
        if output_path.resolve() == path.resolve():
            raise UpscaleError(f"Output folder would overwrite the original file: {path.name}")
        if output_path.exists():
            skipped += 1
            if log:
                log(f"Skipped existing file: {path.name}", "warn")
        else:
            pending.append((path, output_path))

    total = len(input_files)
    completed = skipped
    failed = 0
    cancelled = 0
    saved_paths = []
    progress_lock = threading.Lock()
    if progress_fn:
        progress_fn(completed, total)

    if pending:
        workers = max(1, min(int(concurrency or 1), 8, len(pending)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="upscale") as executor:
            futures = {
                executor.submit(
                    _process_one,
                    path,
                    output_path,
                    mode,
                    strength,
                    provider,
                    hwid,
                    api_url.rstrip("/"),
                    stop_event,
                    log,
                    keep_original_size,
                    blend_original,
                    true_original_dir,
                    credit_callback,
                ): (path, output_path)
                for path, output_path in pending
            }
            for future in as_completed(futures):
                path, output_path = futures[future]
                try:
                    state, saved_path = future.result()
                    if state == "saved" and saved_path:
                        saved_paths.append(str(saved_path))
                        if log:
                            log(f"Saved: {saved_path.name}", "success")
                    elif state == "skipped":
                        skipped += 1
                        if log:
                            log(f"Skipped existing file: {path.name}", "warn")
                    else:
                        cancelled += 1
                except Exception as exc:
                    failed += 1
                    if log:
                        log(f"Failed {path.name}: {exc}", "error")
                with progress_lock:
                    completed += 1
                    if progress_fn:
                        progress_fn(completed, total)

    return {
        "total": total,
        "completed": completed,
        "saved": len(saved_paths),
        "skipped": skipped,
        "failed": failed,
        "cancelled": cancelled,
        "output_dir": str(out_dir),
        "saved_paths": saved_paths,
    }
