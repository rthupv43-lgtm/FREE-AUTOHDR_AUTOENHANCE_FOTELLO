"""Remove private image metadata while retaining copyright and capture time."""

from __future__ import annotations

import os
import html
import io
import re
import shutil
import stat
import struct
import tempfile
import time
from pathlib import Path


_PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
_PNG_CONTENT_CHUNKS = {b'IHDR', b'PLTE', b'tRNS', b'acTL', b'fcTL', b'IDAT', b'fdAT', b'IEND'}
_WEBP_METADATA_CHUNKS = {b'EXIF', b'XMP ', b'ICCP'}
_WINDOWS_REPLACE_ATTEMPTS = 8
_COPYRIGHT_EXIF_TAG = 33432
_CAPTURE_TIME_EXIF_TAGS = (36867, 36881, 37521)

_COPYRIGHT_TEXT_KEYS = {
    'copyright',
    'copyright notice',
    'copyrightnotice',
    'rights',
    'usage terms',
    'usageterms',
    'license',
}

SUPPORTED_METADATA_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff', '.bmp', '.gif', '.heic', '.heif', '.avif'
}


def _normalise_copyright(value) -> str | None:
    if isinstance(value, bytes):
        for encoding in ('utf-8', 'utf-16', 'latin-1'):
            try:
                value = value.decode(encoding).strip('\x00')
                break
            except UnicodeDecodeError:
                continue
    if isinstance(value, (tuple, list)):
        value = ' / '.join(str(item) for item in value if item)
    if not isinstance(value, str):
        return None
    value = ' '.join(value.replace('\x00', ' ').split()).strip()
    return value or None


def _extract_xmp_copyright(value) -> str | None:
    text = _normalise_copyright(value)
    if not text:
        return None
    patterns = (
        r'<dc:rights[^>]*>.*?<rdf:li[^>]*>(.*?)</rdf:li>',
        r'<xmpRights:UsageTerms[^>]*>.*?<rdf:li[^>]*>(.*?)</rdf:li>',
        r'(?:photoshop:Copyright|xmpRights:WebStatement)=["\']([^"\']+)',
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        candidate = re.sub(r'<[^>]+>', ' ', match.group(1))
        candidate = _normalise_copyright(html.unescape(candidate))
        if candidate:
            return candidate
    return None


def _read_copyright(path: Path) -> str | None:
    try:
        from PIL import Image, IptcImagePlugin
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except Exception:
            pass

        with Image.open(path) as image:
            value = _normalise_copyright(image.getexif().get(_COPYRIGHT_EXIF_TAG))
            if value:
                return value

            for key, raw_value in image.info.items():
                compact_key = str(key).replace('_', ' ').strip().lower()
                if compact_key in _COPYRIGHT_TEXT_KEYS:
                    value = _normalise_copyright(raw_value)
                    if value:
                        return value
                if compact_key == 'comment':
                    value = _normalise_copyright(raw_value)
                    if value and ('copyright' in value.lower() or '©' in value):
                        return value
                if 'xmp' in compact_key:
                    value = _extract_xmp_copyright(raw_value)
                    if value:
                        return value

            try:
                iptc = IptcImagePlugin.getiptcinfo(image) or {}
                value = _normalise_copyright(iptc.get((2, 116)))
                if value:
                    return value
            except Exception:
                pass
    except Exception:
        pass

    try:
        import exifread
        with path.open('rb') as source:
            tags = exifread.process_file(source, details=False)
        for key in ('Image Copyright', 'EXIF Copyright'):
            value = _normalise_copyright(str(tags.get(key) or ''))
            if value:
                return value
    except Exception:
        return None
    return None


def _read_capture_time(path: Path) -> dict[int, object]:
    try:
        from PIL import Image
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except Exception:
            pass

        with Image.open(path) as image:
            source_exif = image.getexif()
            return {
                tag: source_exif.get(tag)
                for tag in _CAPTURE_TIME_EXIF_TAGS
                if source_exif.get(tag) not in (None, '', b'')
            }
    except Exception:
        pass

    try:
        import exifread
        with path.open('rb') as source:
            tags = exifread.process_file(source, details=False)
        result = {}
        raw_tag_names = {
            36867: 'EXIF DateTimeOriginal',
            36881: 'EXIF OffsetTimeOriginal',
            37521: 'EXIF SubSecTimeOriginal',
        }
        for tag, name in raw_tag_names.items():
            value = str(tags.get(name) or '').strip()
            if value:
                result[tag] = value
        return result
    except Exception:
        return {}


def _retained_exif(
    copyright_notice: str | None,
    capture_time: dict[int, object] | None = None,
) -> bytes | None:
    if not copyright_notice and not capture_time:
        return None
    from PIL import Image
    exif = Image.Exif()
    if copyright_notice:
        exif[_COPYRIGHT_EXIF_TAG] = copyright_notice.encode('ascii', 'replace').decode('ascii')
    for tag, value in (capture_time or {}).items():
        if tag in _CAPTURE_TIME_EXIF_TAGS and value not in (None, '', b''):
            exif[tag] = value
    return exif.tobytes()


def retained_metadata_exif(image_path: str | Path) -> bytes | None:
    """Build an EXIF block containing only Copyright and capture timestamp."""
    path = Path(image_path)
    return _retained_exif(_read_copyright(path), _read_capture_time(path))


def _insert_jpeg_retained_metadata(
    data: bytes,
    copyright_notice: str | None,
    capture_time: dict[int, object] | None = None,
) -> bytes:
    payload = _retained_exif(copyright_notice, capture_time)
    if not payload:
        return data
    if len(payload) + 2 > 65535:
        raise ValueError('Copyright quá dài để lưu trong EXIF JPEG')
    segment = b'\xff\xe1' + struct.pack('>H', len(payload) + 2) + payload
    return data[:2] + segment + data[2:]


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    import zlib
    body = chunk_type + payload
    return struct.pack('>I', len(payload)) + body + struct.pack('>I', zlib.crc32(body) & 0xFFFFFFFF)


def _encode_with_pillow(
    path: Path,
    copyright_notice: str | None,
    capture_time: dict[int, object] | None = None,
  ) -> bytes:
    """Rebuild formats whose metadata is interleaved with image storage."""
    from PIL import Image, ImageOps, ImageSequence, PngImagePlugin
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass

    with Image.open(path) as source:
        image_format = (source.format or '').upper()
        frames = [ImageOps.exif_transpose(frame.copy()) for frame in ImageSequence.Iterator(source)]
        if not frames:
            raise ValueError(f'Không đọc được pixel từ {path.name}')

        output = io.BytesIO()
        kwargs = {}
        if image_format == 'WEBP':
            kwargs.update(lossless=True, quality=100, method=6)
            exif = _retained_exif(copyright_notice, capture_time)
            if exif:
                kwargs['exif'] = exif
        elif image_format in {'TIFF', 'TIF'}:
            kwargs['compression'] = source.info.get('compression') or 'tiff_deflate'
            exif = _retained_exif(copyright_notice, capture_time)
            if exif:
                kwargs['exif'] = exif
        elif image_format == 'PNG':
            pnginfo = PngImagePlugin.PngInfo()
            if copyright_notice:
                pnginfo.add_itxt('Copyright', copyright_notice)
            kwargs['pnginfo'] = pnginfo
        elif image_format == 'GIF':
            kwargs.update(
                save_all=len(frames) > 1,
                loop=source.info.get('loop', 0),
                duration=source.info.get('duration', 0),
            )
            if copyright_notice:
                kwargs['comment'] = copyright_notice.encode('utf-8')
        elif image_format in {'HEIC', 'HEIF', 'AVIF'}:
            kwargs.update(lossless=True, quality=100)
            exif = _retained_exif(copyright_notice, capture_time)
            if exif:
                kwargs['exif'] = exif

        if len(frames) > 1 and image_format != 'GIF':
            kwargs['save_all'] = True
        if len(frames) > 1:
            kwargs['append_images'] = frames[1:]
        frames[0].save(output, format=image_format, **kwargs)
        return output.getvalue()


def create_metadata_free_copy(source_path: str | Path, destination_path: str | Path) -> Path:
    """Create an upload-safe copy without modifying the user's source image.

    EXIF orientation is applied to pixels before the tag is removed. Images
    without a non-default orientation stay on the lossless metadata-strip path.
    """
    source = Path(source_path)
    destination = Path(destination_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix.lower()
    if suffix not in SUPPORTED_METADATA_EXTENSIONS:
        raise ValueError(f"Chưa hỗ trợ xóa metadata trước upload cho định dạng {suffix or 'không xác định'}")

    orientation = 1
    try:
        from PIL import Image
        with Image.open(source) as image:
            orientation = int(image.getexif().get(274, 1) or 1)
    except Exception:
        orientation = 1

    if orientation in range(2, 9):
        from PIL import Image, ImageOps
        retained_exif = _retained_exif(
            _read_copyright(source),
            _read_capture_time(source),
        )
        with Image.open(source) as image:
            normalized = ImageOps.exif_transpose(image)
            if suffix in {'.jpg', '.jpeg'}:
                if normalized.mode not in {'RGB', 'L'}:
                    normalized = normalized.convert('RGB')
                normalized.save(
                    destination,
                    'JPEG',
                    quality=100,
                    subsampling=0,
                    **({'exif': retained_exif} if retained_exif else {}),
                )
            elif suffix == '.png':
                normalized.save(
                    destination,
                    'PNG',
                    **({'exif': retained_exif} if retained_exif else {}),
                )
            elif suffix == '.webp':
                normalized.save(
                    destination,
                    'WEBP',
                    lossless=True,
                    quality=100,
                    **({'exif': retained_exif} if retained_exif else {}),
                )
            else:
                shutil.copyfile(source, destination)
        strip_image_metadata(destination)
        return destination

    shutil.copyfile(source, destination)
    strip_image_metadata(destination)
    return destination


def _strip_jpeg(data: bytes) -> bytes:
    if not data.startswith(b'\xff\xd8'):
        raise ValueError('Không phải JPEG hợp lệ')

    output = bytearray(data[:2])
    offset = 2
    size = len(data)

    while offset < size:
        if data[offset] != 0xFF:
            raise ValueError('Cấu trúc JPEG không hợp lệ')

        marker_start = offset
        while offset < size and data[offset] == 0xFF:
            offset += 1
        if offset >= size:
            raise ValueError('JPEG bị cắt giữa marker')

        marker = data[offset]
        offset += 1

        if marker == 0xD9:
            output.extend(b'\xff\xd9')
            return bytes(output)

        if marker in {0x01, *range(0xD0, 0xD9)}:
            output.extend((0xFF, marker))
            continue

        if offset + 2 > size:
            raise ValueError('JPEG thiếu độ dài segment')
        segment_length = int.from_bytes(data[offset:offset + 2], 'big')
        if segment_length < 2 or offset + segment_length > size:
            raise ValueError('Độ dài segment JPEG không hợp lệ')
        segment_end = offset + segment_length

        is_metadata = 0xE0 <= marker <= 0xEF or marker == 0xFE
        if not is_metadata:
            output.extend((0xFF, marker))
            output.extend(data[offset:segment_end])

        offset = segment_end
        if marker != 0xDA:
            continue

        scan_start = offset
        while offset < size:
            if data[offset] != 0xFF:
                offset += 1
                continue
            probe = offset + 1
            while probe < size and data[probe] == 0xFF:
                probe += 1
            if probe >= size:
                raise ValueError('JPEG bị cắt trong scan data')
            code = data[probe]
            if code == 0x00 or 0xD0 <= code <= 0xD7:
                offset = probe + 1
                continue
            output.extend(data[scan_start:offset])
            break
        else:
            raise ValueError('JPEG thiếu marker EOI')

    raise ValueError('JPEG thiếu marker EOI')


def _strip_png(
    data: bytes,
    copyright_notice: str | None = None,
    retained_exif: bytes | None = None,
) -> bytes:
    if not data.startswith(_PNG_SIGNATURE):
        raise ValueError('Không phải PNG hợp lệ')

    output = bytearray(_PNG_SIGNATURE)
    offset = len(_PNG_SIGNATURE)
    saw_iend = False
    wrote_copyright = False
    wrote_exif = False

    while offset + 12 <= len(data):
        length = struct.unpack('>I', data[offset:offset + 4])[0]
        chunk_end = offset + 12 + length
        if chunk_end > len(data):
            raise ValueError('PNG có chunk không hợp lệ')
        chunk_type = data[offset + 4:offset + 8]

        if chunk_type in {b'IDAT', b'IEND'} and copyright_notice and not wrote_copyright:
            payload = b'Copyright\x00\x00\x00\x00\x00' + copyright_notice.encode('utf-8')
            output.extend(_png_chunk(b'iTXt', payload))
            wrote_copyright = True

        if chunk_type in {b'IDAT', b'IEND'} and retained_exif and not wrote_exif:
            payload = retained_exif[6:] if retained_exif.startswith(b'Exif\x00\x00') else retained_exif
            output.extend(_png_chunk(b'eXIf', payload))
            wrote_exif = True

        if chunk_type in _PNG_CONTENT_CHUNKS:
            output.extend(data[offset:chunk_end])
        offset = chunk_end
        if chunk_type == b'IEND':
            saw_iend = True
            break

    if not saw_iend:
        raise ValueError('PNG thiếu IEND')

    return bytes(output)


def _strip_webp(data: bytes) -> bytes:
    if len(data) < 12 or data[:4] != b'RIFF' or data[8:12] != b'WEBP':
        raise ValueError('Không phải WebP hợp lệ')

    chunks = []
    offset = 12
    while offset + 8 <= len(data):
        chunk_type = data[offset:offset + 4]
        length = struct.unpack('<I', data[offset + 4:offset + 8])[0]
        chunk_end = offset + 8 + length + (length & 1)
        if chunk_end > len(data):
            raise ValueError('WebP có chunk không hợp lệ')
        if chunk_type not in _WEBP_METADATA_CHUNKS:
            chunk = bytearray(data[offset:chunk_end])
            if chunk_type == b'VP8X' and length >= 1:
                chunk[8] &= ~0x2C
            chunks.append(bytes(chunk))
        offset = chunk_end

    payload = b'WEBP' + b''.join(chunks)
    return b'RIFF' + struct.pack('<I', len(payload)) + payload


def _replace_with_retry(source: str, destination: Path) -> None:
    """Replace a file, tolerating short-lived Windows file locks.

    Windows Explorer, thumbnail providers and antivirus scanners can briefly
    open a newly written image without sharing delete/rename access.  POSIX
    replaces the directory entry immediately, but Windows reports
    ``ERROR_ACCESS_DENIED``/``ERROR_SHARING_VIOLATION`` instead.  Retrying is
    safe here because the destination is only changed after the cleaned file
    has been completely written and fsynced.
    """
    attempts = _WINDOWS_REPLACE_ATTEMPTS if os.name == 'nt' else 1
    for attempt in range(attempts):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt >= attempts - 1:
                raise
            time.sleep(0.15 * (attempt + 1))
        except OSError as error:
            if os.name != 'nt' or getattr(error, 'winerror', None) not in {5, 32}:
                raise
            if attempt >= attempts - 1:
                raise
            time.sleep(0.15 * (attempt + 1))


def strip_image_metadata(image_path: str | Path) -> bool:
    """Strip metadata atomically, retaining Copyright and DateTimeOriginal."""
    path = Path(image_path)
    original_mode = stat.S_IMODE(path.stat().st_mode)
    data = path.read_bytes()
    suffix = path.suffix.lower()
    copyright_notice = _read_copyright(path)
    capture_time = _read_capture_time(path)
    retained_exif = _retained_exif(copyright_notice, capture_time)

    if data.startswith(b'\xff\xd8') or suffix in {'.jpg', '.jpeg'}:
        cleaned = _insert_jpeg_retained_metadata(
            _strip_jpeg(data),
            copyright_notice,
            capture_time,
        )
    elif data.startswith(_PNG_SIGNATURE) or suffix == '.png':
        cleaned = _strip_png(data, copyright_notice, retained_exif)
    elif suffix in {'.webp', '.tif', '.tiff', '.bmp', '.gif', '.heic', '.heif', '.avif'}:
        cleaned = _encode_with_pillow(path, copyright_notice, capture_time)
    else:
        raise ValueError(f"Chưa hỗ trợ xóa metadata cho định dạng {suffix or 'không xác định'}")

    if cleaned == data:
        return False

    fd, temp_name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.metadata-clean', dir=path.parent)
    made_target_writable = False
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(cleaned)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, original_mode)
        if os.name == 'nt' and not (original_mode & stat.S_IWRITE):
            os.chmod(path, original_mode | stat.S_IWRITE)
            made_target_writable = True
        _replace_with_retry(temp_name, path)
        made_target_writable = False
    except Exception:
        if made_target_writable:
            try:
                os.chmod(path, original_mode)
            except OSError:
                pass
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return True

