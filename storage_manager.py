from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlencode

import requests

CONFIG_FILE = Path.home() / ".autofotello_storage.json"
CACHE_ROOT = Path.home() / ".autofotello_cache" / "storage"
DROPBOX_API = "https://api.dropboxapi.com/2"
DROPBOX_CONTENT = "https://content.dropboxapi.com/2"
DROPBOX_OAUTH_AUTHORIZE = "https://www.dropbox.com/oauth2/authorize"
DROPBOX_OAUTH_TOKEN = "https://api.dropboxapi.com/oauth2/token"
DEFAULT_DROPBOX_REDIRECT_URI = "http://127.0.0.1:53682/dropbox/oauth/callback"
DEFAULT_SUPABASE_URL = "https://mcqbutujzfcrrhdbiitw.supabase.co"
DROPBOX_BROKER_FUNCTION = "dropbox-oauth"
UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024  # 8388608

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
    ".cr2",
    ".cr3",
    ".crw",
    ".nef",
    ".nrw",
    ".arw",
    ".srf",
    ".sr2",
    ".dng",
    ".raw",
    ".orf",
    ".rw2",
    ".pef",
    ".raf",
    ".srw",
    ".3fr",
    ".ari",
    ".bay",
    ".dcr",
    ".erf",
    ".fff",
    ".gpr",
    ".iiq",
    ".k25",
    ".kdc",
    ".mdc",
    ".mef",
    ".mos",
    ".mrw",
    ".rwl",
    ".x3f",
}


class StorageError(Exception):
    pass


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_name(value: str, fallback: str = "item") -> str:
    name = (value or "").strip().replace("\\", "/").strip("/")
    if "/" in name:
        name = name.split("/")[-1]
    for char in '<>:"/\\|?*':
        name = name.replace(char, "_")
    return (name or fallback)[:120]


def _load_config() -> dict:
    try:
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_config(data: dict) -> None:
    CONFIG_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_status() -> dict:
    config = _load_config()
    dropbox = config.get("dropbox") if isinstance(config.get("dropbox"), dict) else {}
    token = _dropbox_token(config)
    refresh_token = _dropbox_refresh_token(config)
    app_key = _dropbox_app_key(config)
    broker = dropbox.get("broker") if isinstance(dropbox.get("broker"), dict) else {}
    broker_configured = bool(
        broker.get("connection_id")
        and broker.get("client_secret")
        and broker.get("broker_url")
    )
    return {
        "ok": True,
        "dropbox": {
            "configured": bool(token or refresh_token or broker_configured),
            "oauth_configured": bool(refresh_token and app_key),
            "broker_configured": broker_configured,
            "app_key_configured": bool(app_key),
            "redirect_uri": _dropbox_redirect_uri(config),
            "token_source": (
                "env"
                if os.environ.get("DROPBOX_ACCESS_TOKEN")
                else "broker"
                if broker_configured
                else "oauth"
                if refresh_token
                else "saved"
                if token
                else None
            ),
        },
    }


def set_dropbox_token(access_token: str) -> dict:
    access_token = (access_token or "").strip()
    if not access_token:
        return {"ok": False, "msg": "Dropbox token trống."}
    config = _load_config()
    config["dropbox"] = {"access_token": access_token}
    _save_config(config)
    return {"ok": True, **get_status()}


def clear_dropbox_token() -> dict:
    config = _load_config()
    dropbox = config.get("dropbox") if isinstance(config.get("dropbox"), dict) else {}
    keep = {
        key: dropbox[key]
        for key in ("app_key", "redirect_uri")
        if dropbox.get(key)
    }
    if keep:
        config["dropbox"] = keep
    else:
        config.pop("dropbox", None)
    _save_config(config)
    return {"ok": True, **get_status()}


def _supabase_function_url(
    supabase_url: str | None = None, function_name: str = DROPBOX_BROKER_FUNCTION
) -> str:
    base = (
        supabase_url
        or os.environ.get("SUPABASE_URL")
        or DEFAULT_SUPABASE_URL
    ).rstrip("/")
    return f"{base}/functions/v1/{function_name}"


def _supabase_headers(supabase_key: str | None = None) -> dict:
    key = (supabase_key or os.environ.get("SUPABASE_KEY") or "").strip()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["apikey"] = key
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _save_dropbox_broker_config(
    payload: dict,
    broker_url: str,
    supabase_url: str | None = None,
    supabase_key: str | None = None,
) -> None:
    config = _load_config()
    dropbox = config.get("dropbox") if isinstance(config.get("dropbox"), dict) else {}
    broker = {
        "broker_url": broker_url,
        "connection_id": payload.get("connection_id"),
        "client_secret": payload.get("client_secret"),
        "hwid": payload.get("hwid"),
        "connected": bool(payload.get("connected", False)),
        "supabase_url": supabase_url,
        "supabase_key": supabase_key,
    }
    dropbox["broker"] = {k: v for k, v in broker.items() if v is not None}
    config["dropbox"] = dropbox
    _save_config(config)


def start_dropbox_broker_oauth(
    hwid: str,
    supabase_url: str | None = None,
    supabase_key: str | None = None,
) -> dict:
    broker_url = _supabase_function_url(supabase_url)
    resp = requests.post(
        f"{broker_url}/start",
        headers=_supabase_headers(supabase_key),
        json={"hwid": hwid},
        timeout=60,
    )
    payload = resp.json() if resp.content else {}
    if not resp.ok or not payload.get("ok", True):
        raise StorageError(
            f"Supabase Dropbox OAuth lỗi {resp.status_code}: {resp.text[:400]}"
        )
    payload["hwid"] = hwid
    _save_dropbox_broker_config(
        payload, broker_url, supabase_url=supabase_url, supabase_key=supabase_key
    )
    return {"ok": True, **payload}


def _dropbox_broker_config() -> dict:
    config = _load_config()
    dropbox = config.get("dropbox") if isinstance(config.get("dropbox"), dict) else {}
    return dropbox.get("broker") if isinstance(dropbox.get("broker"), dict) else {}


def poll_dropbox_broker_status() -> dict:
    broker = _dropbox_broker_config()
    if not (
        broker.get("broker_url")
        and broker.get("connection_id")
        and broker.get("client_secret")
    ):
        return {
            "ok": False,
            "connected": False,
            "msg": "Chưa có Dropbox OAuth session.",
        }
    resp = requests.post(
        f"{broker.get('broker_url')}/status",
        headers=_supabase_headers(broker.get("supabase_key")),
        json={
            "connection_id": broker.get("connection_id"),
            "client_secret": broker.get("client_secret"),
        },
        timeout=30,
    )
    payload = resp.json() if resp.content else {}
    if not resp.ok or not payload.get("ok", True):
        return {
            "ok": False,
            "connected": False,
            "msg": payload.get("error") or resp.text[:300],
        }
    if payload.get("connected"):
        _save_dropbox_broker_config(
            {**broker, **payload, "connected": True},
            broker.get("broker_url"),
            supabase_url=broker.get("supabase_url"),
            supabase_key=broker.get("supabase_key"),
        )
    return {"ok": True, **payload, "connected": bool(payload.get("connected"))}


def _dropbox_broker_access_token() -> str:
    broker = _dropbox_broker_config()
    if not (
        broker.get("broker_url")
        and broker.get("connection_id")
        and broker.get("client_secret")
    ):
        return ""
    resp = requests.post(
        f"{broker.get('broker_url')}/token",
        headers=_supabase_headers(broker.get("supabase_key")),
        json={
            "connection_id": broker.get("connection_id"),
            "client_secret": broker.get("client_secret"),
        },
        timeout=60,
    )
    payload = resp.json() if resp.content else {}
    if not resp.ok or not payload.get("ok", True):
        raise StorageError(
            f"Không lấy được Dropbox access token từ Supabase: {resp.text[:400]}"
        )
    token = payload.get("access_token")
    if not token:
        raise StorageError("Supabase không trả Dropbox access token.")
    return str(token)


def is_remote_spec(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and str(value.get("provider", "")).lower() == "dropbox"
    )


def _dropbox_token(config: dict | None = None) -> str:
    env_token = os.environ.get("DROPBOX_ACCESS_TOKEN", "").strip()
    if env_token:
        return env_token
    config = config if isinstance(config, dict) else _load_config()
    dropbox = config.get("dropbox") if isinstance(config.get("dropbox"), dict) else {}
    return str(dropbox.get("access_token") or "").strip()


def _dropbox_refresh_token(config: dict | None = None) -> str:
    env_token = os.environ.get("DROPBOX_REFRESH_TOKEN", "").strip()
    if env_token:
        return env_token
    config = config if isinstance(config, dict) else _load_config()
    dropbox = config.get("dropbox") if isinstance(config.get("dropbox"), dict) else {}
    return str(dropbox.get("refresh_token") or "").strip()


def _dropbox_app_key(config: dict | None = None) -> str:
    env_key = os.environ.get("DROPBOX_APP_KEY", "").strip()
    if env_key:
        return env_key
    config = config if isinstance(config, dict) else _load_config()
    dropbox = config.get("dropbox") if isinstance(config.get("dropbox"), dict) else {}
    return str(dropbox.get("app_key") or "").strip()


def _dropbox_redirect_uri(config: dict | None = None) -> str:
    env_uri = os.environ.get("DROPBOX_REDIRECT_URI", "").strip()
    if env_uri:
        return env_uri
    config = config if isinstance(config, dict) else _load_config()
    dropbox = config.get("dropbox") if isinstance(config.get("dropbox"), dict) else {}
    return str(dropbox.get("redirect_uri") or DEFAULT_DROPBOX_REDIRECT_URI).strip()


def _save_dropbox_oauth_tokens(
    payload: dict, app_key: str | None = None, redirect_uri: str | None = None
) -> None:
    config = _load_config()
    dropbox = config.get("dropbox") if isinstance(config.get("dropbox"), dict) else {}
    if payload.get("access_token"):
        dropbox["access_token"] = payload["access_token"]
    if payload.get("refresh_token"):
        dropbox["refresh_token"] = payload["refresh_token"]
    if app_key:
        dropbox["app_key"] = app_key
    if redirect_uri:
        dropbox["redirect_uri"] = redirect_uri
    expires_in = payload.get("expires_in")
    if expires_in:
        dropbox["expires_at"] = int(time.time()) + int(expires_in) - 60
    if payload.get("account_id"):
        dropbox["account_id"] = payload["account_id"]
    config["dropbox"] = dropbox
    _save_config(config)


def _oauth_code_verifier() -> str:
    return secrets.token_urlsafe(64)[:128]


def _oauth_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def create_dropbox_oauth_session(
    app_key: str | None = None, redirect_uri: str | None = None
) -> dict:
    config = _load_config()
    dropbox = config.get("dropbox") if isinstance(config.get("dropbox"), dict) else {}
    app_key = (app_key or _dropbox_app_key(config)).strip()
    if not app_key:
        raise StorageError("Chưa có Dropbox App key.")
    redirect_uri = (redirect_uri or _dropbox_redirect_uri(config)).strip()
    verifier = _oauth_code_verifier()
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": app_key,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "code_challenge": _oauth_code_challenge(verifier),
        "code_challenge_method": "S256",
        "token_access_type": "offline",
        "state": state,
    }
    return {
        "ok": True,
        "app_key": app_key,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
        "state": state,
        "auth_url": f"{DROPBOX_OAUTH_AUTHORIZE}?{urlencode(params)}",
    }


def finish_dropbox_oauth(
    code: str, code_verifier: str, app_key: str, redirect_uri: str
) -> dict:
    resp = requests.post(
        DROPBOX_OAUTH_TOKEN,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": app_key,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        timeout=60,
    )
    payload = resp.json() if resp.content else {}
    if not resp.ok:
        raise StorageError(f"Dropbox OAuth lỗi {resp.status_code}: {resp.text[:400]}")
    _save_dropbox_oauth_tokens(payload, app_key=app_key, redirect_uri=redirect_uri)
    return {
        "ok": True,
        "account_id": payload.get("account_id"),
        **get_status(),
    }


def refresh_dropbox_access_token() -> str:
    config = _load_config()
    dropbox = config.get("dropbox") if isinstance(config.get("dropbox"), dict) else {}
    refresh_token = _dropbox_refresh_token(config)
    app_key = _dropbox_app_key(config)
    if not refresh_token or not app_key:
        return _dropbox_token(config)
    resp = requests.post(
        DROPBOX_OAUTH_TOKEN,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": app_key,
        },
        timeout=60,
    )
    payload = resp.json() if resp.content else {}
    if not resp.ok:
        raise StorageError(
            f"Dropbox refresh token lỗi {resp.status_code}: {resp.text[:400]}"
        )
    if not payload.get("access_token"):
        raise StorageError("Dropbox không trả access token mới.")
    _save_dropbox_oauth_tokens(payload, app_key=app_key)
    return str(payload["access_token"])


class DropboxClient:
    def __init__(self, access_token: str | None = None):
        self.access_token = access_token or self._current_access_token()
        if not self.access_token:
            raise StorageError("Chưa cấu hình Dropbox token.")
        self.session = requests.Session()

    @staticmethod
    def _current_access_token() -> str:
        config = _load_config()
        dropbox = (
            config.get("dropbox") if isinstance(config.get("dropbox"), dict) else {}
        )
        broker = (
            dropbox.get("broker") if isinstance(dropbox.get("broker"), dict) else {}
        )
        if (
            broker.get("connection_id")
            and broker.get("client_secret")
            and broker.get("broker_url")
        ):
            return _dropbox_broker_access_token()
        token = _dropbox_token(config)
        refresh_token = _dropbox_refresh_token(config)
        expires_at = dropbox.get("expires_at")
        if refresh_token and (not token or (expires_at and time.time() >= float(expires_at))):
            return refresh_dropbox_access_token()
        return token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.access_token}"}

    def _post_json(self, endpoint: str, payload: dict) -> dict:
        resp = self.session.post(
            f"{DROPBOX_API}/{endpoint}",
            headers={**self._headers(), "Content-Type": "application/json"},
            json=payload,
            timeout=60,
        )
        if resp.status_code >= 400:
            raise StorageError(
                f"Dropbox API lỗi {resp.status_code}: {resp.text[:400]}"
            )
        return resp.json() if resp.content else {}

    def get_metadata(self, path: str) -> dict:
        return self._post_json(
            "files/get_metadata", {"path": self._normalize_path(path)}
        )

    def list_folder(self, path: str, recursive: bool = False) -> list[dict]:
        payload = {
            "path": self._normalize_path(path),
            "recursive": bool(recursive),
            "include_media_info": False,
            "include_deleted": False,
            "include_has_explicit_shared_members": False,
        }
        data = self._post_json("files/list_folder", payload)
        entries = list(data.get("entries") or [])
        while data.get("has_more"):
            data = self._post_json(
                "files/list_folder/continue", {"cursor": data.get("cursor")}
            )
            entries.extend(data.get("entries") or [])
        return entries

    def download_file(self, dropbox_path: str, local_path: Path) -> Path:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        resp = self.session.post(
            f"{DROPBOX_CONTENT}/files/download",
            headers={
                **self._headers(),
                "Dropbox-API-Arg": json.dumps(
                    {"path": self._normalize_path(dropbox_path)}
                ),
            },
            stream=True,
            timeout=120,
        )
        if resp.status_code >= 400:
            raise StorageError(
                f"Dropbox download lỗi {resp.status_code}: {resp.text[:400]}"
            )
        with local_path.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)
        return local_path

    def upload_file(self, local_path: Path, dropbox_path: str) -> None:
        size = local_path.stat().st_size
        target = self._normalize_path(dropbox_path)
        if size <= 150 * 1024 * 1024:
            with local_path.open("rb") as fh:
                resp = self.session.post(
                    f"{DROPBOX_CONTENT}/files/upload",
                    headers={
                        **self._headers(),
                        "Content-Type": "application/octet-stream",
                        "Dropbox-API-Arg": json.dumps(
                            {
                                "path": target,
                                "mode": "overwrite",
                                "autorename": False,
                                "mute": True,
                            }
                        ),
                    },
                    data=fh,
                    timeout=180,
                )
            if resp.status_code >= 400:
                raise StorageError(
                    f"Dropbox upload lỗi {resp.status_code}: {resp.text[:400]}"
                )
            return

        with local_path.open("rb") as fh:
            resp = self.session.post(
                f"{DROPBOX_CONTENT}/files/upload_session/start",
                headers={
                    **self._headers(),
                    "Content-Type": "application/octet-stream",
                    "Dropbox-API-Arg": json.dumps({"close": False}),
                },
                data=fh.read(UPLOAD_CHUNK_SIZE),
                timeout=180,
            )
            if resp.status_code >= 400:
                raise StorageError(
                    f"Dropbox upload session lỗi {resp.status_code}: {resp.text[:400]}"
                )
            start = resp.json()
            session_id = start["session_id"]
            offset = fh.tell()

            while offset < size:
                chunk = fh.read(UPLOAD_CHUNK_SIZE)
                is_last = fh.tell() >= size
                endpoint = (
                    "files/upload_session/finish"
                    if is_last
                    else "files/upload_session/append_v2"
                )
                arg: dict[str, Any] = {
                    "cursor": {"session_id": session_id, "offset": offset}
                }
                if is_last:
                    arg["commit"] = {
                        "path": target,
                        "mode": "overwrite",
                        "autorename": False,
                        "mute": True,
                    }
                else:
                    arg["close"] = False

                resp = self.session.post(
                    f"{DROPBOX_CONTENT}/{endpoint}",
                    headers={
                        **self._headers(),
                        "Content-Type": "application/octet-stream",
                        "Dropbox-API-Arg": json.dumps(arg),
                    },
                    data=chunk,
                    timeout=180,
                )
                if resp.status_code >= 400:
                    raise StorageError(
                        f"Dropbox upload chunk lỗi {resp.status_code}: {resp.text[:400]}"
                    )
                offset = fh.tell()

    def upload_dir(
        self,
        local_dir: Path,
        dropbox_dir: str,
        log: Callable[[str, str], None] | None = None,
    ) -> int:
        local_dir = Path(local_dir)
        files = [p for p in sorted(local_dir.rglob("*")) if p.is_file()]
        total = len(files)
        uploaded = 0
        for index, path in enumerate(files, 1):
            rel = path.relative_to(local_dir).as_posix()
            remote_path = self._join(dropbox_dir, rel)
            if log:
                log(f"Dropbox upload [{index}/{total}]: {rel}", "info")
            self.upload_file(path, remote_path)
            uploaded += 1
        return uploaded

    def download_path(
        self,
        dropbox_path: str,
        local_root: Path,
        recursive: bool = True,
        log: Callable[[str, str], None] | None = None,
    ) -> Path | list[str]:
        metadata = self.get_metadata(dropbox_path)
        if metadata.get(".tag") == "file":
            target = local_root / _safe_name(metadata.get("name") or "file")
            if log:
                log(f"Dropbox download: {metadata.get('name')}", "info")
            return self.download_file(
                metadata.get("path_display") or dropbox_path, target
            )

        entries = self.list_folder(dropbox_path, recursive=recursive)
        files = [entry for entry in entries if entry.get(".tag") == "file"]
        base = local_root / _safe_name(dropbox_path, "dropbox")
        for index, entry in enumerate(files, 1):
            source = entry.get("path_display") or entry.get("path_lower")
            if not source:
                continue
            rel = self._relative_path(source, dropbox_path)
            if log:
                log(f"Dropbox download [{index}/{len(files)}]: {rel.as_posix()}", "info")
            self.download_file(source, base / rel)
        return base

    @staticmethod
    def _normalize_path(path: str) -> str:
        path = (path or "").strip()
        if not path or path == "/":
            return ""
        return path if path.startswith("/") else f"/{path}"

    @classmethod
    def _join(cls, root: str, rel: str) -> str:
        root = cls._normalize_path(root).rstrip("/")
        rel = (rel or "").lstrip("/")
        return f"{root}/{rel}" if root else f"/{rel}"

    @classmethod
    def _relative_path(cls, path: str, root: str) -> Path:
        norm_path = cls._normalize_path(path).strip("/")
        norm_root = cls._normalize_path(root).strip("/")
        if norm_root and norm_path.lower().startswith(f"{norm_root.lower()}/"):
            norm_path = norm_path[len(norm_root) + 1 :]
        return Path(*[_safe_name(part) for part in norm_path.split("/") if part])


@dataclass
class PreparedOutput:
    original: Any
    local_path: str
    provider: str = "local"
    remote_path: str | None = None

    @property
    def is_remote(self) -> bool:
        return self.provider == "dropbox" and bool(self.remote_path)

    def finalize(
        self,
        actual_output: str | None = None,
        log: Callable[[str, str], None] | None = None,
    ) -> None:
        if not self.is_remote:
            return
        source = Path(actual_output or self.local_path)
        if not source.exists():
            raise StorageError(f"Không tìm thấy output để upload Dropbox: {source}")
        client = DropboxClient()
        try:
            if source.is_file():
                target = DropboxClient._join(self.remote_path or "", source.name)
                client.upload_file(source, target)
                if log:
                    log(f"Đã upload kết quả lên Dropbox: {target}", "success")
            else:
                count = client.upload_dir(source, self.remote_path or "", log=log)
                if log:
                    log(
                        f"Đã upload {count} file kết quả lên Dropbox: {self.remote_path}",
                        "success",
                    )
        finally:
            try:
                import shutil

                run_dir = None
                for parent in [source] + list(source.parents):
                    if parent.parent == CACHE_ROOT:
                        run_dir = parent
                        break
                if run_dir and run_dir.exists() and run_dir.is_dir():
                    shutil.rmtree(run_dir, ignore_errors=True)
                    if log:
                        log(
                            "Đã dọn dẹp các tệp tạm cục bộ sau khi upload Dropbox.",
                            "info",
                        )
            except Exception as e:
                if log:
                    log(f"Lỗi dọn dẹp thư mục tạm: {e}", "warn")


def _run_cache_dir(run_id: str | None, purpose: str) -> Path:
    root = CACHE_ROOT / f"run_{run_id or _now_ms()}" / purpose
    root.mkdir(parents=True, exist_ok=True)
    return root


def _expand_prepared_list_item(item: Any) -> list[Any]:
    if isinstance(item, (str, os.PathLike)):
        path = Path(item).expanduser()
        if path.is_dir():
            return [
                str(p)
                for p in sorted(path.rglob("*"), key=lambda p: str(p).lower())
                if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
            ]
    return [item]


def prepare_input(
    source: Any,
    run_id: str | None = None,
    log: Callable[[str, str], None] | None = None,
    stop_event: Any = None,
) -> Any:
    if stop_event and hasattr(stop_event, "is_set") and stop_event.is_set():
        raise StorageError("Tác vụ đã bị dừng.")

    if isinstance(source, dict) and str(source.get("provider", "")).lower() == "dropbox_link":
        url = str(source.get("url") or "").strip()
        if "dl=0" in url:
            download_url = url.replace("dl=0", "dl=1")
        elif "dl=1" not in url:
            download_url = f"{url}&dl=1" if "?" in url else f"{url}?dl=1"
        else:
            download_url = url

        local_root = _run_cache_dir(run_id, "input")
        if log:
            log(
                "Đang yêu cầu Dropbox chuẩn bị tệp (có thể mất vài giây để Dropbox nén thư mục)...",
                "info",
            )
        response = requests.get(download_url, allow_redirects=True, stream=True)
        if response.status_code != 200:
            raise StorageError(
                f"Không thể tải link Dropbox (HTTP {response.status_code})"
            )

        import zipfile
        import io

        zip_data = io.BytesIO()
        total_size = 0
        last_logged = 0
        content_length = response.headers.get("content-length")
        total_expected = int(content_length) if content_length else 0

        for chunk in response.iter_content(chunk_size=262144):
            if stop_event and hasattr(stop_event, "is_set") and stop_event.is_set():
                raise StorageError("Tác vụ đã bị dừng.")
            if chunk:
                zip_data.write(chunk)
                total_size += len(chunk)
                if total_size - last_logged >= 2 * 1024 * 1024:
                    last_logged = total_size
                    mb_size = total_size / (1024 * 1024)
                    if total_expected > 0:
                        pct = int(total_size * 100 / total_expected)
                        if log:
                            log(
                                f"Đang tải: {mb_size:.1f} MB / {total_expected / (1024 * 1024):.1f} MB ({pct}%)",
                                "info",
                            )
                    else:
                        if log:
                            log(f"Đang tải: {mb_size:.1f} MB...", "info")

        zip_data.seek(0)
        if zipfile.is_zipfile(zip_data):
            if log:
                log("Đang giải nén thư mục Dropbox...", "info")
            with zipfile.ZipFile(zip_data) as z:
                z.extractall(local_root)
        else:
            filename = "downloaded_file"
            cd = response.headers.get("Content-Disposition")
            if cd and "filename=" in cd:
                import re

                names = re.findall(r'filename="?([^"]+)"?', cd)
                if names:
                    filename = names[0]
            else:
                from urllib.parse import urlparse

                url_path = urlparse(url).path
                if url_path:
                    filename = os.path.basename(url_path)

            if not any(filename.lower().endswith(ext) for ext in IMAGE_EXTENSIONS):
                filename = "image.jpg"

            file_path = local_root / _safe_name(filename)
            with open(file_path, "wb") as f:
                f.write(zip_data.getvalue())

        extracted_files = [
            str(p)
            for p in sorted(local_root.rglob("*"), key=lambda p: str(p).lower())
            if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
        ]
        if log:
            log(
                f"Đã tải và giải nén thành công {len(extracted_files)} ảnh từ link Dropbox.",
                "success",
            )
        return str(local_root)

    if is_remote_spec(source):
        paths = source.get("paths")
        if isinstance(paths, list) and paths:
            local_root = _run_cache_dir(run_id, "input")
            if log:
                log(f"Đang tải {len(paths)} ảnh từ Dropbox...", "info")
            client = DropboxClient()
            downloaded_files = []
            for idx, path in enumerate(paths, 1):
                if stop_event and hasattr(stop_event, "is_set") and stop_event.is_set():
                    raise StorageError("Tác vụ đã bị dừng.")
                target = local_root / f"{idx:03d}_{_safe_name(os.path.basename(str(path)))}"
                if log:
                    log(
                        f"Dropbox download [{idx}/{len(paths)}]: {os.path.basename(str(path))}",
                        "info",
                    )
                client.download_file(str(path), target)
                downloaded_files.append(str(target))
            return downloaded_files

        path = source.get("path") or source.get("dropbox_path") or ""
        recursive = bool(source.get("recursive", True))
        local_root = _run_cache_dir(run_id, "input")
        if log:
            log(f"Đang tải ảnh từ Dropbox: {path}", "info")
        client = DropboxClient()
        return str(client.download_path(str(path), local_root, recursive=recursive, log=log))

    if isinstance(source, (list, tuple)):
        prepared = []
        for item in source:
            prepared_item = prepare_input(
                item, run_id=run_id, log=log, stop_event=stop_event
            )
            if isinstance(prepared_item, list):
                prepared.extend(prepared_item)
            else:
                prepared.extend(_expand_prepared_list_item(prepared_item))
        return prepared

    return source


def prepare_output(target: Any, run_id: str | None = None) -> PreparedOutput:
    if is_remote_spec(target):
        remote_path = (
            target.get("path")
            or target.get("dropbox_path")
            or "/AutoFotello Output"
        )
        local_path = _run_cache_dir(run_id, "output") / _safe_name(
            str(remote_path), "output"
        )
        Path(local_path).mkdir(parents=True, exist_ok=True)
        return PreparedOutput(
            original=target,
            local_path=str(local_path),
            provider="dropbox",
            remote_path=str(remote_path),
        )
    return PreparedOutput(
        original=target,
        local_path=str(target or ""),
        provider="local",
    )


def list_dropbox_folder(path: str = "", recursive: bool = False) -> dict:
    entries = DropboxClient().list_folder(path, recursive=recursive)
    simplified = [
        {
            "type": entry.get(".tag"),
            "name": entry.get("name"),
            "path": entry.get("path_display") or entry.get("path_lower"),
            "size": entry.get("size"),
        }
        for entry in entries
    ]
    return {"ok": True, "entries": simplified}
