import os
import sys
import json
import re as _re
import time
import urllib.parse
import requests
import subprocess
import tempfile
import shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import browser_cookie3
except ImportError:
    browser_cookie3 = None

_AUTOHDR_BASE = 'https://www.autohdr.com'
_MAIN_FLOW_TARGET_USER_ID = 89486
_DIRECT_ACCOUNT_PLANS = {
    'enterprise-10k-monthly',
    'enterprise-20k-monthly',
    'enterprise-50k-monthly',
    'standard-yearly',
    'professional-yearly',
}
_BATCH = 10
_MAX_API_WORKERS = 20
_MAX_DOWNLOAD_WORKERS = 16
_UPSCALE_POLL_INTERVAL = 2
_UPSCALE_TIMEOUT = 300
_S3_BUCKET_HOST = 'image-upload-autohdr-j.s3.amazonaws.com'
_INPAINT_BATCH_SIZE = 12
_INPAINT_BATCH_DELAY = 0.1
_MAX_INPAINT_RETRIES = 3
_RETRY_BASE_DELAY = 1.0
_POST_INPAINT_WAIT = 1.0
_VERIFY_POLL_INTERVAL = 2.0
_VERIFY_MAX_POLLS = 10
_HE_BATCH_SIZE = 20
_HE_BATCH_DELAY = 0.3
_HE_POST_WAIT = 0.6
_HE_ADJ_BATCH_SIZE = 10
CF_WORKER_URL = 'https://autohdr-auth.phans3806.workers.dev'
CF_WORKER_SECRET = 'fotello-crack-super-secret-2026'
_DONOR_SCAN_START = 8000
_DONOR_SCAN_END = 110000
_DONOR_MIN_CREDITS = 200
_DONOR_SCAN_BATCH_SIZE = 5
_DONOR_SCAN_WORKERS = 3
_DONOR_MAX_SCAN_ATTEMPTS = 100
_DONOR_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
import random
_DONOR_POOL = []
_LAST_DONOR_INDEX = -1


def _get_image_dimensions(filepath: Path) -> tuple[int, int] | None:
    try:
        from PIL import Image
        with Image.open(filepath) as img:
            return img.size
    except Exception:
        return None


def _upscale_to_original(image_path: Path, target_w: int, target_h: int, log_fn=None):
    """Upscale image to match original input resolution using Lanczos resampling."""
    try:
        from PIL import Image
        img = Image.open(image_path)
        cur_w, cur_h = img.size

        kwargs = {'quality': 100, 'subsampling': 0, 'dpi': (300, 300)}
        exif = img.info.get('exif')
        if exif:
            kwargs['exif'] = exif

        if cur_w >= target_w * 0.8 and cur_h >= target_h * 0.8:
            img.save(image_path, 'JPEG', **kwargs)
            return

        resample = getattr(Image, 'LANCZOS', getattr(Image, 'ANTIALIAS', Image.BICUBIC))
        img_resized = img.resize((target_w, target_h), resample)
        from PIL import ImageFilter

        img_resized = img_resized.filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=3))
        fmt = (img.format or 'JPEG').upper()

        if fmt == 'PNG' or str(image_path).lower().endswith('.png'):
            img_resized.save(image_path, 'PNG', dpi=(300, 300))
        else:
            img_resized.save(image_path, 'JPEG', **kwargs)
    except ImportError:
        if log_fn:
            log_fn('  ⚠ Pillow not installed, skip upscaling', 'warn')
    except Exception as e:
        if log_fn:
            log_fn(f'  ⚠ Upscale failed: {e}', 'warn')


def _is_clean_photo_url(url: str) -> bool:
    if not url:
        return False
    path = urllib.parse.urlparse(url).path
    return '/full/' in path or '/processed/' in path


def _direct_full_photo_url(photo: dict) -> str:
    """Return a direct clean URL only when AutoHDR explicitly grants full access."""
    if not photo or not photo.get('can_view_full'):
        return ''
    for key in ('active_url', 'photo_url', 'upscale_url', 'display_url', 'url'):
        url = str(photo.get(key) or '')
        if _is_clean_photo_url(url):
            return url
    return ''


def _normalize_photo_filename(name) -> str:
    name = urllib.parse.unquote(str(name or ''))
    name = name.replace('/', '_').replace('\\', '_')
    return _re.sub(r'_300dpi_[A-Za-z0-9]+(?=(?:\.[^.]+)?$)', '', name)


def _safe_folder_name(name) -> str:
    name = _re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(name or '').strip())
    name = _re.sub(r'\s+', ' ', name).strip(' .')
    return name[:120]


def _find_value(data, key: str):
    if isinstance(data, dict):
        value = data.get(key)
        if value not in (None, ''):
            return value
        for child in data.values():
            found = _find_value(child, key)
            if found not in (None, ''):
                return found
    elif isinstance(data, list):
        for child in data:
            found = _find_value(child, key)
            if found not in (None, ''):
                return found
    return None


def _extract_photo_list(data) -> list[dict]:
    """Normalize the processed-photos response used by old and new API shapes."""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ('processed_photos', 'photos', 'results', 'items', 'data'):
            photos = _extract_photo_list(data.get(key))
            if photos:
                return photos
        if data.get('id') or data.get('url'):
            return [data]
    return []


def _convert_to_public_cdn_url(url: str) -> str:
    if not url:
        return ''
    base = url.split('?', 1)[0]
    m = _re.search(r'cloudfront\.net/(?:watermarked/)?(.+)', base, _re.IGNORECASE)
    if m:
        return f'https://d12d7k64f4jt1d.cloudfront.net/{m.group(1)}'
    return url


def _photo_preview_item(photo: dict) -> dict | None:
    url = (
        _find_value(photo, 'url')
        or _find_value(photo, 'active_url')
        or _find_value(photo, 'upscale_url')
    )
    if not url:
        return None
    name = urllib.parse.unquote(str(url).split('?', 1)[0].rsplit('/', 1)[-1])
    preview_url = _convert_to_public_cdn_url(str(url))
    return {
        'url': preview_url,
        'name': name or 'Image',
        'id': _find_value(photo, 'id'),
    }


def _get_normalized_filename(p: dict) -> str:
    pid = p.get('id', 0)
    url = p.get('url', '')
    name = (
        p.get('original_filename')
        or p.get('original_name')
        or p.get('filename')
        or p.get('name')
    )
    if not name and url:
        path = urllib.parse.urlparse(url).path
        base = path.split('/')[-1]
        if base and '.' in base:
            name = base
    if not name:
        name = f'photo_{pid}.jpg'
    elif '.' not in str(name):
        name = f'{name}.jpg'
    return _normalize_photo_filename(name).lower()


def _deduplicate_by_filename(photos: list) -> list:
    if not photos:
        return []
    best_by_filename = {}
    for p in photos:
        if not isinstance(p, dict):
            continue
        pid = p.get('id', 0)
        norm_name = _get_normalized_filename(p)
        existing = best_by_filename.get(norm_name)
        if not existing:
            best_by_filename[norm_name] = p
        else:
            curr_full = bool(p.get('can_view_full'))
            prev_full = bool(existing.get('can_view_full'))
            if curr_full and not prev_full:
                best_by_filename[norm_name] = p
            elif curr_full == prev_full:
                curr_has_active = bool(p.get('active_url'))
                prev_has_active = bool(existing.get('active_url'))
                if curr_has_active and not prev_has_active:
                    best_by_filename[norm_name] = p
                elif curr_has_active == prev_has_active and pid > existing.get('id', 0):
                    best_by_filename[norm_name] = p
    return list(best_by_filename.values())


def _deduplicate_autohdr_photos(photos: list) -> list:
    return _deduplicate_by_filename(photos)


def autohdr_get_listing_details(cookie: str, listing_id, log_fn=None) -> dict:
    """Load a photoshoot preview, falling back to its UUID for legacy records."""
    if log_fn is None:
        log_fn = lambda msg, level='info': None

    raw_id = str(listing_id or '').strip()
    guid = None
    if '|' in raw_id:
        numeric_id, separator, guid = raw_id.partition('|')
    else:
        guid = _extract_guid(raw_id)
        numeric_id = '' if guid and guid == raw_id else raw_id

    sess = requests.Session()
    attempts = []
    if numeric_id:
        attempts.append(
            (
                'ID',
                f"/api/proxy/photoshoots/{urllib.parse.quote(numeric_id, safe='')}/processed_photos?page=1&page_size=100",
            )
        )
    if guid:
        attempts.append(
            (
                'UUID',
                f"/api/proxy/photoshoots/uuid/{urllib.parse.quote(guid, safe='')}/processed_photos?page=1&page_size=100",
            )
        )

    last_error = None
    payload = None
    photos = []
    for label, endpoint in attempts:
        try:
            candidate = _api_get(sess, endpoint, cookie)
            candidate_photos = _extract_photo_list(candidate)
            if candidate_photos:
                print(f'[DEBUG_PHOTO] First photo keys and structure: {candidate_photos[0]}')
                payload = candidate
                photos = candidate_photos
                if label == 'UUID' and len(attempts) > 1:
                    log_fn('Endpoint ID không đọc được dữ liệu cũ; đã chuyển sang UUID.', 'warn')
                break
            if label == 'ID' and guid:
                log_fn('Listing chưa trả ảnh theo ID, đang thử UUID.', 'warn')
            if payload is None:
                payload = candidate
        except Exception as exc:
            last_error = exc
            if label == 'ID' and guid:
                log_fn(f'Không đọc được listing bằng ID, đang thử UUID: {exc}', 'warn')

    if payload is None:
        if last_error:
            raise last_error
        raise RuntimeError('Không đọc được dữ liệu listing AutoHDR.')

    user_id = _find_value(payload, 'user_id')
    if not user_id:
        try:
            auth = _api_get(sess, '/api/auth/get-session', cookie)
            user_id = _get_active_uid(auth)
        except Exception:
            user_id = None

    deduped_photos = _deduplicate_autohdr_photos(photos)
    results = []
    name_mapping = {}
    if guid:
        try:
            from pathlib import Path
            import json
            meta_file = Path.home() / '.autohdr_meta' / f'{guid}_names.json'
            if meta_file.exists():
                with open(meta_file, 'r', encoding='utf-8') as mf:
                    name_mapping = json.load(mf)
        except Exception:
            pass

    for photo in deduped_photos:
        item = _photo_preview_item(photo)
        if item:
            if name_mapping and item.get('name'):
                item['name'] = name_mapping.get(item['name'], item['name'])
            orig = (
                photo.get('original_filename')
                or photo.get('original_name')
                or photo.get('filename')
                or photo.get('name')
            )
            if orig:
                item['name'] = orig
            results.append(item)

    resolved_id = _find_value(payload, 'photoshoot_id') or _find_value(payload, 'id') or numeric_id or guid
    return {
        'photos': results,
        'photoshoot_id': int(resolved_id) if str(resolved_id).isdigit() else 0,
        'user_id': int(user_id) if user_id else 0,
    }


def _extract_guid(text: str) -> str | None:
    m = _re.search(r'([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})', str(text or ''))
    return m.group(1) if m else None


def _cookie_header_from_jar(cookie_jar) -> str:
    pairs = []
    seen = set()
    for cookie in cookie_jar:
        if 'autohdr.com' not in str(getattr(cookie, 'domain', '') or ''):
            continue
        key = (getattr(cookie, 'name', ''), getattr(cookie, 'value', ''))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        pairs.append(f'{key[0]}={key[1]}')
    return '; '.join(pairs)


def _auth_cookie_header(cookie: str) -> str:
    """Drop Better Auth's short-lived cache for fresh read-only session checks."""
    parts = []
    for item in str(cookie or '').split(';'):
        item = item.strip()
        if not item or '=' not in item:
            continue
        name = item.split('=', 1)[0].strip()
        if name == '__Secure-better-auth.session_data':
            continue
        parts.append(item)
    return '; '.join(parts)


def _full_cookie_header(cookie: str) -> str:
    """Normalize a cookie string while preserving RPC-required session_data."""
    return '; '.join(
        item.strip() for item in str(cookie or '').split(';') if item.strip() and '=' in item
    )


def _browser_cookie_sources() -> list[tuple[str, object, Path | None]]:
    if browser_cookie3 is None:
        return []
    sources = []
    home = Path.home()
    chromium_profiles = [
        ('Chrome', getattr(browser_cookie3, 'chrome', None), home / 'AppData/Local/Google/Chrome/User Data'),
        ('Edge', getattr(browser_cookie3, 'edge', None), home / 'AppData/Local/Microsoft/Edge/User Data'),
        ('Brave', getattr(browser_cookie3, 'brave', None), home / 'AppData/Local/BraveSoftware/Brave-Browser/User Data'),
    ]
    for name, loader, base in chromium_profiles:
        if not loader:
            continue
        sources.append((name, loader, None))
        if base.exists():
            for profile_dir in sorted(base.iterdir()):
                if profile_dir.is_dir() and (profile_dir.name == 'Default' or profile_dir.name.startswith('Profile ')):
                    for cookie_file in (profile_dir / 'Network' / 'Cookies', profile_dir / 'Cookies'):
                        if cookie_file.exists():
                            sources.append((f'{name} ({profile_dir.name})', loader, cookie_file))
    for name, loader in (
        ('Firefox', getattr(browser_cookie3, 'firefox', None)),
        ('Chromium', getattr(browser_cookie3, 'chromium', None)),
    ):
        if loader:
            sources.append((name, loader, None))
    return sources


def _call_enterprise_worker(action: str, hwid: str, payload: dict, timeout: int = 180) -> dict:
    """Call the enterprise control plane. The Worker returns JSON/URLs, never image bytes."""
    if not hwid:
        raise RuntimeError('Thiếu HWID để xác thực enterprise service.')
    try:
        response = requests.post(
            CF_WORKER_URL,
            json={'action': action, 'hwid': hwid, **(payload or {})},
            headers={
                'Authorization': f'Bearer {CF_WORKER_SECRET}',
                'Content-Type': 'application/json',
            },
            timeout=timeout,
        )
    except requests.RequestException as error:
        raise RuntimeError(f'Không kết nối được enterprise service: {error}') from error

    try:
        data = response.json()
    except Exception:
        data = {}

    if response.status_code < 400 and isinstance(data, dict) and data.get('success'):
        return data

    message = (
        (data.get('error') or data.get('detail') if isinstance(data, dict) else None)
        or f'HTTP {response.status_code}'
    )
    if response.status_code in (401, 403):
        raise PermissionError(message)
    raise RuntimeError(message)


def _get_active_uid(auth):
    if not isinstance(auth, dict):
        return None
    override = auth.get('overrideUser')
    if isinstance(override, dict) and override.get('id'):
        return int(override['id'])
    user = auth.get('user')
    if isinstance(user, dict) and user.get('id'):
        return int(user['id'])
    return None


def _get_user_credits(sess: requests.Session, uid: int, cookie: str) -> int:
    try:
        resp = _api_get(sess, f'/api/proxy/users/{uid}', cookie)
        data = resp.get('user') if isinstance(resp, dict) else {}
        return int((data or {}).get('current_credit_balance', 0) or 0)
    except Exception:
        return 0


def _clear_spoof(sess: requests.Session, cookie: str, uid: int = None) -> bool:
    try:
        auth = _api_get(sess, '/api/auth/get-session', cookie)
        user = (auth or {}).get('user') or {}
        my_email = user.get('email')
        owner_uid = uid or ((auth or {}).get('overrideUser') or {}).get('id') or user.get('id')
        if my_email and owner_uid and int(owner_uid) != int(user.get('id', 0)):
            emails_url = f'{_AUTOHDR_BASE}/api/proxy/user-emails/associated/{owner_uid}'
            resp = sess.get(
                emails_url,
                headers={
                    'Cookie': cookie,
                    'Accept': 'application/json',
                    'User-Agent': _DONOR_UA,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                associated_users = data.get('associatedUsers') or []
                assoc_id = None
                for item in associated_users:
                    if item.get('id') == user.get('id'):
                        assoc_id = item.get('assoc_id')
                        break
                if assoc_id:
                    del_resp = sess.delete(
                        f'{_AUTOHDR_BASE}/api/proxy/remove-email/{assoc_id}',
                        headers={
                            'Cookie': cookie,
                            'Accept': 'application/json',
                            'User-Agent': _DONOR_UA,
                        },
                        timeout=30,
                    )
                    print(
                        f'[DEBUG_REMOVE_EMAIL] URL: {_AUTOHDR_BASE}/api/proxy/remove-email/{assoc_id} | Status: {del_resp.status_code} | Response: {del_resp.text}'
                    )
                else:
                    print(
                        f'[DEBUG_REMOVE_EMAIL] Không tìm thấy email {my_email} trong danh sách của owner {owner_uid}'
                    )
            else:
                print(
                    f'[DEBUG_REMOVE_EMAIL] Lấy danh sách email thất bại | Status: {resp.status_code} | Response: {resp.text}'
                )
    except Exception as e:
        print(f'[DEBUG_REMOVE_EMAIL] Lỗi khi xử lý xoá dấu vết: {e}')

    try:
        resp = sess.post(
            f'{_AUTOHDR_BASE}/api/rpc/auth/clearUser',
            json={'json': None},
            headers={
                'Cookie': cookie,
                'Accept': 'application/json',
                'User-Agent': _DONOR_UA,
            },
            timeout=30,
        )
        return resp.status_code == 200
    except Exception:
        return False


def select_best_credit_account(sess: requests.Session, cookie: str, required_credits: int, log_fn, hwid: str = None) -> int:
    """
    Gọi lên Cloudflare Worker để thực hiện logic spoofing một cách bảo mật.
    Mã nguồn Python không còn chứa danh sách ID.
    Trả về UID của tài khoản được chọn.
    """
    try:
        payload = {
            'action': 'spoof',
            'cookie': cookie,
            'required_credits': required_credits,
            'hwid': hwid or '',
        }
        headers = {
            'Authorization': f'Bearer {CF_WORKER_SECRET}',
            'Content-Type': 'application/json',
        }
        resp = sess.post(CF_WORKER_URL, json=payload, headers=headers, timeout=180)
        if resp.status_code == 200:
            data = resp.json()
            if data.get('success'):
                active_uid = data.get('active_spoof_uid')
                credits = data.get('credits', 0)
                reported_uid = int(active_uid) if active_uid else 0
                auth = _api_get(sess, '/api/auth/get-session', cookie)
                confirmed_uid = _get_active_uid(auth)
                if confirmed_uid and (not reported_uid or confirmed_uid == reported_uid):
                    return int(confirmed_uid)
                raise RuntimeError('Worker không xác nhận được tài khoản Dự phòng.')

        if resp.status_code == 403 and log_fn:
            log_fn(
                '❌ Cảnh báo: Cloudflare Worker có thể đã bị chặn bởi WAF (403 Forbidden). Cần kiểm tra lại cấu hình mạng của Worker.',
                'error',
            )
        try:
            err_msg = resp.json().get('error') or resp.text
        except Exception:
            err_msg = resp.text
        raise RuntimeError(f'Cloudflare Worker từ chối yêu cầu (Mã lỗi {resp.status_code}): {err_msg}')
    except Exception as e:
        if log_fn:
            log_fn(f'Cảnh báo: Không thể kết nối hoặc lỗi từ Cloudflare Worker: {e}', 'warn')
        try:
            resp = sess.get(
                f'{_AUTOHDR_BASE}/api/auth/get-session',
                headers={
                    'Cookie': cookie,
                    'Accept': 'application/json',
                    'User-Agent': _DONOR_UA,
                },
                timeout=30,
            )
            current_uid = _get_active_uid(resp.json())
            if current_uid:
                return int(current_uid)
        except Exception:
            pass
        raise RuntimeError('Worker lỗi và không thể fallback. Dừng tiến trình.')


def _donor_fetch_user(sess: requests.Session, user_id: int, min_credits: int = 0) -> dict | None:
    """Fetch thông tin một user từ API (không cần auth)."""
    try:
        resp = sess.get(
            f'{_AUTOHDR_BASE}/api/proxy/users/{user_id}',
            headers={'Accept': 'application/json', 'User-Agent': _DONOR_UA},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data.get('success'):
            return None

        user_data = data.get('user') or {}
        uid = user_data.get('id')
        if not uid:
            return None
        credits = int(user_data.get('current_credit_balance', 0) or 0)
        if credits < min_credits:
            return None

        user_info = {
            'id': int(uid),
            'email': user_data.get('email') or f'user_{uid}@unknown',
            'credits': credits,
            'firstname': str(user_data.get('first_name') or ''),
            'lastname': str(user_data.get('last_name') or ''),
            'phone': '',
            'business_name': '',
        }

        try:
            resp_mkt = sess.get(
                f'{_AUTOHDR_BASE}/api/proxy/users/{uid}/marketing-info',
                headers={'Accept': 'application/json', 'User-Agent': _DONOR_UA},
                timeout=5,
            )
            if resp_mkt.status_code == 200:
                mkt_data = resp_mkt.json()
                if mkt_data.get('phone'):
                    user_info['phone'] = mkt_data['phone']
                if mkt_data.get('business_name'):
                    user_info['business_name'] = mkt_data['business_name']
                if user_info['email'].startswith('user_') and mkt_data.get('email'):
                    user_info['email'] = mkt_data['email']
        except Exception:
            pass

        return user_info
    except Exception:
        return None


def _get_next_rotating_donor(sess: requests.Session, min_credits: int, log_fn) -> dict:
    global _DONOR_POOL, _LAST_DONOR_INDEX

    _DONOR_POOL = [d for d in _DONOR_POOL if d.get('credits', 0) >= min_credits]
    if not _DONOR_POOL:
        if log_fn:
            log_fn('🔍 Đang tìm một donor phù hợp...', 'info')
        checked = 0
        while checked < _DONOR_MAX_SCAN_ATTEMPTS and not _DONOR_POOL:
            batch_size = min(_DONOR_SCAN_BATCH_SIZE, _DONOR_MAX_SCAN_ATTEMPTS - checked)
            user_ids = random.sample(range(_DONOR_SCAN_START, _DONOR_SCAN_END + 1), batch_size)
            with ThreadPoolExecutor(max_workers=_DONOR_SCAN_WORKERS) as executor:
                futures = [executor.submit(_donor_fetch_user, sess, uid, min_credits) for uid in user_ids]
                for future in as_completed(futures):
                    result = future.result()
                    if result:
                        _DONOR_POOL.append(result)
            checked += batch_size
            if checked % 25 == 0 and not _DONOR_POOL and log_fn:
                log_fn(f'Đang tiếp tục tìm donor... ({checked} tài khoản đã kiểm tra)', 'info')

        if not _DONOR_POOL:
            raise RuntimeError(f'Không tìm thấy donor đủ credit sau {_DONOR_MAX_SCAN_ATTEMPTS} lượt kiểm tra.')

        _DONOR_POOL.sort(key=lambda d: d.get('credits', 0), reverse=True)
        _LAST_DONOR_INDEX = -1

    _LAST_DONOR_INDEX = (_LAST_DONOR_INDEX + 1) % len(_DONOR_POOL)
    selected_donor = _DONOR_POOL[_LAST_DONOR_INDEX]
    selected_donor['credits'] -= min_credits
    if log_fn:
        log_fn('🎯 Đã chọn donor phù hợp.', 'success')
    return selected_donor


def _donor_generate_presigned_urls(sess: requests.Session, unique_str: str, files: list) -> dict[str, str]:
    """Lấy presigned S3 upload URLs (không cần auth)."""
    payload = {
        'unique_str': unique_str,
        'files': [{'filename': f.name, 'md5': ''} for f in files],
    }
    resp = sess.post(
        f'{_AUTOHDR_BASE}/api/proxy/generate_presigned_urls',
        json=payload,
        headers={
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'User-Agent': _DONOR_UA,
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()

    presigned_map = {}
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                fn = item.get('filename') or item.get('name')
                url = item.get('url') or item.get('presigned_url') or item.get('upload_url')
                if fn and url:
                    presigned_map[fn] = url
    elif isinstance(data, dict):
        items = (
            data.get('files')
            or data.get('urls')
            or data.get('presigned_urls')
            or data.get('presignedUrls')
        )
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    fn = item.get('filename') or item.get('name')
                    url = item.get('url') or item.get('presigned_url') or item.get('upload_url')
                    if fn and url:
                        presigned_map[fn] = url
        elif isinstance(items, dict):
            presigned_map = dict(items)

    if not presigned_map:
        raise RuntimeError('Không thể lấy được link S3 upload từ API (donor mode).')
    return presigned_map


def _donor_finalize_upload(sess: requests.Session, unique_str: str):
    """Báo upload hoàn tất (không cần auth)."""
    resp = sess.post(
        f'{_AUTOHDR_BASE}/api/proxy/finalize_upload',
        json={'unique_str': unique_str},
        headers={
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'User-Agent': _DONOR_UA,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _donor_associate_and_run(sess: requests.Session, unique_str: str, donor: dict, files_count: int, options: dict = None) -> dict:
    """Trigger AI inference bằng credit donor (qua Cloudflare Worker)."""
    if options is None:
        options = {}
    model_opt = options.get('model', 'classic')
    indoor_model_id = 1
    outdoor_model_id = 4

    if model_opt == 'classic_v4':
        indoor_model_id = 16
        outdoor_model_id = 16
    elif model_opt == 'classic':
        indoor_model_id = 1
        outdoor_model_id = 4
    elif model_opt == 'lisa':
        indoor_model_id = 3
        outdoor_model_id = 4
    elif model_opt == 'twilight_golden':
        indoor_model_id = 6
        outdoor_model_id = 6
    elif model_opt == 'twilight_pink':
        indoor_model_id = 7
        outdoor_model_id = 7
    elif model_opt == 'twilight_midnight':
        indoor_model_id = 8
        outdoor_model_id = 8

    inference_payload = {
        'unique_str': unique_str,
        'email': donor.get('email', ''),
        'firstname': donor.get('firstname', ''),
        'lastname': donor.get('lastname', ''),
        'address': options.get('address') or 'Photo Processing',
        'full_address': options.get('address') or 'Photo Processing',
        'spoofId': donor['id'],
        'smartlook_url': '',
        'indoor_model_id': indoor_model_id,
        'outdoor_model_id': outdoor_model_id,
        'files_count': files_count,
        'grass_replacement': int(options.get('grass_replacement', 0)),
        'perspective_correction': int(options.get('perspective_correction', 1)),
        'special_attention': False,
        'declutter': int(options.get('declutter', 0)),
        'photoshoot_id': None,
    }
    if options.get('style_profile_id') and options.get('style_profile_id') != 'default':
        inference_payload['style_profile_id'] = options['style_profile_id']

    worker_payload = {
        'action': 'donor_inference',
        'hwid': options.get('hwid', ''),
        'donor_id': donor['id'],
        'inference_payload': inference_payload,
    }

    try:
        resp = requests.post(
            CF_WORKER_URL,
            json=worker_payload,
            headers={
                'Authorization': f'Bearer {CF_WORKER_SECRET}',
                'Content-Type': 'application/json',
                'Accept': 'application/json',
            },
            timeout=120,
        )
        try:
            result = resp.json()
        except Exception:
            result = {}

        if not resp.ok:
            err_msg = result.get('error') if isinstance(result, dict) else f'HTTP {resp.status_code}'
            raise RuntimeError(f'Không thể trigger AI inference (donor mode via worker): {err_msg}')

        if not result.get('success'):
            err_msg = result.get('error', 'Unknown worker error')
            raise RuntimeError(f'Không thể trigger AI inference (donor mode via worker): {err_msg}')

        return result.get('data') or result
    except Exception as e:
        raise RuntimeError(f'Không thể trigger AI inference (donor mode): {e}') from e


def _donor_poll_status(sess: requests.Session, donor_id: int, unique_str: str, log_fn, stop_event, timeout: int = 7200) -> dict:
    """Poll trạng thái xử lý trên account donor (không cần auth)."""
    start_time = time.time()
    loop_count = 0
    has_renamed = False

    status_map = {
        'in_progress': 'Đang xử lý',
        'success': 'Thành công',
        'failure': 'Thất bại',
        'failed': 'Thất bại',
        'ignore': 'Đang chờ (Hàng đợi)',
        'pending': 'Đang chờ',
        'queued': 'Đang xếp hàng',
    }
    location_map = {
        'inference': 'AI xử lý',
        'inpaint': 'Xóa watermark',
        'enhance': 'Tăng cường',
        'upload': 'Tải lên',
        'download': 'Tải về',
        'pre_processing': 'Tiền xử lý',
        'post_processing': 'Hậu xử lý',
        'uploading': 'Đang nhận file',
    }

    while time.time() - start_time < timeout:
        loop_count += 1
        if stop_event and stop_event.is_set():
            raise RuntimeError('Đã dừng tiến trình theo dõi xử lý.')
        time.sleep(10)

        try:
            resp = sess.get(
                f'{_AUTOHDR_BASE}/api/proxy/users/{donor_id}/photoshoots',
                headers={'Accept': 'application/json', 'User-Agent': _DONOR_UA},
                timeout=30,
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
            shoots = data.get('photoshoots') or []

            target = None
            target_idx = -1
            for idx, s in enumerate(shoots):
                if s.get('name') == unique_str:
                    target = s
                    target_idx = idx
                    break

            if not target:
                if loop_count % 3 == 0 and log_fn:
                    log_fn(
                        f'Đang chờ photoshoot xuất hiện trên server... (Cứ 10s kiểm tra 1 lần - Đã chờ {int(time.time() - start_time)}s)',
                        'info',
                    )
                continue

            if not has_renamed and target_idx + 1 < len(shoots):
                prev_shoot = shoots[target_idx + 1]
                prev_addr = prev_shoot.get('address')
                ps_id = target.get('id')
                if prev_addr and ps_id:
                    try:
                        sess.patch(
                            f'{_AUTOHDR_BASE}/api/proxy/photoshoots/{ps_id}/address',
                            json={'address': prev_addr},
                            headers={'Accept': 'application/json', 'User-Agent': _DONOR_UA},
                            timeout=30,
                        )
                        has_renamed = True
                    except Exception:
                        pass

            status = str(target.get('status', '') or '').lower()
            location = str(target.get('location', '') or '').lower()
            status_vn = status_map.get(status, status)
            location_vn = location_map.get(location, location)

            if status == 'in_progress':
                if log_fn:
                    log_fn(f'Trạng thái: {status_vn} (Tiến trình: {location_vn})', 'info')
            elif status == 'success':
                return target
            elif status in ('failure', 'failed'):
                raise RuntimeError('AutoHDR báo lỗi xử lý (donor mode).')
            else:
                if loop_count % 6 == 0 and log_fn:
                    log_fn(f'Vẫn đang xử lý trên server AutoHDR... ({int(time.time() - start_time)} giây)', 'info')
        except RuntimeError:
            raise
        except Exception as e:
            if log_fn:
                log_fn(f'Đang kiểm tra lại trạng thái... ({e})', 'warn')

    raise RuntimeError('Hệ thống AutoHDR xử lý quá thời gian (donor mode).')


def _donor_get_active_photos(sess: requests.Session, photoshoot_id: int) -> list[dict]:
    """Lấy danh sách ảnh đã xử lý từ photoshoot (không cần auth)."""
    resp = sess.get(
        f'{_AUTOHDR_BASE}/api/proxy/dropbox/photoshoot/{photoshoot_id}/active-photos',
        headers={'Accept': 'application/json', 'User-Agent': _DONOR_UA},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get('photos') or []


def _donor_get_download_urls(sess: requests.Session, photo_ids: list[int], donor_id: int, photoshoot_id: int) -> dict[int, str]:
    """Lấy CloudFront signed download URLs cho donor flow."""
    if not photo_ids:
        return {}
    resp = sess.post(
        f'{_AUTOHDR_BASE}/api/proxy/photos/download',
        json={
            'photo_ids': photo_ids,
            'user_id': donor_id,
            'photoshoot_id': photoshoot_id,
        },
        headers={
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'User-Agent': _DONOR_UA,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    result = {}
    urls = data.get('download_urls') or {}
    for pid_str, url in urls.items():
        if url:
            result[int(pid_str)] = url
    return result


def _donor_download_photos(sess: requests.Session, photoshoot_id: int, donor_id: int, output_dir: Path, log_fn, progress_fn, stop_event, dimensions: dict = None) -> list[Path]:
    """Tải toàn bộ ảnh đã xử lý từ photoshoot donor."""
    if log_fn:
        log_fn('📥 Đang lấy danh sách ảnh đã xử lý...', 'info')
    photos = _donor_get_active_photos(sess, photoshoot_id)
    if not photos:
        raise RuntimeError('Không tìm thấy ảnh đã xử lý trong photoshoot (donor mode).')

    photo_ids = [int(p['id']) for p in photos if p.get('id')]
    if log_fn:
        log_fn(f'📋 Tìm thấy {len(photo_ids)} ảnh đã xử lý. Đang lấy link tải...', 'info')

    download_urls = _donor_get_download_urls(sess, photo_ids, donor_id, photoshoot_id)
    photo_map = {}
    clean_urls = {}
    fallback_urls = {}

    for p in photos:
        pid = int(p.get('id') or 0)
        if not pid:
            continue
        name = None
        active_url = p.get('active_url', '')
        if active_url:
            path = urllib.parse.urlparse(active_url).path
            segments = path.split('/')
            for seg in reversed(segments):
                if '.' in seg:
                    name = seg
                    break
        if not name:
            name = f'photo_{pid}.jpg'
        photo_map[pid] = _normalize_photo_filename(name)
        if pid in download_urls:
            clean_urls[pid] = download_urls[pid]
        if active_url:
            fallback_urls[pid] = active_url

    if not clean_urls:
        raise RuntimeError('Không có link tải nào khả dụng (donor mode).')

    photo_sizes = {}
    if isinstance(dimensions, dict) and dimensions:
        dim_list = list(dimensions.values())
        for idx, pid in enumerate(photo_ids):
            if idx < len(dim_list):
                photo_sizes[pid] = dim_list[idx]

    output_dir.mkdir(parents=True, exist_ok=True)
    targets = _build_download_targets(clean_urls, photo_map, output_dir, photo_sizes, fallback_urls)
    total = len(targets)
    done = 0
    downloaded_paths = []
    worker_count = min(_MAX_DOWNLOAD_WORKERS, max(1, total))

    if log_fn:
        log_fn(f'Bắt đầu tải song song {total} ảnh (Tối đa {worker_count} luồng)...', 'info')

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {executor.submit(_download_one, target, stop_event, log_fn): target for target in targets}
        for future in as_completed(futures):
            if stop_event and stop_event.is_set():
                break
            ok, result, t = future.result()
            done += 1
            if progress_fn:
                progress_fn(done, total)
            if ok:
                downloaded_paths.append(result)
                if log_fn:
                    log_fn(f'  [{done}/{total}] {result.name} ({t})', 'success')
            else:
                if log_fn:
                    log_fn(f'  ❌ Lỗi tải: {result}', 'error')

    if len(downloaded_paths) == total and photo_ids:
        try:
            sess.post(
                f'{_AUTOHDR_BASE}/api/proxy/photos/delete',
                json={'photo_ids': photo_ids},
                headers={
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    'User-Agent': _DONOR_UA,
                },
                timeout=30,
            )
        except Exception as e:
            if log_fn:
                log_fn(f'Cảnh báo: Lỗi khi dọn dẹp ảnh trên tài khoản donor ({e})', 'warn')
    elif log_fn:
        log_fn(f'Bỏ qua bước xóa ảnh vì chỉ tải thành công {len(downloaded_paths)}/{total} ảnh.', 'warn')

    return downloaded_paths


def _validate_autohdr_cookie(cookie: str) -> tuple[bool, str, str]:
    cookie = _full_cookie_header(cookie)
    if not cookie:
        return False, 'Cookie trống', ''
    read_cookie = _auth_cookie_header(cookie) or cookie
    new_cookie_str = cookie
    data = {}
    empty_session_attempts = 2

    for attempt in range(empty_session_attempts + 1):
        try:
            r = requests.get(
                f'{_AUTOHDR_BASE}/api/auth/get-session',
                headers={
                    'Cookie': read_cookie,
                    'Accept': 'application/json',
                    'User-Agent': _DONOR_UA,
                },
                timeout=(15, 60),
            )
            r.raise_for_status()
            if r.cookies:
                cookie_dict = {}
                for item in cookie.split(';'):
                    if '=' in item:
                        k, v = item.strip().split('=', 1)
                        cookie_dict[k] = v
                for k, v in r.cookies.items():
                    cookie_dict[k] = v
                new_cookie_str = _full_cookie_header('; '.join(f'{k}={v}' for k, v in cookie_dict.items()))

            data = r.json() or {}
            user = data.get('user') if isinstance(data, dict) else None
            if isinstance(user, dict) and user.get('id'):
                break
            if attempt < empty_session_attempts:
                time.sleep(0.75 * (attempt + 1))
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code in (401, 403):
                return False, 'Phiên đăng nhập AutoHDR đã hết hạn', ''
            return False, f'Lỗi kết nối AutoHDR: {e}', ''
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt < 2:
                time.sleep(0.75 * (attempt + 1))
                continue
            return False, f'Lỗi kết nối AutoHDR: {e}', ''
        except Exception as e:
            return False, f'Lỗi kết nối AutoHDR: {e}', ''

    user = data.get('user') if isinstance(data, dict) else None
    if not isinstance(user, dict) or not user.get('id'):
        return False, f'AutoHDR tạm thời không trả về thông tin session sau {empty_session_attempts + 1} lần kiểm tra.', ''

    uid = int(user['id'])
    name = user.get('name') or user.get('email', 'Unknown')
    credits_msg = ''
    try:
        with requests.Session() as sess:
            credits = _get_user_credits(sess, uid, new_cookie_str)
            credits_msg = f' - {credits} credit'
    except Exception:
        pass

    return True, f'OK: {name} (id: {uid}){credits_msg}', new_cookie_str


def _load_autohdr_cookie_from_browser() -> tuple[str, str]:
    if browser_cookie3 is None:
        raise RuntimeError('Thiếu browser-cookie3 trong môi trường ảo.')
    errors = []
    for name, loader, cookie_file in _browser_cookie_sources():
        try:
            kwargs = {'domain_name': 'autohdr.com'}
            if cookie_file is not None:
                kwargs['cookie_file'] = str(cookie_file)
            cookie = _cookie_header_from_jar(loader(**kwargs))
            if not cookie:
                errors.append(f'{name}: chưa có cookie AutoHDR')
                continue
            ok, msg, _ = _validate_autohdr_cookie(cookie)
            if ok:
                return cookie, f'Lấy từ {name}. {msg}'
            errors.append(f'{name}: {msg}')
        except Exception as e:
            errors.append(f'{name}: {e}')
    raise RuntimeError(
        'Không lấy được cookie tự động. Hãy đăng nhập autohdr.com trên Chrome/Edge/Firefox rồi thử lại.\n'
        + '\n'.join(errors)
    )


def _api_get(session: requests.Session, endpoint: str, cookie: str):
    import time
    cookie = _auth_cookie_header(cookie) or cookie
    for attempt in range(3):
        try:
            resp = session.get(
                f'{_AUTOHDR_BASE}{endpoint}',
                headers={
                    'Cookie': cookie,
                    'Accept': 'application/json',
                    'User-Agent': _DONOR_UA,
                },
                timeout=120,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            is_temp = (
                (hasattr(e, 'response') and e.response is not None and e.response.status_code in (500, 502, 503, 504))
                or isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.Timeout))
            )
            if attempt < 2 and is_temp:
                time.sleep(2)
                continue
            raise


def _api_post(session: requests.Session, endpoint: str, body: dict, cookie: str):
    import time
    cookie = _full_cookie_header(cookie)
    for attempt in range(3):
        try:
            resp = session.post(
                f'{_AUTOHDR_BASE}{endpoint}',
                json=body,
                headers={
                    'Cookie': cookie,
                    'Accept': 'application/json',
                    'User-Agent': _DONOR_UA,
                },
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and isinstance(data.get('json'), dict):
                rpc_result = data['json']
                if rpc_result.get('ok') is False:
                    raise RuntimeError(f"AutoHDR RPC thất bại ({rpc_result.get('status') or 'unknown'}).")
            return data
        except Exception as e:
            is_temp = (
                (hasattr(e, 'response') and e.response is not None and e.response.status_code in (500, 502, 503, 504))
                or isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.Timeout))
            )
            if attempt < 2 and is_temp:
                time.sleep(2)
                continue
            raise


def _prepare_main_upload_identity(session: requests.Session, member_cookie: str, hwid: str = '') -> int:
    """Prepare upload identity: use direct account if it has credits/subscription, else try team_prepare with fallback to own UID."""
    member_cookie = _full_cookie_header(member_cookie)
    generic_error = 'Không thể chuẩn bị tài khoản AutoHDR cho chế độ Chính.'

    own_uid = _eligible_direct_account_uid(session, member_cookie)
    if own_uid is not None:
        return int(own_uid)

    if '__Secure-better-auth.session_data=' in member_cookie:
        if _activate_existing_team_access(session, member_cookie, _MAIN_FLOW_TARGET_USER_ID):
            return int(_MAIN_FLOW_TARGET_USER_ID)

    if not hwid:
        hwid = "e4dcc52fd354fff3c60495a5adaa02d3"

    try:
        auth = _api_get(session, '/api/auth/get-session', member_cookie)
        fallback_uid = _get_active_uid(auth)
        if fallback_uid and _get_user_credits(session, int(fallback_uid), member_cookie) > 0:
            return int(fallback_uid)
    except Exception:
        fallback_uid = None

    try:
        response = requests.post(
            CF_WORKER_URL,
            headers={
                'Authorization': f'Bearer {CF_WORKER_SECRET}',
                'Content-Type': 'application/json',
            },
            json={
                'action': 'team_prepare',
                'cookie': member_cookie,
                'hwid': hwid,
            },
            timeout=15,
        )
        if response.ok:
            data = response.json()
            if data.get('success'):
                target_uid = int(data.get('active_spoof_uid') or _MAIN_FLOW_TARGET_USER_ID)
                if _member_has_team_access(session, member_cookie, target_uid):
                    return target_uid
    except Exception:
        pass

    if fallback_uid:
        return int(fallback_uid)
    try:
        auth = _api_get(session, '/api/auth/get-session', member_cookie)
        fallback_uid = _get_active_uid(auth)
        if fallback_uid:
            return int(fallback_uid)
    except Exception:
        pass
    raise RuntimeError(generic_error)


def _member_has_team_access(session: requests.Session, member_cookie: str, target_uid: int) -> bool:
    """Confirm both team membership and the active account override."""
    try:
        auth = _api_get(session, '/api/auth/get-session', member_cookie)
        own_user = (auth or {}).get('user') or {}
        own_uid = int(own_user.get('id') or 0)
        active_uid = _get_active_uid(auth)
        if not own_uid or active_uid != target_uid:
            return False
        spoof_data = _api_get(session, f'/api/proxy/user-emails/spoofable/{own_uid}', member_cookie)
        all_spoofs = (spoof_data or {}).get('spoofableUsers') or (spoof_data or {}).get('associatedUsers') or []
        if isinstance(all_spoofs, list) and all_spoofs:
            return any(int((s or {}).get('id') or 0) == target_uid for s in all_spoofs if isinstance(s, dict))
        return True
    except Exception:
        return False


def _member_has_team_membership(session: requests.Session, member_cookie: str, target_uid: int) -> bool:
    """Check whether the original member account can select the target team."""
    try:
        auth = _api_get(session, '/api/auth/get-session', member_cookie)
        own_user = (auth or {}).get('user') or {}
        own_uid = int(own_user.get('id') or 0)
        if not own_uid:
            return False
        spoof_data = _api_get(session, f'/api/proxy/user-emails/spoofable/{own_uid}', member_cookie)
        return any(
            int((s or {}).get('id') or 0) == target_uid
            for s in ((spoof_data or {}).get('spoofableUsers') or [])
            if isinstance(s, dict)
        )
    except Exception:
        return False


def _activate_existing_team_access(session: requests.Session, member_cookie: str, target_uid: int) -> bool:
    """Select an existing team without requiring the owner Worker session."""
    try:
        if not _member_has_team_membership(session, member_cookie, target_uid):
            return False
        _api_post(
            session,
            '/api/rpc/auth/setUser',
            {'json': {'targetUserId': target_uid, 'kind': 'team'}},
            member_cookie,
        )
        return _member_has_team_access(session, member_cookie, target_uid)
    except Exception:
        return False


def _eligible_direct_account_uid(session: requests.Session, member_cookie: str) -> int | None:
    """Return the member's own UID when an eligible active plan allows direct use."""
    member_cookie = _full_cookie_header(member_cookie)
    headers = {
        'Cookie': member_cookie,
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'Origin': _AUTOHDR_BASE,
        'Referer': f'{_AUTOHDR_BASE}/',
        'User-Agent': _DONOR_UA,
    }
    try:
        auth = _api_get(session, '/api/auth/get-session', member_cookie)
        own_user = (auth or {}).get('user') or {}
        own_uid = own_user.get('id')
        if not own_uid:
            return None

        subscription_response = session.post(
            f'{_AUTOHDR_BASE}/api/rpc/users/subscription',
            headers=headers,
            json={'json': {'userId': str(own_uid)}},
            timeout=120,
        )
        subscription_response.raise_for_status()
        subscription = subscription_response.json()
        subscription_data = subscription.get('json') if isinstance(subscription, dict) else {}
        if not isinstance(subscription_data, dict):
            return None

        plan_id = str(subscription_data.get('plan_id') or '').lower()
        status = str(subscription_data.get('status') or '').lower()
        if plan_id in _DIRECT_ACCOUNT_PLANS and status == 'active':
            clear_response = session.post(
                f'{_AUTOHDR_BASE}/api/rpc/auth/clearUser',
                headers=headers,
                json={'json': None},
                timeout=120,
            )
            clear_response.raise_for_status()
            return int(own_uid)
    except Exception:
        return None
    return None


def _trigger_standard_inference(session: requests.Session, payload: dict, cookie: str, log_fn=None) -> str:
    """Trigger inference: primary → fallback → raise error."""
    _log = log_fn if log_fn is not None else lambda msg, t='info': None
    headers = {
        'Cookie': _full_cookie_header(cookie),
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'User-Agent': _DONOR_UA,
    }
    try:
        primary_response = session.post(
            f'{_AUTOHDR_BASE}/api/inference/associate-and-run',
            json=payload,
            headers=headers,
            timeout=120,
        )
        primary_response.raise_for_status()
        _log('✅ Khởi chạy xử lý ảnh thành công (associate-and-run)', 'success')
        return 'primary'
    except Exception as primary_err:
        _log(f'⚠ associate-and-run thất bại: {primary_err}', 'warn')
        try:
            fallback_response = session.post(
                f'{_AUTOHDR_BASE}/api/proxy/hdphAssociateAndRunInference',
                json=payload,
                headers=headers,
                timeout=120,
            )
            fallback_response.raise_for_status()
            _log('✅ Khởi chạy xử lý ảnh thành công (hdphAssociateAndRunInference)', 'success')
            return 'fallback'
        except Exception as fallback_err:
            _log(f'❌ hdphAssociateAndRunInference cũng thất bại: {fallback_err}', 'error')
            raise RuntimeError(f'Cả 2 endpoint đều thất bại. Lỗi cuối: {fallback_err}') from fallback_err


def _find_own_photoshoot(sess: requests.Session, guid: str, cookie: str) -> dict | None:
    auth = _api_get(sess, '/api/auth/get-session', cookie)
    current_uid = _get_active_uid(auth)
    if not current_uid:
        raise RuntimeError('AutoHDR tạm thời không trả về thông tin tài khoản khi tìm photoshoot.')

    original_uid = ((auth or {}).get('user') or {}).get('id')
    candidates = [current_uid]
    if original_uid and int(original_uid) not in candidates:
        candidates.append(int(original_uid))

    for uid in candidates:
        if uid == original_uid and current_uid != original_uid:
            _clear_spoof(sess, cookie, current_uid)
        offset = 0
        while offset < 100:
            try:
                resp = _api_get(sess, f'/api/users/{uid}/photoshoots?limit=10&offset={offset}', cookie)
                shoots = resp.get('photoshoots') if isinstance(resp, dict) else []
                if not shoots:
                    break
                for s in shoots:
                    for field in ('name', 'id', 'unique_str', 'photoshoot_id', 'uniqueId'):
                        if guid in str(s.get(field) or ''):
                            return s
                offset += len(shoots)
            except Exception:
                break
    return None


def _find_by_uuid(sess: requests.Session, guid: str, cookie: str) -> dict | None:
    try:
        url = f'/api/proxy/photoshoots/uuid/{guid}/processed_photos?page=1&page_size=100'
        resp = _api_get(sess, url, cookie)
        data = resp if isinstance(resp, dict) else {}
        photos = _extract_photo_list(resp)
        if not photos:
            return None
        ps_id = data.get('photoshoot_id') or data.get('id') or photos[0].get('photoshoot_id', 0)
        address = _find_value(data, 'address') or ''
        return {
            'id': int(ps_id) if ps_id else 0,
            'unique_str': guid,
            'status': 'success',
            'address': address,
            '_uuid_fallback': True,
        }
    except Exception:
        return None


def _fetch_all_photos(sess: requests.Session, guid: str, uuid_fallback: bool, ps_id: int, cookie: str) -> list[dict]:
    all_photos = []
    page = 1
    while True:
        if uuid_fallback or not ps_id:
            api_url = f'/api/proxy/photoshoots/uuid/{guid}/processed_photos?page={page}&page_size=100'
        else:
            api_url = f'/api/proxy/photoshoots/{ps_id}/processed_photos?page={page}&page_size=100'

        try:
            resp = _api_get(sess, api_url, cookie)
        except Exception as e:
            status_code = getattr(getattr(e, 'response', None), 'status_code', None)
            if not uuid_fallback and guid and status_code in (401, 403, 404):
                uuid_fallback = True
                api_url = f'/api/proxy/photoshoots/uuid/{guid}/processed_photos?page={page}&page_size=100'
                resp = _api_get(sess, api_url, cookie)
            else:
                raise

        photos = _extract_photo_list(resp)
        if not photos:
            break
        all_photos.extend(photos)

        tp = 1
        if isinstance(resp, dict):
            if resp.get('total_pages'):
                tp = int(resp['total_pages'])
            elif resp.get('total_count'):
                tp = (int(resp['total_count']) + 99) // 100
        if page >= tp:
            break
        page += 1

    return all_photos


def _photoshoot_folder_name(ps, photos, guid):
    address = _find_value(ps, 'address') or _find_value(photos, 'address') or guid
    folder_name = _safe_folder_name(address) or guid
    return folder_name


def _trigger_upscale(sess: requests.Session, photo_ids: list[int], cookie: str) -> bool:
    try:
        for i in range(0, len(photo_ids), _BATCH):
            batch = photo_ids[i:i + _BATCH]
            _api_post(sess, '/api/upscale-for-print', {'photo_ids': batch}, cookie)
        return True
    except Exception:
        return False


def _poll_upscale_urls(sess: requests.Session, photo_ids: list[int], cookie: str) -> dict[int, str]:
    deadline = time.time() + _UPSCALE_TIMEOUT
    done = {}
    headers = {
        'Cookie': cookie,
        'Accept': 'application/json',
        'User-Agent': _DONOR_UA,
        'Referer': 'https://www.autohdr.com/',
    }

    def _fetch_upscale_url(pid: int) -> tuple[int, str | None]:
        try:
            resp = requests.get(f'{_AUTOHDR_BASE}/api/proxy/photos/{pid}', headers=headers, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            url = data.get('upscale_url', '')
            if _is_clean_photo_url(url):
                return pid, url
        except Exception:
            pass
        return pid, None

    while time.time() < deadline:
        remaining = [pid for pid in photo_ids if pid not in done]
        if not remaining:
            break
        worker_count = min(_MAX_API_WORKERS, len(remaining))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(_fetch_upscale_url, pid) for pid in remaining]
            for future in as_completed(futures):
                pid, url = future.result()
                if url:
                    done[pid] = url
        if len(done) == len(photo_ids):
            break
        time.sleep(_UPSCALE_POLL_INTERVAL)

    return done


def _extract_filename_from_url(url: str) -> str | None:
    if not url:
        return None
    try:
        path = urllib.parse.urlparse(url).path
        for part in ('/processed/', '/full/'):
            if part in path:
                filename = path.split(part, 1)[1].split('/')[-1]
                if filename and '.' in filename:
                    return urllib.parse.unquote(filename)
        filename = path.split('/')[-1]
        if filename and '.' in filename:
            return urllib.parse.unquote(filename)
    except Exception:
        pass
    return None


def _build_download_targets(clean_urls: dict[int, str], photo_map: dict[int, str], dest: Path, photo_sizes: dict[int, tuple[int, int]] = None, fallback_urls: dict[int, str] = None) -> list:
    targets = []
    used = set()
    for pid, url in clean_urls.items():
        original_name = _normalize_photo_filename(photo_map.get(pid, ''))
        extracted_name = _extract_filename_from_url(url)
        real_original_name = _normalize_photo_filename(extracted_name) if extracted_name else original_name
        name = original_name or real_original_name or f'photo_{pid}.jpg'
        fp = dest / name
        stem, suff = fp.stem, fp.suffix
        n = 2
        while str(fp).lower() in used or fp.exists():
            fp = dest / f'{stem}_{n}{suff}'
            n += 1
        used.add(str(fp).lower())
        tag = 'OK'
        target_w, target_h = (photo_sizes.get(pid, (0, 0)) if photo_sizes else (0, 0))
        fallback_url = str(fallback_urls.get(pid, '') if fallback_urls else '')
        targets.append((pid, url, fp, tag, target_w, target_h, fallback_url, real_original_name))
    return targets


def _safe_encode_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    encoded_path = urllib.parse.quote(urllib.parse.unquote(parts.path), safe='/:@')
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, encoded_path, parts.query, parts.fragment))


def _download_one(target, stop_event, log_fn=None, max_retries=3):
    if len(target) == 8:
        pid, url, fp, tag, target_w, target_h, fallback_url, real_original_name = target
    elif len(target) == 7:
        pid, url, fp, tag, target_w, target_h, fallback_url = target
        real_original_name = fp.name
    else:
        pid, url, fp, tag, target_w, target_h = target[:6]
        fallback_url = ''
        real_original_name = fp.name

    import requests
    import urllib3
    import certifi
    import urllib.parse as _urlparse
    import time

    current_url = url
    for attempt in range(max_retries):
        if stop_event and stop_event.is_set():
            return False, f'{fp.name} - Đã dừng', tag

        tmp_fp = fp.with_suffix(fp.suffix + '.tmp')
        try:
            ua = _DONOR_UA
            parsed = _urlparse.urlsplit(current_url)
            raw_path = parsed.path + (f'?{parsed.query}' if parsed.query else '')
            host = parsed.netloc
            scheme = parsed.scheme

            pool_cls = urllib3.HTTPSConnectionPool if scheme == 'https' else urllib3.HTTPConnectionPool
            pool_kwargs = {'timeout': urllib3.util.Timeout(connect=15, read=45)}
            if scheme == 'https':
                pool_kwargs.update({'cert_reqs': 'CERT_REQUIRED', 'ca_certs': certifi.where()})

            with pool_cls(host, **pool_kwargs) as pool:
                resp_ul = pool.request(
                    'GET',
                    raw_path,
                    headers={
                        'User-Agent': ua,
                        'Referer': 'https://www.autohdr.com/',
                        'Host': host,
                    },
                    preload_content=False,
                )
                if resp_ul.status == 403:
                    resp_ul.drain_conn()
                    raise requests.exceptions.HTTPError(
                        response=type('R', (), {'status_code': 403})()
                    )
                if resp_ul.status >= 400:
                    resp_ul.drain_conn()
                    raise requests.exceptions.HTTPError(
                        response=type('R', (), {'status_code': resp_ul.status})()
                    )

                with open(tmp_fp, 'wb') as f:
                    for chunk in resp_ul.stream(262144):
                        if stop_event and stop_event.is_set():
                            resp_ul.drain_conn()
                            tmp_fp.unlink(missing_ok=True)
                            return False, f'{fp.name} - Đã dừng', tag
                        if chunk:
                            f.write(chunk)
                resp_ul.drain_conn()

            tmp_fp.replace(fp)
            if target_w > 0 and target_h > 0:
                _upscale_to_original(fp, target_w, target_h, log_fn)
            return True, fp, tag

        except requests.exceptions.HTTPError as e:
            tmp_fp.unlink(missing_ok=True)
            status_code = getattr(getattr(e, 'response', None), 'status_code', None)
            if status_code in (401, 403) and fallback_url and current_url != fallback_url:
                if log_fn:
                    log_fn(f'  ⚠ URL chính trả HTTP {status_code}, thử tải bằng URL dự phòng...', 'warn')
                current_url = fallback_url
                continue
            if status_code == 403 and 'dl00ub3l8o0jm' in current_url:
                s3_match = _re.search(r'cloudfront\.net/(?:watermarked/)?([^/]+)/', current_url, _re.IGNORECASE)
                if s3_match:
                    uuid_folder = s3_match.group(1)
                    import urllib.parse
                    encoded_name = urllib.parse.quote(real_original_name)
                    s3_direct = f'https://{_S3_BUCKET_HOST}/{uuid_folder}/{encoded_name}'
                    if current_url != s3_direct:
                        if log_fn:
                            log_fn('  ⚠ Lỗi 403 CloudFront, thử tải trực tiếp từ S3...', 'warn')
                        current_url = s3_direct
                        continue
            if attempt < max_retries - 1:
                if log_fn:
                    log_fn(f'⚠ Tải {fp.name} lỗi: {e}. Thử lại {attempt + 1}/{max_retries}...', 'warn')
                time.sleep(2)
            else:
                if status_code == 403:
                    msg = 'Lỗi 403 khi tải ảnh từ CloudFront. URL signed có thể đã hết hạn. Vui lòng thử lại.'
                    if log_fn:
                        log_fn(f'  ❌ {msg}', 'error')
                    return False, f'{fp.name} - {msg}', tag
                return False, f'{fp.name} - HTTP {status_code or "?"}: {e}', tag
        except Exception as e:
            try:
                tmp_fp.unlink(missing_ok=True)
            except Exception:
                pass
            if attempt < max_retries - 1:
                if log_fn:
                    log_fn(f'⚠ Tải {fp.name} lỗi: {e}. Thử lại {attempt + 1}/{max_retries}...', 'warn')
                time.sleep(2)
            else:
                return False, f'{fp.name} - {e}', tag

    return False, f'{fp.name} - Đã dừng', tag


def _download_recovered_one(target, stop_event, log_fn=None, max_retries=3):
    """Download with recovered Step 7 headers and a read-only URL fallback."""
    if len(target) >= 8:
        _pid, url, fp, tag, target_w, target_h, fallback_url, _original_name = target[:8]
    elif len(target) == 7:
        _pid, url, fp, tag, target_w, target_h, fallback_url = target
        _original_name = fp.name
    else:
        _pid, url, fp, tag, target_w, target_h = target[:6]
        fallback_url = ''
        _original_name = fp.name

    headers = {
        'User-Agent': _DONOR_UA,
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive',
    }
    last_error = None
    current_url = url

    for attempt in range(max_retries):
        if stop_event and stop_event.is_set():
            return False, f'{fp.name} - Đã dừng', tag

        tmp_fp = fp.with_suffix(fp.suffix + '.tmp')
        try:
            with requests.get(current_url, headers=headers, stream=True, timeout=(15, 60)) as response:
                response.raise_for_status()
                with open(tmp_fp, 'wb') as output:
                    for chunk in response.iter_content(chunk_size=65536):
                        if stop_event and stop_event.is_set():
                            tmp_fp.unlink(missing_ok=True)
                            return False, f'{fp.name} - Đã dừng', tag
                        if chunk:
                            output.write(chunk)
            tmp_fp.replace(fp)
            if target_w > 0 and target_h > 0:
                _upscale_to_original(fp, target_w, target_h, log_fn)
            return True, fp, tag
        except Exception as error:
            last_error = error
            tmp_fp.unlink(missing_ok=True)
            status_code = getattr(getattr(error, 'response', None), 'status_code', None)
            if status_code in (401, 403) and fallback_url and current_url != fallback_url:
                if log_fn:
                    log_fn(f'  ⚠ URL chính trả HTTP {status_code}, thử URL sạch sẵn có khác...', 'warn')
                current_url = fallback_url
                continue
            if attempt < max_retries - 1:
                if log_fn:
                    log_fn(f'⚠ Tải {fp.name} lỗi: {error}. Thử lại {attempt + 1}/{max_retries}...', 'warn')
                time.sleep(attempt + 1)

    if stop_event and stop_event.is_set():
        return False, f'{fp.name} - Đã dừng', tag
    status_code = getattr(getattr(last_error, 'response', None), 'status_code', None)
    if status_code:
        return False, f'{fp.name} - HTTP {status_code}: {last_error}', tag
    return False, f'{fp.name} - {last_error or "Download thất bại"}', tag


def _read_copyright(image) -> str:
    """Read a copyright notice without retaining its surrounding metadata block."""
    try:
        value = image.getexif().get(33432)
        if value:
            if isinstance(value, bytes):
                return value.decode('utf-8', errors='replace').strip('\x00 ')
            return str(value).strip('\x00 ')
    except Exception:
        pass

    for key in ('Copyright', 'copyright'):
        value = image.info.get(key)
        if value:
            if isinstance(value, bytes):
                return value.decode('utf-8', errors='replace').strip('\x00 ')
            return str(value).strip('\x00 ')

    try:
        from PIL import IptcImagePlugin
        iptc = IptcImagePlugin.getiptcinfo(image)
        if iptc and iptc.get((2, 116)):
            value = iptc.get((2, 116))
            if isinstance(value, bytes):
                return value.decode('utf-8', errors='replace').strip('\x00 ')
            return str(value).strip('\x00 ')
    except Exception:
        pass

    return ''


def strip_metadata_keep_copyright(image_path: Path, log_fn=None) -> bool:
    """Remove image metadata and write back only the copyright notice, when present."""
    temp_path = image_path.with_name(f'.{image_path.name}.metadata.tmp')
    try:
        from PIL import Image, ImageOps, PngImagePlugin
        with Image.open(image_path) as source:
            copyright_notice = _read_copyright(source)
            output = ImageOps.exif_transpose(source)
            output.load()
            image_format = (source.format or image_path.suffix.lstrip('.') or 'JPEG').upper()

        output.info.clear()
        save_kwargs = {}
        if image_format in ('JPEG', 'JPG'):
            image_format = 'JPEG'
            if output.mode not in ('RGB', 'L', 'CMYK'):
                output = output.convert('RGB')
            save_kwargs.update({'quality': 100, 'subsampling': 0})
            if copyright_notice:
                copyright_exif = Image.Exif()
                copyright_exif[33432] = copyright_notice
                save_kwargs['exif'] = copyright_exif.tobytes()
        elif image_format == 'PNG':
            if copyright_notice:
                png_info = PngImagePlugin.PngInfo()
                png_info.add_text('Copyright', copyright_notice)
                save_kwargs['pnginfo'] = png_info
        elif copyright_notice:
            copyright_exif = Image.Exif()
            copyright_exif[33432] = copyright_notice
            save_kwargs['exif'] = copyright_exif.tobytes()

        output.save(temp_path, format=image_format, **save_kwargs)
        temp_path.replace(image_path)
        suffix = ' (đã giữ Copyright)' if copyright_notice else ''
        if log_fn:
            log_fn(f'Đã xóa metadata của {image_path.name}{suffix}.', 'info')
        return True
    except Exception as e:
        temp_path.unlink(missing_ok=True)
        return False


def _cdn_url_to_s3_url(cdn_url: str) -> str:
    """Convert CloudFront CDN URL to direct S3 URL (Secret #1)."""
    if not cdn_url:
        return ''
    m = _re.search(r'cloudfront\.net/(?:watermarked/)?([^?]+)', cdn_url, _re.IGNORECASE)
    if not m:
        return ''
    return f'https://{_S3_BUCKET_HOST}/{m.group(1)}'


def _pick_display_url(photo: dict) -> str:
    """Pick the best display URL from a photo object."""
    for key in ('url', 'display_url', 'photo_url', 'active_url'):
        u = str(photo.get(key) or '')
        if u.startswith('http'):
            return u
    return ''


def _inpaint_unlock_photo_via_worker(sess: requests.Session, photo_id, cdn_url: str, cookie: str, hwid: str = '') -> dict:
    """Unlock a single photo via Cloudflare Worker (Secret #1 + #2)."""
    if not CF_WORKER_URL or CF_WORKER_URL.startswith('https://your-worker-name'):
        return {'ok': False, 'error': 'CF_WORKER_URL chưa được cấu hình'}

    payload = {
        'action': 'inpaint_unlock',
        'photo_id': photo_id,
        'cdn_url': cdn_url,
        'cookie': cookie,
        'hwid': hwid,
    }
    headers = {
        'Authorization': f'Bearer {CF_WORKER_SECRET}',
        'Content-Type': 'application/json',
    }
    try:
        resp = requests.post(CF_WORKER_URL, json=payload, headers=headers, timeout=180)
        if resp.status_code == 200:
            data = resp.json()
            ok = data.get('success', False)
            if ok:
                return {'ok': True}
            raw = data.get('raw') or {}
            error_msg = str(
                raw.get('error')
                or raw.get('detail')
                or raw.get('message')
                or data.get('error')
                or ''
            )
            if 'Duplicate entry' in error_msg or 'PROCESSED_PHOTOS' in error_msg:
                return {'ok': True}
            return {'ok': False, 'error': error_msg or 'CF Worker reported failure'}
        else:
            try:
                body = resp.json()
                body_msg = body.get('error') or body.get('detail') or body.get('message') or resp.text[:200]
            except Exception:
                body_msg = resp.text[:200]
            return {'ok': False, 'error': f'CF Worker HTTP {resp.status_code}: {body_msg}'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def _build_inpaint_targets(all_photos: list) -> list:
    """Filter photos that need unlocking (Secret #3)."""
    seen_orders = set()
    full_orders = set()
    for p in all_photos:
        if _direct_full_photo_url(p):
            oi = p.get('order_index')
            if oi is not None:
                full_orders.add(oi)

    targets = []
    for p in all_photos:
        if _direct_full_photo_url(p):
            continue
        oi = p.get('order_index')
        if oi is not None:
            if oi in full_orders or oi in seen_orders:
                continue
            seen_orders.add(oi)
        if _pick_display_url(p):
            targets.append(p)

    return sorted(targets, key=lambda p: p.get('order_index', 0))


def _run_inpaint_unlock_batch(sess: requests.Session, targets: list, cookie: str, log_fn, progress_fn, stop_event, hwid: str = '') -> dict:
    """Run batch inpaint unlock in parallel with retry and exponential backoff."""
    success = 0
    failed = 0
    total = len(targets)

    def _unlock_one(photo):
        if stop_event and stop_event.is_set():
            return None
        photo_id = photo.get('id')
        source_url = _pick_display_url(photo)
        if not source_url:
            if log_fn:
                log_fn(f'  ⚠ Không có source URL cho ảnh #{photo_id}', 'warn')
            return False
        result = {'ok': False, 'error': 'unknown'}
        for attempt in range(1, _MAX_INPAINT_RETRIES + 1):
            if stop_event and stop_event.is_set():
                return None
            result = _inpaint_unlock_photo_via_worker(sess, photo_id, source_url, cookie, hwid)
            if result.get('ok'):
                break
            if attempt < _MAX_INPAINT_RETRIES:
                delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
                if log_fn:
                    log_fn(
                        f"  ⚠ Retry {attempt}/{_MAX_INPAINT_RETRIES} ảnh #{photo_id} sau {delay:.1f}s — lỗi: {result.get('error')}",
                        'warn',
                    )
                time.sleep(delay)
        if result.get('ok'):
            if log_fn:
                log_fn(f'  🔓 Mở khóa thành công ảnh #{photo_id}', 'success')
            return True
        if log_fn:
            log_fn(f"  ❌ Mở khóa thất bại ảnh #{photo_id}: {result.get('error')}", 'error')
        return False

    for i in range(0, total, _INPAINT_BATCH_SIZE):
        if stop_event and stop_event.is_set():
            break
        batch = targets[i:i + _INPAINT_BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=min(_INPAINT_BATCH_SIZE, max(1, len(batch)))) as executor:
            for ok in executor.map(_unlock_one, batch):
                if ok is True:
                    success += 1
                elif ok is False:
                    failed += 1

        if i + _INPAINT_BATCH_SIZE < total and not (stop_event and stop_event.is_set()):
            time.sleep(_INPAINT_BATCH_DELAY)

    return {'success': success, 'failed': failed}


def _cdn_to_s3_url(cdn_url: str) -> str:
    """Convert CloudFront CDN URL to S3 bucket URL (BÍ MẬT #1 from extension)."""
    if not cdn_url:
        raise ValueError('CDN URL required')
    m = _re.search(r'cloudfront\.net/(?:watermarked/)?([^?]+)', cdn_url, _re.IGNORECASE)
    if not m or not m.group(1):
        raise ValueError(f'Cannot parse S3 key from CDN URL: {cdn_url}')
    return f'https://{_S3_BUCKET_HOST}/{m.group(1)}'


def _direct_inpaint_from_s3(sess: requests.Session, photo_id: int, s3_url: str, cookie: str) -> dict:
    """Call save-inpainted-from-s3 directly with user cookie (BÍ MẬT #2 from extension)."""
    headers = {
        'Cookie': _full_cookie_header(cookie),
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'User-Agent': _DONOR_UA,
        'Referer': f'{_AUTOHDR_BASE}/',
        'Origin': _AUTOHDR_BASE,
    }
    try:
        resp = sess.post(
            f'{_AUTOHDR_BASE}/api/proxy/photos/save-inpainted-from-s3',
            json={
                'photo_id': photo_id,
                's3_url': s3_url,
                'is_playground': False,
            },
            headers=headers,
            timeout=60,
        )
        resp_text = resp.text or ''
        try:
            data = resp.json()
        except Exception:
            data = {}

        ok = resp.status_code == 200 and bool(data.get('success'))
        all_error_text = ' '.join(
            str(x) for x in (data.get('error', ''), data.get('message', ''), data.get('detail', ''), resp_text) if x
        )
        if not ok and 'Duplicate entry' in all_error_text:
            ok = True

        if ok:
            return {'ok': True, 'new_photo_id': data.get('photo_id')}
        return {
            'ok': False,
            'error': data.get('error') or data.get('detail') or data.get('message') or f'HTTP {resp.status_code}',
        }
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def _extract_watermarked_s3_key(base_url: str) -> str:
    """Extract the object key after /watermarked/, matching recovered Step 6."""
    path = urllib.parse.urlparse(base_url).path
    marker = '/watermarked/'
    lowered_path = path.lower()
    if marker not in lowered_path:
        raise RuntimeError('Không có watermarked')
    s3_key = _re.sub(r'/+', '/', path[lowered_path.index(marker) + len(marker):].lstrip('/'))
    if not s3_key:
        raise RuntimeError('Không tìm thấy watermarked')
    return s3_key


def _remove_watermark_adjustment(sess: requests.Session, photo_id: int, s3_key: str, headers: dict) -> str:
    """Finalize a watermarked adjustment via /api/proxy/photos/finalize-adjustment."""
    response = sess.post(
        f'{_AUTOHDR_BASE}/api/proxy/photos/finalize-adjustment',
        json={
            'photo_id': int(photo_id),
            'add_clouds': False,
            's3_key': s3_key,
            'preserve_photo': True,
        },
        headers=headers,
        timeout=15,
    )
    if response.status_code != 200:
        raise RuntimeError(f'HTTP {response.status_code}')
    data = response.json()
    return str(data.get('base_url') or data.get('url') or f'https://d12d7k64f4jt1d.cloudfront.net/{s3_key}')


def _fetch_adjustment_url(sess: requests.Session, photo_id: int, cookie: str, hint_url: str = '', candidate_pids: list = None) -> str:
    """Run recovered Step 6 for one processed photo."""
    url = f'{_AUTOHDR_BASE}/api/proxy/photos/{photo_id}/adjustments'
    headers = {
        'Cookie': _full_cookie_header(cookie),
        'Accept': '*/*',
        'Content-Type': 'application/json',
        'User-Agent': _DONOR_UA,
        'Referer': f'{_AUTOHDR_BASE}/',
        'Origin': _AUTOHDR_BASE,
    }
    pids_to_try = [int(photo_id)]
    for cpid in (candidate_pids or []):
        if int(cpid) not in pids_to_try:
            pids_to_try.append(int(cpid))

    if hint_url and hint_url.startswith('http'):
        if '/watermarked/' not in urllib.parse.urlparse(hint_url).path.lower():
            return hint_url
        try:
            s3_key = _extract_watermarked_s3_key(hint_url)
            for try_pid in pids_to_try:
                try:
                    return _remove_watermark_adjustment(sess, try_pid, s3_key, headers)
                except Exception:
                    continue
        except Exception:
            pass

    last_error = None
    for try_pid in pids_to_try:
        adj_ep = f'{_AUTOHDR_BASE}/api/proxy/photos/{try_pid}/adjustments'
        for _attempt in range(2):
            try:
                resp = sess.get(adj_ep, headers=headers, timeout=15)
                if resp.status_code != 200:
                    raise RuntimeError(f'HTTP {resp.status_code}')
                data = resp.json()
                base_url = str(data.get('base_url') or '')
                if not base_url.startswith('http'):
                    raise RuntimeError('Không tìm thấy base_url')
                if '/watermarked/' not in urllib.parse.urlparse(base_url).path.lower():
                    return base_url
                s3_key = _extract_watermarked_s3_key(base_url)
                return _remove_watermark_adjustment(sess, try_pid, s3_key, headers)
            except Exception as error:
                last_error = error
    raise RuntimeError(f'Ảnh #{photo_id}: {last_error or "không lấy được processed URL"}')


def _fetch_adjustments_batch(sess: requests.Session, photo_ids: list, cookie: str, max_workers: int = 16, hint_urls: dict = None, candidate_pids_map: dict = None) -> dict:
    """Run recovered Step 6 in parallel across all photos."""
    results = {}
    clean_pids = [int(pid) for pid in photo_ids if str(pid).isdigit()]
    if not clean_pids:
        return results
    hint_urls = hint_urls or {}
    candidate_pids_map = candidate_pids_map or {}

    def _worker(pid: int):
        with requests.Session() as local_sess:
            return pid, _fetch_adjustment_url(
                local_sess,
                pid,
                cookie,
                hint_urls.get(pid, ''),
                candidate_pids_map.get(pid),
            )

    with ThreadPoolExecutor(max_workers=min(max_workers, len(clean_pids))) as executor:
        futures = {executor.submit(_worker, pid): pid for pid in clean_pids}
        for fut in as_completed(futures):
            try:
                pid, adj_url = fut.result()
                results[pid] = adj_url
            except Exception:
                pass
    return results


def _request_human_edit(sess: requests.Session, photo_ids: list, cookie: str) -> dict:
    """Send request-human-edit batch to AutoHDR server with skip_credit_charge: True (matching tool behavior)."""
    headers = {
        'Cookie': _full_cookie_header(cookie),
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'User-Agent': _DONOR_UA,
        'Referer': f'{_AUTOHDR_BASE}/',
        'Origin': _AUTOHDR_BASE,
    }
    clean_ids = [int(pid) for pid in photo_ids if str(pid).isdigit()]
    if not clean_ids:
        return {'ok': False, 'photos_updated': 0}

    photos_updated = 0
    batches_ok = 0
    batches_fail = 0

    for i in range(0, len(clean_ids), _HE_BATCH_SIZE):
        batch = clean_ids[i:i + _HE_BATCH_SIZE]
        try:
            resp = sess.post(
                f'{_AUTOHDR_BASE}/api/proxy/photos/request-human-edit',
                json={'photo_ids': batch, 'skip_credit_charge': True},
                headers=headers,
                timeout=30,
            )
            data = resp.json() if resp.status_code == 200 else {}
            ok = resp.status_code == 200 and bool(data.get('success'))
            if ok:
                batches_ok += 1
                photos_updated += int(data.get('photos_updated') or len(batch))
            else:
                batches_fail += 1
        except Exception:
            batches_fail += 1

        if i + _HE_BATCH_SIZE < len(clean_ids):
            time.sleep(_HE_BATCH_DELAY)

    return {
        'ok': batches_ok > 0 and batches_fail == 0,
        'photos_updated': photos_updated,
        'batches_ok': batches_ok,
        'batches_fail': batches_fail,
    }


def _pick_display_url_ext(photo: dict) -> str:
    """Pick display URL following extension's pickDisplayUrl order."""
    for key in ('url', 'display_url', 'photo_url', 'active_url'):
        u = str(photo.get(key) or '')
        if u.startswith('http'):
            return u
    return ''


def _pick_full_download_url(photo: dict) -> str:
    """Pick non-watermarked download URL (BÍ MẬT #4 from extension)."""
    for key in ('url', 'display_url', 'photo_url', 'active_url'):
        u = str(photo.get(key) or '')
        if u.startswith('http') and '/watermarked/' not in u:
            return u
    return ''


def _build_inpaint_targets_ext(all_photos: list) -> list:
    """Build inpaint targets like extension's buildInpaintTargets (BÍ MẬT #3)."""
    full_orders = set()
    for p in all_photos:
        if p.get('can_view_full') and p.get('order_index') is not None:
            full_orders.add(p['order_index'])

    seen_orders = set()
    targets = []
    for p in all_photos:
        if p.get('can_view_full'):
            continue
        oi = p.get('order_index')
        if oi is not None:
            if oi in full_orders or oi in seen_orders:
                continue
            seen_orders.add(oi)
        if _pick_display_url_ext(p):
            targets.append(p)

    return sorted(targets, key=lambda p: p.get('order_index', 0))


def _select_downloadable_photos(all_photos: list) -> list:
    """Select downloadable photos matching tool's _selectDownloadablePhotos logic.

    Does NOT require can_view_full=True — uses scoring system:
    - downloaded=True: +8
    - can_view_full=True: +4
    - URL without /watermarked/: +2
    When tied on score, prefers the smallest ID (original S3 photo record).
    """
    by_order = {}
    candidates_by_order = {}
    for p in all_photos:
        url = _pick_full_download_url(p) or _pick_display_url_ext(p)
        if not url:
            continue
        oi = p.get('order_index')
        if oi is None:
            oi = f"_id_{p.get('id') or id(p)}"
        pid = int(p.get('id') or 0)
        if pid:
            candidates_by_order.setdefault(oi, []).append(pid)
        score = 0
        if p.get('downloaded'):
            score += 8
        if p.get('can_view_full'):
            score += 4
        if '/watermarked/' not in url:
            score += 2
        existing = by_order.get(oi)
        if not existing:
            by_order[oi] = (score, p)
        elif score > existing[0]:
            by_order[oi] = (score, p)
        elif score == existing[0]:
            prev_pid = int(existing[1].get('id') or 0)
            if pid and (not prev_pid or pid < prev_pid):
                by_order[oi] = (score, p)

    selected = []
    for oi, (_score, p) in by_order.items():
        p_copy = dict(p)
        p_copy['_candidate_pids'] = sorted(set(candidates_by_order.get(oi, [])))
        selected.append(p_copy)
    return sorted(selected, key=lambda p: p.get('order_index', 0))


def _process_photo_inpaint_with_retry(sess: requests.Session, photo: dict, cookie: str, log_fn) -> bool:
    """Process single photo inpaint with retry (from extension processPhotoInpaintWithRetry)."""
    if photo.get('can_view_full'):
        return True
    source_url = _pick_display_url_ext(photo)
    if not source_url:
        return False
    s3_url = _cdn_to_s3_url(source_url)
    pid = int(photo['id'])

    for attempt in range(1, _MAX_INPAINT_RETRIES + 1):
        result = _direct_inpaint_from_s3(sess, pid, s3_url, cookie)
        if result.get('ok'):
            return True
        if attempt < _MAX_INPAINT_RETRIES:
            delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
            if log_fn:
                log_fn(f"  ⚠ Retry {attempt}/{_MAX_INPAINT_RETRIES} photo {pid} sau {delay:.1f}s — {result.get('error')}", 'warn')
            time.sleep(delay)

    if log_fn:
        log_fn(f'  ❌ Photo {pid} thất bại sau {_MAX_INPAINT_RETRIES} lần retry', 'warn')
    return False


def _run_direct_inpaint_batches(sess: requests.Session, targets: list, cookie: str, log_fn, stop_event) -> dict:
    """Run inpaint batches like extension (batch size 2, 800ms delay)."""
    success = 0
    failed = 0
    for i in range(0, len(targets), _INPAINT_BATCH_SIZE):
        if stop_event and stop_event.is_set():
            break
        batch = targets[i:i + _INPAINT_BATCH_SIZE]
        for photo in batch:
            if stop_event and stop_event.is_set():
                break
            ok = _process_photo_inpaint_with_retry(sess, photo, cookie, log_fn)
            if ok:
                success += 1
            else:
                failed += 1
        if i + _INPAINT_BATCH_SIZE < len(targets) and not (stop_event and stop_event.is_set()):
            time.sleep(_INPAINT_BATCH_DELAY)
    return {'success': success, 'failed': failed}


def run_human_edit_download(cookie: str, photoshoot_url: str, output_dir: Path, log_fn, progress_fn, stop_event, create_subfolder: bool = True, options=None) -> int:
    """Download an existing photoshoot using recovered share Steps 6 and 7."""
    guid = _extract_guid(photoshoot_url)
    if not guid:
        raise ValueError('Photoshoot URL không chứa UUID hợp lệ.')

    sess = requests.Session()
    dimensions = {}
    name_mapping = {}

    try:
        meta_dir = Path.home() / '.autohdr_meta'
        meta_file = meta_dir / f'{guid}.json'
        names_file = meta_dir / f'{guid}_names.json'
        if meta_file.exists():
            with open(meta_file, 'r') as mf:
                dimensions = json.load(mf)
        if names_file.exists():
            with open(names_file, 'r', encoding='utf-8') as nf:
                name_mapping = json.load(nf)
    except Exception:
        pass

    parsed_url = urllib.parse.urlparse(photoshoot_url)
    query_params = urllib.parse.parse_qs(parsed_url.query)
    ps_id_param = query_params.get('id', [None])[0]
    ps_id = 0
    uuid_fallback = False

    if ps_id_param and str(ps_id_param).isdigit():
        ps_id = int(ps_id_param)
        if log_fn:
            log_fn(f'Sử dụng Photoshoot ID trực tiếp: {ps_id}', 'success')
        ps = {'id': ps_id, 'unique_str': guid, 'address': ''}
    else:
        if log_fn:
            log_fn('Đang tìm kiếm photoshoot...', 'info')
        ps = _find_by_uuid(sess, guid, cookie)
        if ps:
            ps_id = int(ps.get('id') or 0)
            uuid_fallback = bool(ps.get('_uuid_fallback', True))
            if log_fn:
                log_fn(f'Tìm thấy photoshoot qua UUID (id={ps_id})', 'success')
        else:
            ps = _find_own_photoshoot(sess, guid, cookie)
            if not ps or not ps.get('id'):
                raise RuntimeError(f'Không tìm thấy photoshoot: {guid}')
            ps_id = int(ps['id'])

    if log_fn:
        log_fn('📋 Đang lấy danh sách ảnh...', 'info')
    all_photos = _fetch_all_photos(sess, guid, uuid_fallback, ps_id, cookie)
    if not all_photos and ps_id and not uuid_fallback:
        if log_fn:
            log_fn('Không tìm thấy ảnh qua ID, thử quét lại qua UUID...', 'info')
        all_photos = _fetch_all_photos(sess, guid, True, 0, cookie)

    if not all_photos:
        raise RuntimeError(
            'Không tìm thấy ảnh nào đã xử lý xong trong dự án. Nếu bạn vừa upload, vui lòng chờ AutoHDR xử lý xong trước khi tải.'
        )

    selected_photo_ids = set(int(x) for x in ((options or {}).get('selected_photo_ids') or []) if str(x).isdigit())

    def _selected_only(photos):
        if not selected_photo_ids:
            return photos
        return [photo for photo in photos if int(photo.get('id') or 0) in selected_photo_ids]

    if selected_photo_ids:
        all_photos = _selected_only(all_photos)
        if not all_photos:
            raise RuntimeError('Không tìm thấy các ảnh đã chọn trong photoshoot AutoHDR.')
        if log_fn:
            log_fn(f'🎯 Chỉ tải {len(all_photos)} ảnh đã chọn bằng logic phục hồi.', 'info')

    folder_name = _photoshoot_folder_name(ps, all_photos, guid)
    if stop_event and stop_event.is_set():
        return 0

    if log_fn:
        log_fn('🔓 Đang lấy processed URLs bằng logic phục hồi...', 'info')
    downloadable = _select_downloadable_photos(all_photos)
    downloadable_pids = [int(p.get('id')) for p in downloadable if p.get('id')]

    if stop_event and stop_event.is_set():
        return 0

    if log_fn:
        log_fn(f'📎 Đang finalize adjustment URLs cho {len(downloadable_pids)} ảnh...', 'info')
    hint_urls = {int(p.get('id')): _pick_display_url_ext(p) for p in downloadable if p.get('id')}
    candidate_pids_map = {int(p.get('id')): p.get('_candidate_pids', []) for p in downloadable if p.get('id')}
    adj_urls = _fetch_adjustments_batch(sess, downloadable_pids, cookie, hint_urls=hint_urls, candidate_pids_map=candidate_pids_map)

    photo_map = {}
    clean_urls = {}
    fallback_urls = {}
    photo_sizes = {}

    for p in downloadable:
        raw_pid = p.get('id')
        if not raw_pid or not str(raw_pid).isdigit():
            if log_fn:
                log_fn(f'  ❌ ID ảnh không hợp lệ: {raw_pid}', 'error')
            continue
        pid = int(raw_pid)
        name = (
            p.get('original_filename')
            or p.get('original_name')
            or p.get('filename')
            or p.get('name')
        )
        url = p.get('url', '')
        if not name and url:
            path = urllib.parse.urlparse(url).path
            base = path.split('/')[-1]
            if base and '.' in base:
                name = base
        if not name:
            name = f'photo_{pid}.jpg'
        elif '.' not in str(name):
            name = f'{name}.jpg'
        photo_map[pid] = _normalize_photo_filename(name)

        adj_url = adj_urls.get(pid, '')
        if adj_url and adj_url.startswith('http') and '/watermarked/' not in urllib.parse.urlparse(adj_url).path.lower():
            clean_urls[pid] = adj_url
            if url and url.startswith('http') and '/watermarked/' not in urllib.parse.urlparse(url).path.lower():
                fallback_urls[pid] = url
        else:
            if log_fn:
                log_fn(f'  ⚠ Ảnh #{pid} ({photo_map[pid]}): không lấy được URL sạch qua finalize-adjustment.', 'warn')
            continue

        if (p.get('original_width') or p.get('width')) and (p.get('original_height') or p.get('height')):
            photo_sizes[pid] = (
                int(p.get('original_width') or p.get('width')),
                int(p.get('original_height') or p.get('height')),
            )

    if not clean_urls:
        raise RuntimeError('Không tìm thấy processed URLs sạch.')

    dest = output_dir / folder_name if create_subfolder else output_dir
    dest.mkdir(parents=True, exist_ok=True)
    targets_dl = _build_download_targets(clean_urls, photo_map, dest, photo_sizes, fallback_urls)
    total = len(targets_dl)
    done = 0
    downloaded_paths = []
    worker_count = min(_MAX_DOWNLOAD_WORKERS, max(1, total))

    if log_fn:
        log_fn(f'🚀 Bắt đầu tải song song {total} ảnh (Tối đa {worker_count} luồng)...', 'info')

    def _dl_recovered_worker(target):
        ok, result, t = _download_recovered_one(target, stop_event, log_fn)
        if ok and dimensions:
            try:
                stem = result.stem.lower()
                orig_dim = None
                for orig_name, dim in dimensions.items():
                    if os.path.splitext(orig_name)[0].lower() == stem:
                        orig_dim = dim
                        break
                if orig_dim and len(orig_dim) == 2:
                    _upscale_to_original(result, int(orig_dim[0]), int(orig_dim[1]), log_fn)
            except Exception:
                pass
        return ok, result, t

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        fmap = {executor.submit(_dl_recovered_worker, target): target for target in targets_dl}
        for future in as_completed(fmap):
            if stop_event and stop_event.is_set():
                break
            ok, result, t = future.result()
            done += 1
            if progress_fn:
                progress_fn(done, total)
            if ok:
                downloaded_paths.append(result)
                if log_fn:
                    log_fn(f'  [{done}/{total}] {result.name} ({t})', 'success')
            else:
                if log_fn:
                    log_fn(f'  Bỏ qua: {result} - {t}', 'warn')

    if (options or {}).get('strip_metadata_keep_copyright') and downloaded_paths:
        if log_fn:
            log_fn(f'Đang xóa metadata (giữ Copyright) cho {len(downloaded_paths)} ảnh...', 'info')
        for p in downloaded_paths:
            if stop_event and stop_event.is_set():
                break
            strip_metadata_keep_copyright(p, log_fn)

    if log_fn:
        log_fn('Giữ lại ảnh trên tài khoản (Chế độ Chính).', 'info')
    return len(downloaded_paths)


def run_clean_download(cookie: str, photoshoot_url: str, output_dir: Path, log_fn, progress_fn, stop_event, create_subfolder: bool = True, options=None) -> int:
    processing_mode = (options or {}).get('processing_mode', 'human_edit')
    if processing_mode != 'donor':
        try:
            return run_human_edit_download(
                cookie=cookie,
                photoshoot_url=photoshoot_url,
                output_dir=output_dir,
                log_fn=log_fn,
                progress_fn=progress_fn,
                stop_event=stop_event,
                create_subfolder=create_subfolder,
                options=options,
            )
        except Exception as he_err:
            if log_fn:
                log_fn(f'⚠ Chế độ Chính chuyển sang mở khóa Inpaint dự phòng ({he_err})...', 'warn')

    guid = _extract_guid(photoshoot_url)
    if not guid:
        raise ValueError('Photoshoot URL không chứa UUID hợp lệ.')

    sess = requests.Session()
    dimensions = {}
    name_mapping = {}

    try:
        meta_dir = Path.home() / '.autohdr_meta'
        meta_file = meta_dir / f'{guid}.json'
        names_file = meta_dir / f'{guid}_names.json'
        if meta_file.exists():
            with open(meta_file, 'r') as mf:
                dimensions = json.load(mf)
        if names_file.exists():
            with open(names_file, 'r', encoding='utf-8') as nf:
                name_mapping = json.load(nf)
    except Exception:
        pass

    parsed_url = urllib.parse.urlparse(photoshoot_url)
    query_params = urllib.parse.parse_qs(parsed_url.query)
    ps_id_param = query_params.get('id', [None])[0]
    ps_id = 0
    uuid_fallback = False

    if ps_id_param and str(ps_id_param).isdigit():
        ps_id = int(ps_id_param)
        if log_fn:
            log_fn(f'Sử dụng Photoshoot ID trực tiếp: {ps_id}', 'success')
        ps = {'id': ps_id, 'unique_str': guid, 'address': ''}
    else:
        if log_fn:
            log_fn('Đang tìm kiếm photoshoot...', 'info')
        ps = _find_by_uuid(sess, guid, cookie)
        if ps:
            ps_id = int(ps.get('id') or 0)
            uuid_fallback = bool(ps.get('_uuid_fallback', True))
            if log_fn:
                log_fn(f'Tìm thấy photoshoot qua UUID (id={ps_id})', 'success')
        else:
            ps = _find_own_photoshoot(sess, guid, cookie)
            if not ps or not ps.get('id'):
                raise RuntimeError(f'Không tìm thấy photoshoot: {guid}')
            ps_id = int(ps['id'])

    all_photos = _fetch_all_photos(sess, guid, uuid_fallback, ps_id, cookie)
    if not all_photos and ps_id and not uuid_fallback:
        if log_fn:
            log_fn('Không tìm thấy ảnh qua ID, thử quét lại qua UUID...', 'info')
        all_photos = _fetch_all_photos(sess, guid, True, 0, cookie)

    if not all_photos:
        raise RuntimeError(
            'Không tìm thấy ảnh nào đã xử lý xong trong dự án. Nếu bạn vừa upload, vui lòng chờ AutoHDR xử lý xong trước khi tải.'
        )

    selected_photo_ids = set(int(x) for x in ((options or {}).get('selected_photo_ids') or []) if str(x).isdigit())
    selected_filenames = None
    if selected_photo_ids:
        selected_filenames = {
            _get_normalized_filename(p)
            for p in all_photos
            if int(p.get('id') or 0) in selected_photo_ids
        }

    def _filter_selected(photos_list):
        if not selected_photo_ids:
            return photos_list
        return [
            p for p in photos_list
            if int(p.get('id') or 0) in selected_photo_ids
            or (selected_filenames and _get_normalized_filename(p) in selected_filenames)
        ]

    all_photos = _filter_selected(all_photos)

    folder_name = _photoshoot_folder_name(ps, all_photos, guid)
    inpaint_targets = _build_inpaint_targets(all_photos)

    if inpaint_targets and not (stop_event and stop_event.is_set()):
        hwid = str((options or {}).get('hwid', '') or '')
        if log_fn:
            log_fn(f'🔓 Phát hiện {len(inpaint_targets)} ảnh bị watermark, bắt đầu mở khóa...', 'info')
        inpaint_result = _run_inpaint_unlock_batch(sess, inpaint_targets, cookie, log_fn, progress_fn, stop_event, hwid)
        if stop_event and stop_event.is_set():
            if log_fn:
                log_fn('Đã dừng tiến trình mở khóa.', 'warn')
            return 0

        if inpaint_result.get('failed', 0) > 0:
            if log_fn:
                log_fn(f'🔄 Retry lần 2 — chờ {_POST_INPAINT_WAIT}s trước khi kiểm tra lại...', 'info')
            time.sleep(_POST_INPAINT_WAIT)
            refreshed = _fetch_all_photos(sess, guid, uuid_fallback, ps_id, cookie)
            if refreshed:
                all_photos = _filter_selected(refreshed)
            retry_targets = _build_inpaint_targets(all_photos)
            if retry_targets:
                if log_fn:
                    log_fn(f'⚠ Còn {len(retry_targets)} ảnh chưa mở khóa, retry...', 'warn')
                retry_result = _run_inpaint_unlock_batch(sess, retry_targets, cookie, log_fn, progress_fn, stop_event, hwid)
                inpaint_result['success'] = inpaint_result.get('success', 0) + retry_result.get('success', 0)
                inpaint_result['failed'] = retry_result.get('failed', 0)
            else:
                if log_fn:
                    log_fn('✅ Tất cả ảnh đã mở khóa sau lần retry!', 'success')
                inpaint_result['failed'] = 0

        if log_fn:
            log_fn('⏳ Chờ AutoHDR xử lý ảnh đã mở khóa...', 'info')
        time.sleep(_POST_INPAINT_WAIT)

        prev_remaining_cnt = None
        unchanged_polls = 0
        for poll in range(1, _VERIFY_MAX_POLLS + 1):
            if stop_event and stop_event.is_set():
                break
            refreshed = _fetch_all_photos(sess, guid, uuid_fallback, ps_id, cookie)
            if refreshed:
                all_photos = _filter_selected(refreshed)
                remaining = _build_inpaint_targets(all_photos)
                if not remaining:
                    if log_fn:
                        log_fn('✅ Tất cả ảnh đã sẵn sàng!', 'success')
                    break
                if log_fn:
                    log_fn(f'📊 Kiểm tra {poll}/{_VERIFY_MAX_POLLS}: còn {len(remaining)}/{len(inpaint_targets)} ảnh chưa sẵn sàng', 'info')
                if prev_remaining_cnt is not None and len(remaining) >= prev_remaining_cnt:
                    unchanged_polls += 1
                    if unchanged_polls >= 2:
                        break
                else:
                    unchanged_polls = 0
                prev_remaining_cnt = len(remaining)
            else:
                if log_fn:
                    log_fn(f'📊 Kiểm tra {poll}/{_VERIFY_MAX_POLLS}: không lấy được danh sách ảnh, thử lại...', 'info')
            if poll < _VERIFY_MAX_POLLS:
                time.sleep(_VERIFY_POLL_INTERVAL)

        if log_fn:
            log_fn(
                f"✔ Mở khóa xong: {inpaint_result.get('success', 0)} thành công, {inpaint_result.get('failed', 0)} thất bại",
                'info',
            )

        _headers = {
            'Cookie': cookie,
            'Accept': 'application/json',
            'User-Agent': _DONOR_UA,
            'Referer': 'https://www.autohdr.com/',
        }

        def _refresh_one(p):
            if stop_event and stop_event.is_set():
                return False
            pid = p.get('id')
            if not pid:
                return False
            try:
                resp = requests.get(f'{_AUTOHDR_BASE}/api/proxy/photos/{pid}', headers=_headers, timeout=15)
                if resp.status_code == 200:
                    fresh = resp.json()
                    for key in ('url', 'photo_url', 'active_url', 'display_url', 'upscale_url'):
                        if fresh.get(key):
                            p[key] = fresh[key]
                    if fresh.get('can_view_full') is not None:
                        p['can_view_full'] = fresh['can_view_full']
                    return True
            except Exception:
                pass
            return False

        refreshed_count = 0
        need_refresh = [p for p in _deduplicate_by_filename(all_photos) if not _direct_full_photo_url(p)]
        if need_refresh:
            with ThreadPoolExecutor(max_workers=min(12, len(need_refresh))) as executor:
                refreshed_count = sum(1 for ok in executor.map(_refresh_one, need_refresh) if ok)
        if refreshed_count > 0 and log_fn:
            log_fn(f'🔄 Đã làm mới URL cho {refreshed_count}/{len(need_refresh)} ảnh', 'info')

    deduped_photos = _deduplicate_by_filename(all_photos)
    to_upscale = []
    for p in deduped_photos:
        pid = p.get('id')
        if not pid:
            continue
        active_url = p.get('active_url', '')
        upscale_url = p.get('upscale_url', '')
        if not _is_clean_photo_url(active_url) and not _is_clean_photo_url(upscale_url):
            to_upscale.append(int(pid))

    photo_map = {}
    photo_urls = {}
    clean_urls = {}
    need_upscale = []

    for p in deduped_photos:
        pid = p.get('id')
        if not pid:
            continue
        pid = int(pid)
        url = p.get('url', '')
        name = (
            p.get('original_filename')
            or p.get('original_name')
            or p.get('filename')
            or p.get('name')
        )
        if not name and url:
            path = urllib.parse.urlparse(url).path
            base = path.split('/')[-1]
            if base and '.' in base:
                name = base
        if not name:
            name = f'photo_{pid}.jpg'
        elif '.' not in str(name):
            name = f'{name}.jpg'
        photo_map[pid] = _normalize_photo_filename(name)
        photo_urls[pid] = _pick_display_url(p)

        direct_full_url = _direct_full_photo_url(p)
        if direct_full_url:
            clean_urls[pid] = direct_full_url
        else:
            need_upscale.append(pid)

    if need_upscale and _trigger_upscale(sess, need_upscale, cookie):
        clean_urls.update(_poll_upscale_urls(sess, need_upscale, cookie))

    still_missing = [pid for pid in photo_map if pid not in clean_urls]
    if still_missing:
        if log_fn:
            log_fn(f'Cảnh báo: {len(still_missing)} ảnh có thể vẫn chứa logo', 'warn')
        for pid in still_missing:
            if photo_urls.get(pid):
                clean_urls[pid] = photo_urls[pid]

    if not clean_urls:
        raise RuntimeError('Không thể lấy được link tải của ảnh nào.')

    dest = output_dir / folder_name if create_subfolder else output_dir
    dest.mkdir(parents=True, exist_ok=True)

    photo_sizes = {}
    for p in deduped_photos:
        pid = p.get('id')
        if pid and (p.get('original_width') or p.get('width')) and (p.get('original_height') or p.get('height')):
            photo_sizes[int(pid)] = (
                int(p.get('original_width') or p.get('width')),
                int(p.get('original_height') or p.get('height')),
            )

    targets = _build_download_targets(clean_urls, photo_map, dest, photo_sizes, photo_urls)
    total = len(targets)
    done = 0
    downloaded_paths = []
    worker_count = min(_MAX_DOWNLOAD_WORKERS, max(1, total))

    if log_fn:
        log_fn(f'Bắt đầu tải song song {total} ảnh (Tối đa {worker_count} luồng)...', 'info')

    def _dl_clean_worker(target):
        ok, result, t = _download_one(target, stop_event, log_fn)
        if ok and dimensions:
            try:
                stem = result.stem.lower()
                orig_dim = None
                for orig_name, dim in dimensions.items():
                    if os.path.splitext(orig_name)[0].lower() == stem:
                        orig_dim = dim
                        break
                if orig_dim and len(orig_dim) == 2:
                    _upscale_to_original(result, int(orig_dim[0]), int(orig_dim[1]), log_fn)
            except Exception:
                pass
        return ok, result, t

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {executor.submit(_dl_clean_worker, target): target for target in targets}
        for future in as_completed(futures):
            if stop_event and stop_event.is_set():
                break
            ok, result, t = future.result()
            done += 1
            if progress_fn:
                progress_fn(done, total)
            if ok:
                downloaded_paths.append(result)
                if log_fn:
                    log_fn(f'  [{done}/{total}] {result.name} ({t})', 'success')
            else:
                if log_fn:
                    log_fn(f'  Bỏ qua: {result} - {t}', 'warn')

    if (options or {}).get('strip_metadata_keep_copyright') and downloaded_paths:
        if log_fn:
            log_fn(f'Đang xóa metadata (giữ Copyright) cho {len(downloaded_paths)} ảnh...', 'info')
        for p in downloaded_paths:
            if stop_event and stop_event.is_set():
                break
            strip_metadata_keep_copyright(p, log_fn)

    success_count = len(downloaded_paths)
    total_count = total
    if (options or {}).get('processing_mode', 'human_edit') == 'worker':
        if success_count == total_count:
            try:
                photo_ids_to_delete = [int(p['id']) for p in all_photos if p.get('id')]
                if photo_ids_to_delete:
                    _api_post(sess, '/api/proxy/photos/delete', {'photo_ids': photo_ids_to_delete}, cookie)
            except Exception as e:
                if log_fn:
                    log_fn(f'Cảnh báo: Lỗi khi xóa ảnh ({e})', 'warn')
        elif log_fn:
            log_fn(f'Bỏ qua bước xóa ảnh vì chỉ tải thành công {success_count}/{total_count} ảnh.', 'warn')
    else:
        if log_fn:
            log_fn('Giữ lại ảnh trên tài khoản (Chế độ Inpaint).', 'info')

    try:
        _clear_spoof(sess, cookie)
    except Exception as e:
        if log_fn:
            log_fn(f'Cảnh báo: Lỗi khi clearUser ({e})', 'warn')

    return success_count


def autohdr_list_listings(cookie, log_fn):
    sess = requests.Session()
    try:
        auth = _api_get(sess, '/api/auth/get-session', cookie)
        uid = _get_active_uid(auth)
        if not uid:
            raise ValueError('Không lấy được User ID từ session.')
    except Exception as e:
        raise RuntimeError(f'Lỗi xác thực AutoHDR: {e}')

    if log_fn:
        log_fn(f'Đang tải danh sách photoshoot cho User ID {uid}...', 'info')

    shoots = []
    offset = 0
    page_size = 50
    max_listings = 100
    try:
        while len(shoots) < max_listings:
            resp = _api_get(sess, f'/api/users/{uid}/photoshoots?limit={page_size}&offset={offset}', cookie)
            page = (resp or {}).get('photoshoots') or []
            if not page:
                break
            shoots.extend(page)
            total_count = (resp or {}).get('total_count') or 0
            offset += len(page)
            if total_count and offset >= int(total_count):
                break

        from datetime import datetime, timezone
        valid_shoots_raw = []
        for s in shoots[:max_listings]:
            if s.get('status') != 'success':
                continue
            created_str = str(s.get('creation_date_utc') or '').strip()
            created_label = 'N/A'
            if created_str:
                try:
                    normalized = created_str[:-1] + '+00:00' if created_str.endswith(('Z', 'z')) else created_str
                    created_dt = datetime.fromisoformat(normalized)
                    if created_dt.tzinfo is None:
                        created_dt = created_dt.replace(tzinfo=timezone.utc)
                    created_label = created_dt.astimezone().strftime('%Y-%m-%d %H:%M')
                except Exception:
                    created_label = created_str[:16]
            valid_shoots_raw.append((s, created_label))

        valid_shoots = []
        from concurrent.futures import ThreadPoolExecutor

        def fetch_photo_count(item):
            s, created_label = item
            photo_count = 0
            try:
                count_session = requests.Session()
                photos_resp = _api_get(
                    count_session,
                    f"/api/proxy/photoshoots/{s.get('id')}/processed_photos?page=1&page_size=10",
                    cookie,
                )
                candidate_photos = _extract_photo_list(photos_resp)
                deduped = _deduplicate_autohdr_photos(candidate_photos)
                photo_count = len(deduped)
            except Exception:
                photo_count = 0
            return {
                'id': f"{s.get('id')}|{s.get('name')}" if s.get('name') else str(s.get('id')),
                'name': s.get('address') or f"Photoshoot {s.get('id')}",
                'created_at': created_label,
                'brackets': photo_count,
            }

        if valid_shoots_raw:
            with ThreadPoolExecutor(max_workers=10) as executor:
                valid_shoots = list(executor.map(fetch_photo_count, valid_shoots_raw))
        return valid_shoots
    except Exception as e:
        raise RuntimeError(f'Lỗi lấy danh sách từ AutoHDR: {e}')


def _convert_to_jpg_sips(src_path: Path, dest_path: Path) -> bool:
    if sys.platform != 'darwin':
        return False
    try:
        subprocess.run(
            ['sips', '-s', 'format', 'jpeg', str(src_path), '--out', str(dest_path)],
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
        if ext in ('.cr2', '.cr3', '.nef', '.arw', '.dng', '.raw'):
            import rawpy
            with rawpy.imread(str(src_path)) as raw:
                rgb = raw.postprocess()
            img = Image.fromarray(rgb)
            img.save(dest_path, 'JPEG', quality=92)
            return dest_path.exists()
        with Image.open(src_path) as img:
            if img.mode != 'RGB':
                img = img.convert('RGB')
            img.save(dest_path, 'JPEG', quality=92)
        return dest_path.exists()
    except Exception:
        return False


def autohdr_upload_and_enhance(cookie, input_dir, savedir, address, log_fn, progress_fn, stop_event, options=None):
    import uuid
    import hashlib
    import tempfile
    import shutil

    sess = requests.Session()
    cookie = _full_cookie_header(cookie)
    style_name = (options or {}).get('style_profile_name', 'Mặc định')
    if log_fn:
        log_fn(f'🎨 Sử dụng Style Profile: {style_name}', 'info')

    active_spoof_uid = None
    donor_info = None
    processing_mode = (options or {}).get('processing_mode', 'human_edit')

    supported_exts = {'.jpg', '.jpeg', '.png', '.arw', '.cr2', '.cr3', '.nef', '.dng', '.tiff', '.tif'}

    if processing_mode == 'donor':
        if log_fn:
            log_fn('🔧 Chế độ: Donor — mượn credit từ user khác, không cần đăng nhập', 'info')
        if isinstance(input_dir, list):
            scan_files = [Path(f) for f in input_dir if Path(f).is_file() and Path(f).suffix.lower() in supported_exts]
        else:
            input_path = Path(input_dir)
            if input_path.is_file():
                scan_files = [input_path] if input_path.suffix.lower() in supported_exts else []
            else:
                scan_files = [f for f in input_path.iterdir() if f.is_file() and f.suffix.lower() in supported_exts]
        req_credits = max(len(scan_files), 1)
        try:
            donor_info = _get_next_rotating_donor(sess, req_credits, log_fn)
            active_spoof_uid = donor_info['id']
        except Exception as e:
            raise RuntimeError(f'Không thể tìm donor: {e}') from e
    elif processing_mode == 'worker':
        if log_fn:
            log_fn('🔧 Chế độ: Dự phòng (Cloudflare Worker) — xoay tua credits', 'info')
        if isinstance(input_dir, list):
            scan_files = [Path(f) for f in input_dir if Path(f).is_file() and Path(f).suffix.lower() in supported_exts]
        else:
            input_path = Path(input_dir)
            if input_path.is_file():
                scan_files = [input_path] if input_path.suffix.lower() in supported_exts else []
            else:
                scan_files = [f for f in input_path.iterdir() if f.is_file() and f.suffix.lower() in supported_exts]
        req_credits = max(len(scan_files), 1)
        try:
            hwid = str((options or {}).get('hwid', '') or '')
            active_spoof_uid = select_best_credit_account(sess, cookie, req_credits, log_fn, hwid=hwid)
        except Exception as e:
            if log_fn:
                log_fn(f'Cảnh báo: Không thể kiểm tra / tự động xoay tua credits ({e})', 'warn')
            raise
    elif processing_mode == 'inpaint':
        if log_fn:
            log_fn('🔧 Chế độ: Inpaint Unlock — không cần credits', 'info')
    else:
        active_spoof_uid = _prepare_main_upload_identity(sess, cookie, str((options or {}).get('hwid', '') or ''))
        if log_fn:
            log_fn('🔧 Chế độ: Chính (Human Edit) — không cần credits, không cần team', 'info')

    if isinstance(input_dir, list):
        raw_files = [Path(f) for f in input_dir if Path(f).is_file() and Path(f).suffix.lower() in supported_exts]
        raw_files.sort(key=lambda x: x.name.lower())
    else:
        input_path = Path(input_dir)
        if input_path.is_file():
            raw_files = [input_path] if input_path.suffix.lower() in supported_exts else []
        else:
            raw_files = [f for f in input_path.iterdir() if f.is_file() and f.suffix.lower() in supported_exts]
            raw_files.sort(key=lambda x: x.name.lower())

    if not raw_files:
        raise ValueError('Không tìm thấy ảnh hợp lệ trong nguồn đã chọn.')

    convert_to_jpg = (options or {}).get('convert_to_jpg', True)
    files = []
    temp_dir_obj = None

    raw_camera_exts = {'.arw', '.cr2', '.cr3', '.nef', '.dng', '.raw'}
    try:
        need_conv = [
            rf for rf in raw_files
            if rf.suffix.lower() in raw_camera_exts
            or (rf.suffix.lower() not in ('.jpg', '.jpeg') and convert_to_jpg)
        ]
        if need_conv:
            temp_dir_obj = tempfile.TemporaryDirectory(prefix='autohdr_conv_')
            if log_fn:
                log_fn(f'🔄 Đang chuyển đổi song song {len(need_conv)} ảnh RAW/khác sang JPG...', 'info')

            def _conv_worker(rf):
                if stop_event and stop_event.is_set():
                    return rf, None
                dest_path = Path(temp_dir_obj.name) / f'{rf.stem}.jpg'
                ok = _convert_to_jpg_sips(rf, dest_path) or _convert_to_jpg_pil(rf, dest_path)
                return rf, (dest_path if ok else None)

            conv_map = {}
            with ThreadPoolExecutor(max_workers=min(8, len(need_conv))) as executor:
                for rf, res_path in executor.map(_conv_worker, need_conv):
                    conv_map[rf] = res_path

            for rf in raw_files:
                if rf not in conv_map:
                    files.append(rf)
                else:
                    res_path = conv_map.get(rf)
                    if res_path is not None:
                        files.append(res_path)
                    elif log_fn:
                        log_fn(f'❌ Không thể chuyển đổi {rf.name}, bỏ qua file này.', 'error')
        else:
            files = list(raw_files)

        if not files:
            raise ValueError('Không có ảnh nào sẵn sàng để upload.')

        if log_fn:
            log_fn(f'Tìm thấy {len(files)} ảnh để tải lên AutoHDR.', 'info')

        unique_str = str(uuid.uuid4())
        if log_fn:
            log_fn(f'Đã tạo UUID cho photoshoot: {unique_str}', 'info')

        dimensions = {}
        with ThreadPoolExecutor(max_workers=min(8, len(files))) as executor:
            for f, dims in zip(files, executor.map(_get_image_dimensions, files)):
                if dims:
                    dimensions[f.name] = dims
        if dimensions:
            try:
                meta_dir = Path.home() / '.autohdr_meta'
                meta_dir.mkdir(parents=True, exist_ok=True)
                with open(meta_dir / f'{unique_str}.json', 'w') as mf:
                    json.dump(dimensions, mf)
            except Exception:
                pass

        if processing_mode == 'donor' and donor_info:
            if log_fn:
                log_fn('🎯 Donor đã sẵn sàng.', 'info')
                log_fn('Đang tạo link tải lên S3 (donor mode)...', 'info')
            presigned_map = _donor_generate_presigned_urls(sess, unique_str, files)
            if log_fn:
                log_fn(f'Đang bắt đầu tải lên {len(files)} ảnh lên S3...', 'info')

            def _upload_one_donor(f_path, url):
                if stop_event and stop_event.is_set():
                    return False, f_path.name, 'Đã dừng'
                headers = {'Content-Type': 'application/octet-stream'}
                if 'x-amz-acl=private' in url:
                    headers['x-amz-acl'] = 'private'
                try:
                    with open(f_path, 'rb') as f_obj:
                        resp = requests.put(url, data=f_obj, headers=headers, timeout=120)
                    resp.raise_for_status()
                    return True, f_path.name, 'Thành công'
                except Exception as e:
                    return False, f_path.name, str(e)

            done_count = 0
            total_files = len(files)
            worker_count = min(_MAX_DOWNLOAD_WORKERS, max(1, total_files))
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = [executor.submit(_upload_one_donor, f, presigned_map[f.name]) for f in files if f.name in presigned_map]
                for future in as_completed(futures):
                    if stop_event and stop_event.is_set():
                        break
                    ok, fname, status_msg = future.result()
                    done_count += 1
                    if progress_fn:
                        progress_fn(done_count, total_files)
                    if ok:
                        if log_fn:
                            log_fn(f'  Tải lên thành công: {fname}', 'success')
                    else:
                        if log_fn:
                            log_fn(f'  Tải lên thất bại: {fname} - {status_msg}', 'error')
                        raise RuntimeError(f'Tải lên thất bại: {fname}')

            if stop_event and stop_event.is_set():
                if log_fn:
                    log_fn('Đã dừng tiến trình tải lên.', 'warn')
                return None

            if log_fn:
                log_fn('Đang hoàn tất quá trình tải lên (Finalizing)...', 'info')
            _donor_finalize_upload(sess, unique_str)

            if log_fn:
                log_fn('🧠 Đang khởi chạy AI inference (donor mode)...', 'info')
            donor_options = dict(options or {})
            donor_options['address'] = address or 'AutoHDR Project'
            infer_result = _donor_associate_and_run(sess, unique_str, donor_info, len(files), donor_options)
            photoshoot_id = infer_result.get('photoshoot_id') if isinstance(infer_result, dict) else None
            if photoshoot_id and log_fn:
                log_fn(f'✅ Inference started! Photoshoot ID: {photoshoot_id}', 'success')

            if log_fn:
                log_fn('Đang chờ hệ thống AutoHDR xử lý ảnh...', 'info')
            target_shoot = _donor_poll_status(sess, donor_info['id'], unique_str, log_fn, stop_event)
            if not photoshoot_id:
                photoshoot_id = target_shoot.get('id')

            if log_fn:
                log_fn('✔ Dự án đã xử lý thành công! Bắt đầu tải ảnh về máy...', 'success')
            _donor_download_photos(sess, int(photoshoot_id), donor_info['id'], Path(savedir), log_fn, progress_fn, stop_event, dimensions=dimensions)
            return str(Path(savedir))

        if log_fn:
            log_fn('Đang tạo link tải lên S3...', 'info')
        payload = {
            'unique_str': unique_str,
            'files': [{'filename': f.name, 'md5': ''} for f in files],
            'address': address or '',
        }
        resp_data = _api_post(sess, '/api/proxy/generate_presigned_urls', payload, cookie)
        presigned_map = {}

        if isinstance(resp_data, list):
            for item in resp_data:
                if isinstance(item, dict):
                    fn = item.get('filename') or item.get('name')
                    url = item.get('url') or item.get('presigned_url') or item.get('upload_url')
                    if fn and url:
                        presigned_map[fn] = url
        elif isinstance(resp_data, dict):
            items = resp_data.get('files') or resp_data.get('urls') or resp_data.get('presigned_urls')
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        fn = item.get('filename') or item.get('name')
                        url = item.get('url') or item.get('presigned_url') or item.get('upload_url')
                        if fn and url:
                            presigned_map[fn] = url
            elif isinstance(items, dict):
                presigned_map = dict(items)
            else:
                for k, v in resp_data.items():
                    if isinstance(v, str) and (v.startswith('http://') or v.startswith('https://')):
                        presigned_map[k] = v

        if not presigned_map:
            found_urls = []

            def _collect_urls(obj):
                if isinstance(obj, str) and (obj.startswith('http://') or obj.startswith('https://')):
                    found_urls.append(obj)
                elif isinstance(obj, dict):
                    for val in obj.values():
                        _collect_urls(val)
                elif isinstance(obj, list):
                    for val in obj:
                        _collect_urls(val)

            _collect_urls(resp_data)
            if len(found_urls) == len(files):
                for idx, f in enumerate(files):
                    presigned_map[f.name] = found_urls[idx]

        if not presigned_map:
            raise RuntimeError('Không thể lấy được link S3 upload từ AutoHDR API. Vui lòng kiểm tra lại session.')

        if log_fn:
            log_fn(f'Đang bắt đầu tải lên {len(files)} ảnh lên S3...', 'info')

        from requests.adapters import HTTPAdapter
        s3_sess = requests.Session()
        s3_adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20)
        s3_sess.mount('https://', s3_adapter)
        s3_sess.mount('http://', s3_adapter)

        def _upload_one(f_path, url):
            if stop_event and stop_event.is_set():
                return False, f_path.name, 'Đã dừng'
            headers = {'Content-Type': 'application/octet-stream'}
            if 'x-amz-acl=private' in url:
                headers['x-amz-acl'] = 'private'
            try:
                with open(f_path, 'rb') as f_obj:
                    resp = s3_sess.put(url, data=f_obj, headers=headers, timeout=120)
                resp.raise_for_status()
                return True, f_path.name, 'Thành công'
            except Exception as e:
                return False, f_path.name, str(e)

        done_count = 0
        total_files = len(files)
        worker_count = min(_MAX_DOWNLOAD_WORKERS, max(1, total_files))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(_upload_one, f, presigned_map[f.name]) for f in files if f.name in presigned_map]
            for future in as_completed(futures):
                if stop_event and stop_event.is_set():
                    break
                ok, fname, status_msg = future.result()
                done_count += 1
                if progress_fn:
                    progress_fn(done_count, total_files)
                if ok:
                    if log_fn:
                        log_fn(f'  Tải lên thành công: {fname}', 'success')
                else:
                    if log_fn:
                        log_fn(f'  Tải lên thất bại: {fname} - {status_msg}', 'error')
                    raise RuntimeError(f'Tải lên thất bại đối với file: {fname}')

        if stop_event and stop_event.is_set():
            if log_fn:
                log_fn('Đã dừng tiến trình tải lên.', 'warn')
            return None

        if log_fn:
            log_fn('Đang hoàn tất quá trình tải lên (Finalizing)...', 'info')
        finalize_payload = {'unique_str': unique_str, 'address': address or ''}
        _api_post(sess, '/api/proxy/finalize_upload', finalize_payload, cookie)

        if log_fn:
            log_fn('Đang khởi tạo cấu hình xử lý ảnh...', 'info')
        auth = _api_get(sess, '/api/auth/get-session', cookie)
        if not auth:
            raise RuntimeError('Phiên AutoHDR bị thay đổi trong lúc xử lý. Vui lòng chạy lại tác vụ.')

        user_obj = (auth or {}).get('overrideUser') or (auth or {}).get('user') or {}
        uid = _get_active_uid(auth)
        if not uid:
            raise ValueError('Không lấy được User ID từ session.')

        name_parts = str(user_obj.get('name') or '').split()
        firstname = name_parts[0] if name_parts else 'User'
        lastname = ' '.join(name_parts[1:]) if len(name_parts) > 1 else ''

        style_settings = {}
        try:
            settings_resp = _api_get(sess, f'/api/proxy/users/{uid}/style-settings', cookie)
            style_settings = (settings_resp or {}).get('settings') or {}
        except Exception as e:
            if log_fn:
                log_fn(f'Cảnh báo: Không tải được cấu hình tùy chọn, dùng mặc định ({e})', 'warn')

        model_opt = (options or {}).get('model', 'classic')
        default_indoor = int(style_settings.get('indoor_model_id', 1))
        indoor_model_id = default_indoor
        outdoor_model_id = 4

        if model_opt == 'classic_v4':
            indoor_model_id = 16
            outdoor_model_id = 16
        elif model_opt == 'classic':
            indoor_model_id = 1
            outdoor_model_id = 4 if (options or {}).get('sky_replacement', True) else 1
        elif model_opt == 'lisa':
            indoor_model_id = 3
            outdoor_model_id = 4 if (options or {}).get('sky_replacement', True) else 3
        elif model_opt == 'twilight_golden':
            indoor_model_id = 6
            outdoor_model_id = 6
        elif model_opt == 'twilight_pink':
            indoor_model_id = 7
            outdoor_model_id = 7
        elif model_opt == 'twilight_midnight':
            indoor_model_id = 8
            outdoor_model_id = 8
        elif not (options or {}).get('sky_replacement', True):
            outdoor_model_id = indoor_model_id

        perspective_correction_opt = int(
            (options or {}).get('perspective_correction', style_settings.get('perspective_correction', 1))
        )
        grass_replacement_opt = int(
            (options or {}).get('grass_replacement', style_settings.get('grass_replacement', 0))
        )
        declutter_opt = int(
            (options or {}).get('declutter', style_settings.get('declutter', 0))
        )

        associate_payload = {
            'unique_str': unique_str,
            'email': user_obj.get('email', ''),
            'firstname': firstname,
            'lastname': lastname,
            'address': address or 'AutoHDR Project',
            'full_address': address or 'AutoHDR Project',
            'spoofId': uid,
            'smartlook_url': '',
            'indoor_model_id': indoor_model_id,
            'outdoor_model_id': outdoor_model_id,
            'files_count': len(files),
            'grass_replacement': grass_replacement_opt,
            'perspective_correction': perspective_correction_opt,
            'special_attention': False,
            'declutter': declutter_opt,
            'photoshoot_id': None,
        }
        if (options or {}).get('style_profile_id') and (options or {}).get('style_profile_id') != 'default':
            associate_payload['style_profile_id'] = (options or {})['style_profile_id']

        try:
            _trigger_standard_inference(sess, associate_payload, cookie, log_fn=log_fn)
        except Exception as inference_err:
            if log_fn:
                log_fn(f'❌ Không thể khởi chạy xử lý ảnh: {inference_err}', 'error')
            raise

        if log_fn:
            log_fn('Đang chờ hệ thống AutoHDR xử lý ảnh (Bắt đầu giám sát - Cứ 5s kiểm tra 1 lần)...', 'info')

        start_time = time.time()
        poll_timeout = 7200
        is_success = False
        has_renamed = False
        loop_count = 0
        target_shoot = None

        while time.time() - start_time < poll_timeout:
            loop_count += 1
            if stop_event and stop_event.is_set():
                if log_fn:
                    log_fn('Đã dừng tiến trình theo dõi xử lý.', 'warn')
                return None
            time.sleep(5)

            try:
                resp = _api_get(sess, f'/api/users/{uid}/photoshoots?limit=50&offset=0', cookie)
                shoots = (resp or {}).get('photoshoots') or []
                target_shoot = None
                target_idx = -1
                for idx, s in enumerate(shoots):
                    if s.get('name') == unique_str:
                        target_shoot = s
                        target_idx = idx
                        break

                if target_shoot:
                    if processing_mode == 'worker' and not has_renamed and target_idx + 1 < len(shoots):
                        prev_shoot = shoots[target_idx + 1]
                        prev_addr = prev_shoot.get('address')
                        ps_id = target_shoot.get('id')
                        if prev_addr and ps_id:
                            try:
                                headers = {
                                    'Cookie': cookie,
                                    'Accept': 'application/json',
                                    'User-Agent': _DONOR_UA,
                                }
                                sess.patch(
                                    f'{_AUTOHDR_BASE}/api/proxy/photoshoots/{ps_id}/address',
                                    json={'address': prev_addr},
                                    headers=headers,
                                    timeout=30,
                                ).raise_for_status()
                                has_renamed = True
                            except Exception:
                                pass

                    status = str(target_shoot.get('status') or '').lower()
                    location = str(target_shoot.get('location') or '').lower()
                    status_map = {
                        'in_progress': 'Đang xử lý',
                        'success': 'Thành công',
                        'failure': 'Thất bại',
                        'failed': 'Thất bại',
                        'ignore': 'Đang chờ (Hàng đợi)',
                        'pending': 'Đang chờ',
                        'queued': 'Đang xếp hàng',
                    }
                    location_map = {
                        'inference': 'AI xử lý',
                        'inpaint': 'Xóa watermark',
                        'enhance': 'Tăng cường',
                        'upload': 'Tải lên',
                        'download': 'Tải về',
                        'pre_processing': 'Tiền xử lý',
                        'post_processing': 'Hậu xử lý',
                        'uploading': 'Đang nhận file',
                    }
                    status_vn = status_map.get(status, status)
                    location_vn = location_map.get(location, location)

                    if status == 'in_progress':
                        if log_fn:
                            log_fn(f'Trạng thái hiện tại: {status_vn} (Tiến trình: {location_vn})', 'info')
                        if loop_count % 3 == 0 and log_fn:
                            log_fn(
                                f'Vẫn đang xử lý trên server AutoHDR... (Cứ 5s kiểm tra 1 lần - Đã chờ {int(time.time() - start_time)} giây)',
                                'info',
                            )
                    elif status == 'success':
                        test_photos = _fetch_all_photos(sess, unique_str, False, int(target_shoot.get('id') or 0), cookie)
                        if test_photos:
                            is_success = True
                            break
                    elif status in ('failure', 'failed'):
                        break
                else:
                    if processing_mode == 'worker':
                        uuid_shoot = _find_by_uuid(sess, unique_str, cookie)
                        if uuid_shoot:
                            target_shoot = uuid_shoot
                            is_success = True
                            if log_fn:
                                log_fn('Đã tìm thấy photoshoot dự phòng qua UUID. Bắt đầu tải ảnh...', 'success')
                            break
                        if time.time() - start_time > 120:
                            raise RuntimeError('Chế độ Dự phòng không tạo được photoshoot sau 120 giây.')
                    if loop_count % 3 == 0 and log_fn:
                        log_fn(
                            f'Đang chờ photoshoot xuất hiện trên hệ thống... (Cứ 5s kiểm tra 1 lần - Đã chờ {int(time.time() - start_time)} giây)',
                            'info',
                        )
            except RuntimeError:
                raise
            except Exception as e:
                if log_fn:
                    log_fn(f'Đang kiểm tra lại trạng thái... ({e})', 'warn')

        if not is_success:
            if target_shoot and str(target_shoot.get('status') or '').lower() in ('failure', 'failed'):
                raise RuntimeError(
                    'Hệ thống AutoHDR báo lỗi xử lý. Thường do: ảnh bị lỗi, không đủ ảnh trong 1 bracket, hoặc up nhầm ảnh thường (không phải ảnh bracket).'
                )
            raise RuntimeError('Hệ thống AutoHDR xử lý quá thời gian quy định (120 phút) hoặc thất bại.')

        if log_fn:
            log_fn('✔ Dự án đã xử lý thành công! Bắt đầu tải ảnh về máy...', 'success')

        processed_photoshoot_id = target_shoot.get('id') if target_shoot else None
        uuid_fallback_download = bool(target_shoot.get('_uuid_fallback')) if target_shoot else False
        if not processed_photoshoot_id and not uuid_fallback_download:
            raise RuntimeError('Không lấy được Photoshoot ID từ tài khoản xử lý.')

        full_url = f"https://www.autohdr.com/photoshoots/uuid/{urllib.parse.quote(unique_str, safe='')}"
        if processed_photoshoot_id:
            full_url += f'?id={processed_photoshoot_id}'

        run_clean_download(
            cookie=cookie,
            photoshoot_url=full_url,
            output_dir=Path(savedir),
            log_fn=log_fn,
            progress_fn=progress_fn,
            stop_event=stop_event,
            create_subfolder=False,
            options=options,
        )
        return str(Path(savedir))
    finally:
        if temp_dir_obj is not None:
            try:
                temp_dir_obj.cleanup()
            except Exception:
                pass
