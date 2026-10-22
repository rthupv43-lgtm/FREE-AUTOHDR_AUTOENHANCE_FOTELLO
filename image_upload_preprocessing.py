"""Upload-side metadata preprocessing and download-side colour conversion helpers."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Callable, Iterable

from PIL import Image, ImageCms

from image_metadata import strip_image_metadata

_SRGB_PROFILE = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
_SRGB_PROFILE_BYTES = _SRGB_PROFILE.tobytes()


def convert_image_to_srgb(
    path: str | Path, target_dpi: float | None = None
) -> None:
    """Convert a finished/downloaded image to sRGB and embed the sRGB profile.

    This helper is intentionally reserved for post-download output processing.
    Upload preprocessing must never call it: source images should keep their
    original colour data/profile on the way to AutoHDR, AutoEnhance or Fotello.
    """
    path = Path(path)
    with Image.open(path) as source:
        source.load()
        image = source.copy()
        image_format = source.format
        source_profile_bytes = source.info.get("icc_profile")
        source_exif_bytes = source.info.get("exif")
        source_dpi = source.info.get("dpi")
        if not source_exif_bytes:
            try:
                source_exif = source.getexif()
                if source_exif:
                    source_exif_bytes = source_exif.tobytes()
            except Exception:
                source_exif_bytes = None

    if not image_format:
        raise ValueError("Không nhận diện được định dạng ảnh")

    if image_format.upper() == "JPEG":
        target_mode = "RGB"
    else:
        target_mode = "RGBA" if "A" in image.getbands() else "RGB"

    if source_profile_bytes:
        try:
            source_profile = ImageCms.ImageCmsProfile(BytesIO(source_profile_bytes))
            image = ImageCms.profileToProfile(
                image,
                source_profile,
                _SRGB_PROFILE,
                outputMode=target_mode,
            )
        except Exception as error:
            raise ValueError(f"ICC profile không hợp lệ: {error}") from error
    elif image.mode != target_mode:
        image = image.convert(target_mode)

    save_kwargs = {"icc_profile": _SRGB_PROFILE_BYTES}
    if source_exif_bytes:
        save_kwargs["exif"] = source_exif_bytes
    if target_dpi is not None:
        save_kwargs["dpi"] = (float(target_dpi), float(target_dpi))
    elif source_dpi and len(source_dpi) >= 2:
        dpi_x, dpi_y = source_dpi[0], source_dpi[1]
        if dpi_x and dpi_y:
            save_kwargs["dpi"] = (float(dpi_x), float(dpi_y))

    if image_format.upper() == "JPEG":
        save_kwargs.update({"quality": 100, "subsampling": 0})

    image.save(path, format=image_format, **save_kwargs)


def prepare_upload_images(
    files: Iterable[str | Path],
    *,
    remove_metadata: bool = False,
    convert_to_srgb: bool = False,
    log_fn: Callable[[str, str], None] | None = None,
) -> tuple[list[Path], str | None]:
    """Apply upload-safe preprocessing without changing the source colour space.

    ``convert_to_srgb`` remains in the signature for backwards compatibility,
    but is deliberately ignored here.  The UI option named "Convert to sRGB"
    is an output option and must run only after the processed image is
    downloaded.  This prevents source JPEG/PNG/TIFF files from being re-encoded
    or colour-transformed before upload.
    """
    paths = [Path(file_path) for file_path in files]
    _ = convert_to_srgb
    if not remove_metadata:
        return paths, None

    for source_path in paths:
        try:
            if strip_image_metadata(source_path) and log_fn:
                log_fn(
                    f"  🧹 Đã xóa metadata riêng tư, giữ Copyright và ngày giờ chụp: {source_path.name}",
                    "info",
                )
        except Exception as error:
            if log_fn:
                log_fn(
                    f"  ⚠ Không thể xóa metadata {source_path.name} trước khi upload: {error}",
                    "warn",
                )

    return paths, None

