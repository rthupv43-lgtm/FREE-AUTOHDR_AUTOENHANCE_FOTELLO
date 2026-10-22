import os
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import unquote

from PIL import Image, ImageChops, ImageFilter, ImageOps

try:
    import cv2
    import numpy as np
except Exception:
    cv2 = None
    np = None

try:
    import rawpy
except Exception:
    rawpy = None

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception:
    register_heif_opener = None

Image.MAX_IMAGE_PIXELS = None

STANDARD_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
HEIF_EXTENSIONS = {".heic", ".heif"}
RAW_EXTENSIONS = {
    ".3fr",
    ".ari",
    ".arw",
    ".bay",
    ".cr2",
    ".cr3",
    ".crw",
    ".dcr",
    ".dng",
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
    ".nef",
    ".nrw",
    ".orf",
    ".pef",
    ".raf",
    ".raw",
    ".rw2",
    ".rwl",
    ".sr2",
    ".srf",
    ".srw",
    ".x3f",
}
SUPPORTED_EXTENSIONS = STANDARD_EXTENSIONS | HEIF_EXTENSIONS | RAW_EXTENSIONS
GENERATED_NAME_SUFFIXES = {
    "ai",
    "corrected",
    "correction",
    "edit",
    "edited",
    "enhance",
    "enhanced",
    "final",
    "fotello",
    "hdr",
    "merged",
    "output",
    "perspective",
    "upscale",
    "upscaled",
}


class DetailMergeError(Exception):
    pass


def _image_files(folder):
    if isinstance(folder, (list, tuple, set)):
        files = []
        for item in folder:
            path = Path(item).expanduser()
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                files.append(path)
        return sorted(files, key=_natural_sort_key)
    root = Path(folder).expanduser()
    if not root.is_dir():
        raise DetailMergeError(f"Folder not found: {folder}")
    return sorted(
        [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS],
        key=_natural_sort_key,
    )


def _is_grouped_source(source):
    if not isinstance(source, (list, tuple, set)):
        return False
    return any(isinstance(item, (list, tuple, set)) for item in source)


def _image_groups(source):
    if not _is_grouped_source(source):
        return [[path] for path in _image_files(source)]

    groups = []
    for group in source:
        items = group if isinstance(group, (list, tuple, set)) else [group]
        files = []
        for item in items:
            path = Path(item).expanduser()
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                files.append(path)
        if files:
            groups.append(sorted(files, key=_natural_sort_key))
    return groups


def _group_lead(group):
    return group[0]


def analyze_brackets(input_source, bracket_size=3, grouping_mode="order_fixed"):
    files = _image_files(input_source)
    if not files:
        return []
    bracket_size = max(1, int(bracket_size or 1))
    mode = str(grouping_mode or "order_fixed").lower()
    if mode == "time":
        ordered = sorted(files, key=lambda path: (path.stat().st_mtime, _natural_sort_key(path)))
    else:
        ordered = sorted(files, key=_natural_sort_key)
    if bracket_size <= 1:
        return [[path] for path in ordered]
    return [ordered[index : index + bracket_size] for index in range(0, len(ordered), bracket_size)]


def _load_rgb(path):
    suffix = path.suffix.lower()
    if suffix in RAW_EXTENSIONS:
        if rawpy is None:
            raise DetailMergeError("RAW support requires rawpy.")
        with rawpy.imread(str(path)) as raw:
            rgb = raw.postprocess(
                use_camera_wb=True,
                no_auto_bright=True,
                output_bps=8,
                output_color=rawpy.ColorSpace.sRGB,
            )
        return Image.fromarray(rgb).convert("RGB")

    with Image.open(path) as image:
        return ImageOps.exif_transpose(image).convert("RGB")


def _resample_lanczos():
    return getattr(getattr(Image, "Resampling", Image), "LANCZOS")


def _natural_sort_key(path):
    text = unquote(str(path)).lower()
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


def _fit_to_target(image, target_size):
    if image.size == target_size:
        return image
    return ImageOps.fit(
        image,
        target_size,
        method=_resample_lanczos(),
        centering=(0.5, 0.5),
    )


def _scale_for_alignment(size, max_side=1600):
    longest = max(size)
    if longest <= max_side:
        return 1.0
    return max_side / float(longest)


def _gray_for_alignment(image, scale):
    if cv2 is None or np is None:
        return None
    if scale < 1.0:
        new_size = (
            max(1, round(image.size[0] * scale)),
            max(1, round(image.size[1] * scale)),
        )
        image = image.resize(new_size, _resample_lanczos())
    gray = np.array(ImageOps.grayscale(image))
    return cv2.equalizeHist(gray)


def _homography_is_sane(matrix, original_size, target_size):
    if matrix is None or cv2 is None or np is None:
        return False
    width, height = original_size
    target_width, target_height = target_size
    corners = np.float32(
        [
            [0, 0],
            [width, 0],
            [width, height],
            [0, height],
        ]
    ).reshape(-1, 1, 2)
    warped = cv2.perspectiveTransform(corners, matrix).reshape(-1, 2)
    if not np.isfinite(warped).all():
        return False
    area = abs(cv2.contourArea(warped.astype(np.float32)))
    target_area = float(target_width * target_height)
    if target_area <= 0 or area < target_area * 0.25 or area > target_area * 4.0:
        return False
    x_min, y_min = warped.min(axis=0)
    x_max, y_max = warped.max(axis=0)
    if x_min < -target_width * 0.35 or y_min < -target_height * 0.35:
        return False
    if x_max > target_width * 1.35 or y_max > target_height * 1.35:
        return False
    return True


def _find_homography(source_image, target_image):
    if cv2 is None or np is None:
        return None

    scale_source = _scale_for_alignment(source_image.size)
    scale_target = _scale_for_alignment(target_image.size)
    gray_source = _gray_for_alignment(source_image, scale_source)
    gray_target = _gray_for_alignment(target_image, scale_target)
    if gray_source is None or gray_target is None:
        return None

    detector_sift = getattr(cv2, "SIFT_create", None)
    if detector_sift is not None:
        detector = detector_sift(nfeatures=8000, contrastThreshold=0.04)
        kp_source, desc_source = detector.detectAndCompute(gray_source, None)
        kp_target, desc_target = detector.detectAndCompute(gray_target, None)
        norm_type = cv2.NORM_L2
        ratio_thresh = 0.75
    else:
        detector = cv2.ORB_create(nfeatures=8000, fastThreshold=7)
        kp_source, desc_source = detector.detectAndCompute(gray_source, None)
        kp_target, desc_target = detector.detectAndCompute(gray_target, None)
        norm_type = cv2.NORM_HAMMING
        ratio_thresh = 0.78

    if desc_source is None or desc_target is None:
        return None

    matcher = cv2.BFMatcher(norm_type)
    raw_matches = matcher.knnMatch(desc_source, desc_target, k=2)
    good = []
    for pair in raw_matches:
        if len(pair) < 2:
            continue
        first, second = pair
        if first.distance < ratio_thresh * second.distance:
            good.append(first)

    if len(good) < 12:
        return None

    src_points = np.float32(
        [
            [kp_source[match.queryIdx].pt[0] / scale_source, kp_source[match.queryIdx].pt[1] / scale_source]
            for match in good
        ]
    ).reshape(-1, 1, 2)
    dst_points = np.float32(
        [
            [kp_target[match.trainIdx].pt[0] / scale_target, kp_target[match.trainIdx].pt[1] / scale_target]
            for match in good
        ]
    ).reshape(-1, 1, 2)

    matrix, mask = cv2.findHomography(src_points, dst_points, cv2.RANSAC, 3.0)
    if matrix is None or mask is None:
        return None

    inliers = int(mask.ravel().sum())
    if inliers < max(12, int(len(good) * 0.2)):
        return None
    if not _homography_is_sane(matrix, source_image.size, target_image.size):
        return None
    return matrix


def _warp_image(image, matrix, target_size):
    if cv2 is None or np is None:
        return None
    image_array = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
    warped = cv2.warpPerspective(
        image_array,
        matrix,
        target_size,
        flags=cv2.INTER_LANCZOS4,
        borderMode=cv2.BORDER_REFLECT_101,
    )
    return Image.fromarray(cv2.cvtColor(warped, cv2.COLOR_BGR2RGB))


def _edge_alignment_score(source_image, target_image, max_side=1200):
    if cv2 is None or np is None or source_image.size != target_image.size:
        return 0.0
    scale = _scale_for_alignment(source_image.size, max_side=max_side)
    if scale < 1.0:
        size = (
            max(1, round(source_image.size[0] * scale)),
            max(1, round(source_image.size[1] * scale)),
        )
        source_image = source_image.resize(size, _resample_lanczos())
        target_image = target_image.resize(size, _resample_lanczos())
    source_gray = cv2.GaussianBlur(np.array(ImageOps.grayscale(source_image)), (3, 3), 0)
    target_gray = cv2.GaussianBlur(np.array(ImageOps.grayscale(target_image)), (3, 3), 0)
    source_edges = cv2.Canny(source_gray, 55, 150)
    target_edges = cv2.Canny(target_gray, 55, 150)
    kernel = np.ones((3, 3), np.uint8)
    source_edges = cv2.dilate(source_edges, kernel)
    target_edges = cv2.dilate(target_edges, kernel)
    source_count = int((source_edges > 0).sum())
    target_count = int((target_edges > 0).sum())
    if min(source_count, target_count) < 500:
        return 0.0
    overlap = int(np.logical_and(source_edges > 0, target_edges > 0).sum())
    return overlap / float(min(source_count, target_count))


def _ecc_alignment_image(image, max_side=1200):
    scale = _scale_for_alignment(image.size, max_side=max_side)
    if scale < 1.0:
        size = (
            max(1, round(image.size[0] * scale)),
            max(1, round(image.size[1] * scale)),
        )
        image = image.resize(size, _resample_lanczos())
    gray = np.array(ImageOps.grayscale(image))
    gray = cv2.equalizeHist(gray)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    grad_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    grad_y = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    magnitude = cv2.magnitude(grad_x, grad_y)
    magnitude = cv2.GaussianBlur(magnitude, (5, 5), 0)
    max_value = float(magnitude.max())
    if max_value > 0:
        magnitude = magnitude / max_value
    return magnitude.astype(np.float32), scale


def _refine_aligned_image(aligned, target_image):
    if cv2 is None or np is None or aligned.size != target_image.size:
        return None
    try:
        template, scale = _ecc_alignment_image(target_image)
        moving, _ = _ecc_alignment_image(aligned)
        warp = np.eye(2, 3, dtype=np.float32)
        criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 80, 1e-5)
        _cc, warp = cv2.findTransformECC(
            template,
            moving,
            warp,
            cv2.MOTION_AFFINE,
            criteria,
            None,
            5,
        )
        if scale < 1.0:
            warp[0, 2] /= scale
            warp[1, 2] /= scale
        moving_array = cv2.cvtColor(np.array(aligned), cv2.COLOR_RGB2BGR)
        refined = cv2.warpAffine(
            moving_array,
            warp,
            target_image.size,
            flags=cv2.INTER_LANCZOS4 | cv2.WARP_INVERSE_MAP,
            borderMode=cv2.BORDER_REFLECT_101,
        )
        return Image.fromarray(cv2.cvtColor(refined, cv2.COLOR_BGR2RGB))
    except Exception:
        return None


def _aligned_original_to_ai(original, ai_image):
    matrix = _find_homography(original, ai_image)
    if matrix is None:
        return None
    aligned = _warp_image(original, matrix, ai_image.size)
    if aligned is None:
        return None
    refined = _refine_aligned_image(aligned, ai_image)
    if refined is not None and _edge_alignment_score(refined, ai_image) >= 0.52:
        return refined
    if _edge_alignment_score(aligned, ai_image) >= 0.52:
        return aligned
    return None


def _prepare_original_for_ai_canvas(original, ai_image, align=True):
    aligned = _aligned_original_to_ai(original, ai_image) if align else None
    if aligned is not None:
        return aligned, True
    fitted = _fit_to_target(original, ai_image.size)
    return fitted, _edge_alignment_score(fitted, ai_image) >= 0.52


def _save_image(image, output_path):
    output_format = (
        "PNG"
        if output_path.suffix.lower() == ".png"
        else "WEBP"
        if output_path.suffix.lower() == ".webp"
        else "JPEG"
    )
    options = {}
    if output_format == "JPEG":
        options.update(quality=98, subsampling=0)
        image = image.convert("RGB")
    elif output_format == "WEBP":
        options.update(quality=98, method=4)
        image = image.convert("RGB")
    else:
        options.update(optimize=True)

    image.save(output_path, format=output_format, **options)


def _output_path_for_ai(out_dir, ai_path):
    output_path = out_dir / ai_path.name
    if output_path.suffix.lower() not in STANDARD_EXTENSIONS:
        output_path = output_path.with_suffix(".jpg")
    return output_path


def _auto_white_balance(image, strength=0.7):
    if np is None or strength <= 0:
        return image

    arr = np.asarray(image).astype(np.float32)
    if arr.ndim != 3 or arr.shape[2] < 3:
        return image

    luminance = arr.mean(axis=2)
    valid = (luminance > 18) & (luminance < 238)
    if int(valid.sum()) < 256:
        valid = np.ones(luminance.shape, dtype=bool)

    channel_means = arr[valid].mean(axis=0)
    if not np.isfinite(channel_means).all() or float(channel_means.min()) <= 1.0:
        return image

    gray_mean = float(channel_means.mean())
    gains = 1.0 + (gray_mean / channel_means - 1.0) * float(strength)
    gains = np.clip(gains, 0.85, 1.15)
    balanced = np.clip(arr * gains.reshape(1, 1, 3), 0, 255).astype(np.uint8)
    return Image.fromarray(balanced, mode="RGB")


def _white_balance_image(image, reference_image=None):
    if np is None:
        return image
    arr = np.asarray(image).astype(np.float32)
    if arr.ndim != 3 or arr.shape[2] < 3:
        return image

    luminance = arr.mean(axis=2)
    low, high = np.percentile(luminance, [8, 96])
    mask = (luminance >= low) & (luminance <= high)
    if int(mask.sum()) < 1000:
        return image

    source_means = arr[mask].mean(axis=0)
    if reference_image is not None and reference_image.size == image.size:
        ref_arr = np.asarray(reference_image).astype(np.float32)
        ref_luminance = ref_arr.mean(axis=2)
        ref_low, ref_high = np.percentile(ref_luminance, [8, 96])
        ref_mask = (ref_luminance >= ref_low) & (ref_luminance <= ref_high)
        target_means = ref_arr[ref_mask].mean(axis=0) if int(ref_mask.sum()) >= 1000 else source_means
    else:
        target = float(source_means.mean())
        target_means = np.array([target, target, target], dtype=np.float32)

    if not np.isfinite(source_means).all() or not np.isfinite(target_means).all():
        return image
    gains = np.clip(target_means / np.maximum(source_means, 1.0), 0.78, 1.28)
    balanced = np.clip(arr * gains.reshape(1, 1, 3), 0, 255).astype(np.uint8)
    return Image.fromarray(balanced, mode="RGB")


def _align_to_reference(image, reference):
    if image.size == reference.size:
        candidate = image
    else:
        candidate = _fit_to_target(image, reference.size)
    if cv2 is None or np is None:
        return candidate
    matrix = _find_homography(image, reference)
    if matrix is None:
        return candidate
    aligned = _warp_image(image, matrix, reference.size)
    if aligned is None:
        return candidate
    refined = _refine_aligned_image(aligned, reference)
    if refined is not None and _edge_alignment_score(refined, reference) >= 0.48:
        return refined
    if _edge_alignment_score(aligned, reference) >= 0.48:
        return aligned
    return candidate


def _bracket_exposure_fusion(images):
    if not images:
        raise DetailMergeError("Bracket trống.")
    if len(images) == 1 or cv2 is None or np is None:
        return images[0]

    try:
        arrays = [cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR) for img in images]
        merge_mertens = cv2.createMergeMertens(
            contrast_weight=1.0,
            saturation_weight=1.2,
            exposure_weight=1.0,
        )
        fused = merge_mertens.process(arrays)
        fused = np.clip(fused * 255.0, 0, 255).astype(np.uint8)

        lab = cv2.cvtColor(fused, cv2.COLOR_BGR2LAB)
        l_channel, a_channel, b_channel = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8))
        l_channel = clahe.apply(l_channel)
        lab = cv2.merge((l_channel, a_channel, b_channel))
        fused = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

        fused_rgb = cv2.cvtColor(fused, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(fused_rgb)

        denoised = image.filter(ImageFilter.GaussianBlur(radius=0.5))
        image = Image.blend(image, denoised, 0.15)
        return image
    except Exception as e:
        return images[len(images) // 2]


def _load_original_entry(entry, log=None):
    if not isinstance(entry, (list, tuple)):
        return _load_rgb(entry)
    paths = list(entry)
    if not paths:
        raise DetailMergeError("Nhóm ảnh gốc trống.")
    if len(paths) == 1:
        return _load_rgb(paths[0])

    lead = paths[0]
    reference = _load_rgb(lead)
    aligned_images = [reference]
    for path in paths[1:]:
        try:
            image = _load_rgb(path)
            aligned_images.append(_align_to_reference(image, reference))
        except Exception as exc:
            if log:
                log("warn", f"Bỏ qua ảnh bracket {Path(path).name}: {exc}")
    if log:
        log("info", f"Fusion bracket gốc: {Path(lead).name} ({len(aligned_images)}/{len(paths)} ảnh)")
    return _bracket_exposure_fusion(aligned_images)


def _denoise_detail_layer(high_pass):
    denoised = high_pass.filter(ImageFilter.MedianFilter(size=3))
    return denoised.point(lambda value: 128 if abs(value - 128) <= 5 else value)


def _detail_mask(high_pass, strength):
    threshold = 7 if strength >= 0.35 else 9
    mask = high_pass.point(lambda value: min(255, max(0, (abs(value - 128) - threshold) * 18)))
    mask = mask.filter(ImageFilter.MaxFilter(size=3))
    return mask.filter(ImageFilter.GaussianBlur(radius=0.9))


def _fine_detail_mask(high_pass, y_orig, structure_guard=None):
    clean = y_orig.filter(ImageFilter.MedianFilter(size=3))
    edge = clean.filter(ImageFilter.FIND_EDGES)
    edge_mask = edge.point(lambda value: min(255, max(0, (value - 10) * 18)))
    detail_mask = high_pass.point(lambda value: min(255, max(0, (abs(value - 128) - 7) * 34)))
    highlight_guard = clean.point(lambda value: 255 if value <= 225 else max(48, 255 - (value - 225) * 9))
    mask = _multiply_masks(edge_mask, detail_mask, highlight_guard)
    if structure_guard is not None:
        mask = _multiply_masks(mask, structure_guard)
    return mask.filter(ImageFilter.GaussianBlur(radius=0.25))


def _restore_original_pixels(merged, original, detail_mask, strength, max_positive=14, max_negative=52):
    restore_amount = max(0.45, min(0.85, float(strength) + 0.35))
    alpha = detail_mask.point(lambda value: int(value * restore_amount))
    luminance_source = _with_limited_source_luminance(
        merged,
        original,
        max_positive=max_positive,
        max_negative=max_negative,
    )
    return Image.composite(luminance_source, merged, alpha)


def _multiply_masks(*masks):
    result = None
    for mask in masks:
        if mask is None:
            continue
        result = mask if result is None else ImageChops.multiply(result, mask)
    return result


def _with_source_luminance(base_image, source_image):
    source_y = source_image.convert("YCbCr").split()[0]
    _base_y, base_cb, base_cr = base_image.convert("YCbCr").split()
    return Image.merge("YCbCr", (source_y, base_cb, base_cr)).convert("RGB")


def _with_limited_source_luminance(base_image, source_image, max_delta=32, max_positive=None, max_negative=None):
    base_y, base_cb, base_cr = base_image.convert("YCbCr").split()
    source_y = source_image.convert("YCbCr").split()[0]
    if np is None:
        return _with_source_luminance(base_image, source_image)

    base_arr = np.asarray(base_y).astype(np.float32)
    source_arr = np.asarray(source_y).astype(np.float32)
    pos_limit = float(max_delta if max_positive is None else max_positive)
    neg_limit = float(max_delta if max_negative is None else max_negative)
    delta = np.clip(source_arr - base_arr, -neg_limit, pos_limit)
    limited_y = np.clip(base_arr + delta, 0, 255).astype(np.uint8)
    return Image.merge("YCbCr", (Image.fromarray(limited_y, mode="L"), base_cb, base_cr)).convert("RGB")


def _exposure_detail_guard(y_orig, y_ai):
    if np is None:
        return Image.new("L", y_orig.size, 255)
    try:
        orig = np.asarray(y_orig).astype(np.float32)
        ai = np.asarray(y_ai).astype(np.float32)
        source_headroom = np.clip(
            np.minimum(orig / 12.0, (255.0 - orig) / 12.0),
            0.0,
            1.0,
        )
        luma_delta = np.abs(orig - ai)
        diff_guard = np.clip(1.0 - (luma_delta - 40.0) / 60.0, 0.0, 1.0)
        guard = (np.minimum(source_headroom, diff_guard) * 255.0).astype(np.uint8)
        return Image.fromarray(guard, mode="L").filter(ImageFilter.GaussianBlur(radius=1.2))
    except Exception:
        return Image.new("L", y_orig.size, 255)


def _cv_luma_for_mask(image, max_side=2200, median=True):
    scale = _scale_for_alignment(image.size, max_side=max_side)
    if scale < 1.0:
        size = (
            max(1, round(image.size[0] * scale)),
            max(1, round(image.size[1] * scale)),
        )
        image = image.resize(size, _resample_lanczos())
    if median:
        image = image.filter(ImageFilter.MedianFilter(size=3))
    return np.asarray(image), scale


def _mask_from_cv_array(mask_arr, target_size):
    if (mask_arr.shape[1], mask_arr.shape[0]) != target_size:
        mask_arr = cv2.resize(mask_arr, target_size, interpolation=cv2.INTER_LINEAR)
    return Image.fromarray(np.clip(mask_arr, 0, 255).astype(np.uint8), mode="L")


def _source_edge_guard(y_orig):
    if cv2 is None or np is None:
        clean = y_orig.filter(ImageFilter.MedianFilter(size=3))
        edge = clean.filter(ImageFilter.FIND_EDGES)
        return edge.point(lambda value: min(255, max(0, (value - 8) * 20))).filter(ImageFilter.MaxFilter(size=3))
    try:
        arr, scale = _cv_luma_for_mask(y_orig, max_side=2200, median=True)
        edges = cv2.Canny(arr, 55, 145)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)
        count, labels, stats, _centroids = cv2.connectedComponentsWithStats(edges, 8)
        kept = np.zeros(edges.shape, dtype=np.uint8)
        area_scale = max(0.25, scale * scale)
        length_scale = max(0.5, scale)
        for label in range(1, count):
            x, y, width, height, area = stats[label]
            if area >= max(1, int(24 * area_scale)) or max(width, height) >= max(1, int(16 * length_scale)):
                kept[labels == label] = 255
        kept = cv2.dilate(kept, kernel, iterations=1)
        kept = cv2.GaussianBlur(kept, (3, 3), 0)
        return _mask_from_cv_array(kept, y_orig.size)
    except Exception:
        return Image.new("L", y_orig.size, 255)


def _source_object_guard(y_orig):
    if cv2 is None or np is None:
        clean = y_orig.filter(ImageFilter.MedianFilter(size=3))
        edge = clean.filter(ImageFilter.FIND_EDGES)
        mask = edge.point(lambda value: min(255, max(0, (value - 6) * 18)))
        mask = mask.filter(ImageFilter.MaxFilter(size=7))
        return mask.filter(ImageFilter.GaussianBlur(radius=1.2))
    try:
        arr, scale = _cv_luma_for_mask(y_orig, max_side=2200, median=True)
        blur = cv2.GaussianBlur(arr, (0, 0), 1.4)
        signed_delta = arr.astype(np.int16) - blur.astype(np.int16)
        bright_detail = (signed_delta >= 5).astype(np.uint8) * 255
        dark_detail = (signed_delta <= -5).astype(np.uint8) * 255
        pair_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
        paired_detail = cv2.bitwise_and(
            cv2.dilate(bright_detail, pair_kernel, iterations=1),
            cv2.dilate(dark_detail, pair_kernel, iterations=1),
        )
        paired_detail = cv2.dilate(paired_detail, pair_kernel, iterations=1)
        texture = cv2.bitwise_or(bright_detail, dark_detail)
        edges = cv2.Canny(arr, 42, 125)
        candidates = cv2.bitwise_and(cv2.bitwise_or(texture, edges), paired_detail)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        candidates = cv2.morphologyEx(candidates, cv2.MORPH_CLOSE, kernel)
        count, labels, stats, _centroids = cv2.connectedComponentsWithStats(candidates, 8)
        kept = np.zeros(candidates.shape, dtype=np.uint8)
        area_scale = max(0.25, scale * scale)
        length_scale = max(0.5, scale)
        for label in range(1, count):
            x, y, width, height, area = stats[label]
            enough_area = area >= max(1, int(24 * area_scale))
            enough_length = max(width, height) >= max(1, int(13 * length_scale))
            if enough_area or enough_length:
                kept[labels == label] = 255
        kept = cv2.dilate(kept, kernel, iterations=2)
        kept = cv2.GaussianBlur(kept, (7, 7), 0)
        return _mask_from_cv_array(kept, y_orig.size)
    except Exception:
        return Image.new("L", y_orig.size, 255)


def _white_object_guard(y_orig):
    if cv2 is None or np is None:
        clean = y_orig.filter(ImageFilter.MedianFilter(size=3))
        edge = clean.filter(ImageFilter.FIND_EDGES)
        bright = clean.point(lambda value: 255 if value >= 172 else max(0, (value - 138) * 6))
        mask = _multiply_masks(bright, edge.point(lambda value: min(255, max(0, (value - 6) * 22))))
        mask = mask.filter(ImageFilter.MaxFilter(size=9))
        return mask.filter(ImageFilter.GaussianBlur(radius=1.4))
    try:
        arr, scale = _cv_luma_for_mask(y_orig, max_side=2200, median=True)
        local_bg = cv2.GaussianBlur(arr, (0, 0), 5.0)
        local_delta = arr.astype(np.int16) - local_bg.astype(np.int16)
        bright = ((arr >= 172) | ((arr >= 145) & (local_delta >= 8))).astype(np.uint8) * 255
        dark_detail = ((arr <= 178) & ((local_bg.astype(np.int16) - arr.astype(np.int16)) >= 10)).astype(np.uint8) * 255
        edges = cv2.Canny(arr, 36, 118)
        support_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
        support = cv2.dilate(cv2.bitwise_or(dark_detail, edges), support_kernel, iterations=1)
        bright_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
        candidates = cv2.morphologyEx(bright, cv2.MORPH_CLOSE, bright_kernel)
        candidates = cv2.bitwise_and(candidates, support)
        candidates = cv2.morphologyEx(
            cv2.dilate(candidates, bright_kernel, iterations=1),
            cv2.MORPH_CLOSE,
            bright_kernel,
        )
        count, labels, stats, _centroids = cv2.connectedComponentsWithStats(candidates, 8)
        kept = np.zeros(candidates.shape, dtype=np.uint8)
        total_area = int(arr.shape[0] * arr.shape[1])
        area_scale = max(0.25, scale * scale)
        length_scale = max(0.5, scale)
        min_area = max(1, int(120 * area_scale))
        max_area = max(min_area + 1, int(total_area * 0.022))
        min_side = max(4, int(14 * length_scale))
        max_side = max(min_side + 1, int(max(arr.shape) * 0.28))
        for label in range(1, count):
            x, y_pos, width, height, area = stats[label]
            if area < min_area or area > max_area:
                continue
            if width < min_side or height < min_side or max(width, height) > max_side:
                continue
            aspect = max(width, height) / float(max(1, min(width, height)))
            if aspect > 5.5:
                continue
            component = labels == label
            support_pixels = int(np.count_nonzero(support[component]))
            bright_pixels = int(np.count_nonzero(bright[component]))
            if support_pixels < area * 0.04 or bright_pixels < area * 0.18:
                continue
            kept[component] = 255
        if int(kept.sum()) <= 0:
            return Image.new("L", y_orig.size, 0)
        kept = cv2.dilate(kept, bright_kernel, iterations=1)
        kept = cv2.GaussianBlur(kept, (9, 9), 0)
        return _mask_from_cv_array(kept, y_orig.size)
    except Exception:
        return Image.new("L", y_orig.size, 0)


def _suppress_unpaired_bright_detail(high_pass, source_detail_guard):
    if np is None or source_detail_guard is None:
        return high_pass
    detail = np.asarray(high_pass).astype(np.float32) - 128.0
    guard = (
        np.asarray(
            source_detail_guard
            if source_detail_guard.size == high_pass.size
            else source_detail_guard.resize(high_pass.size, _resample_lanczos())
        ).astype(np.float32)
        / 255.0
    )
    positive = detail > 0
    detail[positive] *= guard[positive] * 0.64 + 0.06
    return Image.fromarray(np.clip(detail + 128.0, 0, 255).astype(np.uint8), mode="L")


def _bright_speckle_guard(y_orig):
    if cv2 is None or np is None:
        return Image.new("L", y_orig.size, 255)
    try:
        arr, scale = _cv_luma_for_mask(y_orig, max_side=2200, median=False)
        blur = cv2.GaussianBlur(arr.astype(np.float32), (0, 0), 1.8)
        bright_delta = arr.astype(np.float32) - blur
        candidates = ((arr >= 132.0) & (bright_delta >= 8.0)).astype(np.uint8) * 255
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        candidates = cv2.morphologyEx(candidates, cv2.MORPH_CLOSE, kernel)
        count, labels, stats, _centroids = cv2.connectedComponentsWithStats(candidates, 8)
        suppress = np.zeros(candidates.shape, dtype=np.uint8)
        area_scale = max(0.25, scale * scale)
        length_scale = max(0.5, scale)
        for label in range(1, count):
            x, y, width, height, area = stats[label]
            long_edge = max(width, height) >= max(1, int(58 * length_scale)) and area >= max(1, int(70 * area_scale))
            chunky_edge = min(width, height) >= max(1, int(18 * length_scale)) and area >= max(1, int(180 * area_scale))
            if not (long_edge or chunky_edge):
                suppress[labels == label] = 255
        if int(suppress.sum()) <= 0:
            return Image.new("L", y_orig.size, 255)
        suppress = cv2.dilate(suppress, kernel, iterations=2)
        suppress = cv2.GaussianBlur(suppress, (7, 7), 0)
        guard = 255 - suppress
        return _mask_from_cv_array(guard, y_orig.size)
    except Exception:
        return Image.new("L", y_orig.size, 255)


def _output_edge_mask(y_image):
    if cv2 is not None and np is not None:
        try:
            arr, scale = _cv_luma_for_mask(y_image, max_side=2200, median=True)
            edges = cv2.Canny(arr, 55, 150)
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
            edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)
            count, labels, stats, _centroids = cv2.connectedComponentsWithStats(edges, 8)
            kept = np.zeros(edges.shape, dtype=np.uint8)
            area_scale = max(0.25, scale * scale)
            length_scale = max(0.5, scale)
            for label in range(1, count):
                x, y, width, height, area = stats[label]
                if area >= max(1, int(12 * area_scale)) or max(width, height) >= max(1, int(10 * length_scale)):
                    kept[labels == label] = 255
            kept = cv2.dilate(kept, kernel, iterations=1)
            kept = cv2.GaussianBlur(kept, (3, 3), 0)
            return _mask_from_cv_array(kept, y_image.size)
        except Exception:
            pass

    clean = y_image.filter(ImageFilter.MedianFilter(size=3))
    edge = clean.filter(ImageFilter.FIND_EDGES)
    mask = edge.point(lambda value: min(255, max(0, (value - 7) * 22)))
    mask = mask.filter(ImageFilter.MaxFilter(size=3))
    return mask.filter(ImageFilter.GaussianBlur(radius=0.35))


def _sharpen_luminance_edges(image, strength):
    y, cb, cr = image.convert("YCbCr").split()
    percent = int(70 + min(1.0, max(0.0, float(strength))) * 135)
    sharpened_y = y.filter(ImageFilter.UnsharpMask(radius=0.55, percent=percent, threshold=3))
    edge_mask = _output_edge_mask(y)
    y = Image.composite(sharpened_y, y, edge_mask)
    return Image.merge("YCbCr", (y, cb, cr)).convert("RGB")


def _suppress_final_bright_speckles(image, protect_mask=None):
    if cv2 is None or np is None:
        return image
    try:
        y, cb, cr = image.convert("YCbCr").split()
        arr = np.asarray(y).astype(np.uint8)
        small, scale = _cv_luma_for_mask(y, max_side=2400, median=False)
        local_bg_small = cv2.GaussianBlur(small, (0, 0), 2.0)
        delta = small.astype(np.int16) - local_bg_small.astype(np.int16)
        candidates = ((small >= 150) & (delta >= 10)).astype(np.uint8) * 255
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        candidates = cv2.morphologyEx(candidates, cv2.MORPH_CLOSE, kernel)
        count, labels, stats, _centroids = cv2.connectedComponentsWithStats(candidates, 8)
        suppress = np.zeros(candidates.shape, dtype=np.uint8)
        dark_delta = local_bg_small.astype(np.int16) - small.astype(np.int16)
        dark_detail = ((small <= 150) & (dark_delta >= 12)).astype(np.uint8) * 255
        protect_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
        protected = cv2.dilate(dark_detail, protect_kernel, iterations=1)
        area_scale = max(0.25, scale * scale)
        length_scale = max(0.5, scale)
        for label in range(1, count):
            x, y_pos, width, height, area = stats[label]
            long_grout = max(width, height) >= max(1, int(72 * length_scale)) and area >= max(1, int(95 * area_scale))
            broad_highlight = min(width, height) >= max(1, int(22 * length_scale)) and area >= max(1, int(220 * area_scale))
            if not (long_grout or broad_highlight):
                suppress[labels == label] = 255
        if int(suppress.sum()) <= 0:
            return image
        suppress = cv2.bitwise_and(suppress, cv2.bitwise_not(protected))
        if protect_mask is not None:
            protect_arr = np.asarray(
                protect_mask
                if protect_mask.size == y.size
                else protect_mask.resize(y.size, _resample_lanczos())
            ).astype(np.uint8)
            if protect_arr.shape != suppress.shape:
                protect_arr = cv2.resize(protect_arr, suppress.shape[::-1], interpolation=cv2.INTER_LINEAR)
            suppress = cv2.bitwise_and(suppress, cv2.bitwise_not(protect_arr))
        if int(suppress.sum()) <= 0:
            return image
        suppress = cv2.dilate(suppress, kernel, iterations=2)
        if (suppress.shape[1], suppress.shape[0]) != y.size:
            suppress = cv2.resize(suppress, y.size, interpolation=cv2.INTER_LINEAR)
        alpha = cv2.GaussianBlur(suppress, (7, 7), 0).astype(np.float32) / 255.0
        local_bg = cv2.GaussianBlur(arr, (0, 0), 2.0).astype(np.float32)
        repaired = np.clip(arr.astype(np.float32) * (1.0 - alpha) + local_bg * alpha, 0, 255).astype(np.uint8)
        return Image.merge("YCbCr", (Image.fromarray(repaired, mode="L"), cb, cr)).convert("RGB")
    except Exception:
        return image


def _local_alignment_mask(y_orig, y_ai):
    if cv2 is None or np is None:
        return Image.new("L", y_orig.size, 255)
    try:
        scale = min(1.0, 800.0 / max(1, max(y_orig.size)))
        size = (max(1, int(y_orig.size[0] * scale)), max(1, int(y_orig.size[1] * scale)))
        arr_orig = cv2.resize(np.array(y_orig), size, interpolation=cv2.INTER_AREA)
        arr_ai = cv2.resize(np.array(y_ai), size, interpolation=cv2.INTER_AREA)
        edge_orig = cv2.Canny(arr_orig, 40, 120)
        edge_ai = cv2.Canny(arr_ai, 40, 120)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        dilated_ai = cv2.dilate(edge_ai, kernel)
        misaligned = cv2.bitwise_and(edge_orig, cv2.bitwise_not(dilated_ai))
        density = cv2.GaussianBlur(misaligned, (31, 31), 0)
        align_mask = np.clip(255 - (density.astype(np.float32) - 5) * 12, 0, 255).astype(np.uint8)
        align_mask = cv2.resize(align_mask, y_orig.size, interpolation=cv2.INTER_LINEAR)
        return Image.fromarray(align_mask).convert("L")
    except Exception:
        return Image.new("L", y_orig.size, 255)


def _structure_guard(y_ai, expansion=5, align_mask=None):
    clean = y_ai.filter(ImageFilter.MedianFilter(size=3))
    edge = clean.filter(ImageFilter.FIND_EDGES)
    edge_mask = edge.point(lambda value: min(255, max(0, (value - 5) * 18)))

    local_blur = clean.filter(ImageFilter.GaussianBlur(radius=1.1))
    local_delta = ImageChops.subtract(clean, local_blur, scale=1.0, offset=128)
    detail_mask = local_delta.point(lambda value: min(255, max(0, (abs(value - 128) - 2) * 42)))

    mask = ImageChops.lighter(edge_mask, detail_mask)
    if align_mask is not None:
        mask = ImageChops.multiply(mask, align_mask)

    mask = mask.filter(ImageFilter.MaxFilter(size=max(1, expansion)))
    mask = mask.filter(ImageFilter.GaussianBlur(radius=0.8))
    return mask.point(lambda value: min(255, int(value * 1.35)))


def _text_detail_mask(y_orig, structure_guard=None):
    clean = y_orig.filter(ImageFilter.MedianFilter(size=3))
    edge = clean.filter(ImageFilter.FIND_EDGES)
    edge_mask = edge.point(lambda value: min(255, max(0, (value - 10) * 20)))

    local_blur = clean.filter(ImageFilter.GaussianBlur(radius=0.9))
    local_delta = ImageChops.subtract(clean, local_blur, scale=1.0, offset=128)
    contrast_mask = local_delta.point(lambda value: min(255, max(0, (abs(value - 128) - 6) * 44)))

    highlight_guard = clean.point(lambda value: 255 if value <= 225 else max(52, 255 - (value - 225) * 9))
    mask = _multiply_masks(edge_mask, contrast_mask, highlight_guard)
    if structure_guard is not None:
        mask = _multiply_masks(mask, structure_guard)
    mask = mask.filter(ImageFilter.MaxFilter(size=3))
    mask = mask.filter(ImageFilter.GaussianBlur(radius=0.1))
    return mask.point(lambda value: min(255, int(value * 2.0)))


def _restore_text_details(merged, original, y_orig, structure_guard=None):
    alpha = _text_detail_mask(y_orig, structure_guard=structure_guard)
    text_source = original.filter(ImageFilter.UnsharpMask(radius=0.4, percent=380, threshold=1))
    text_source = text_source.filter(ImageFilter.UnsharpMask(radius=0.9, percent=100, threshold=6))
    luminance_source = _with_limited_source_luminance(
        merged,
        text_source,
        max_positive=18,
        max_negative=42,
    )
    return Image.composite(luminance_source, merged, alpha)


def _restore_fine_details(merged, original, high_pass, y_orig, structure_guard=None):
    alpha = _fine_detail_mask(high_pass, y_orig, structure_guard=structure_guard)
    alpha = alpha.point(lambda value: min(255, int(value * 1.85)))
    detail_source = original.filter(ImageFilter.UnsharpMask(radius=0.55, percent=200, threshold=3))
    luminance_source = _with_limited_source_luminance(
        merged,
        detail_source,
        max_positive=16,
        max_negative=36,
    )
    return Image.composite(luminance_source, merged, alpha)


def _restore_white_objects(merged, original, white_object_guard, strength):
    if white_object_guard is None:
        return merged
    amount = max(0.62, min(0.92, 0.52 + float(strength) * 0.65))
    alpha = white_object_guard.point(lambda value: int(value * amount))
    detail_source = original.filter(ImageFilter.UnsharpMask(radius=0.45, percent=260, threshold=2))
    detail_source = detail_source.filter(ImageFilter.UnsharpMask(radius=0.9, percent=90, threshold=5))
    luminance_source = _with_limited_source_luminance(
        merged,
        detail_source,
        max_positive=48,
        max_negative=96,
    )
    return Image.composite(luminance_source, merged, alpha)


def _adjust_highlights_shadows(image, highlight_val, shadow_val):
    if highlight_val == 0.0 and shadow_val == 0.0:
        return image
    if np is None:
        return image
    arr = np.array(image.convert("YCbCr")).astype(np.float32)
    y = arr[..., 0]
    if highlight_val != 0.0:
        hi_mask = np.clip((y - 128.0) / 127.0, 0.0, 1.0)
        hi_mask = np.power(hi_mask, 2.0)
        factor_hi = (highlight_val / 100.0) * 0.38
        if factor_hi > 0:
            y = y + factor_hi * (255.0 - y) * hi_mask
        else:
            y = y + factor_hi * y * hi_mask

    if shadow_val != 0.0:
        shadow_mask = np.clip((128.0 - y) / 128.0, 0.0, 1.0)
        shadow_mask = np.power(shadow_mask, 2.0)
        factor_sh = (shadow_val / 100.0) * 0.38
        if factor_sh > 0:
            y = y + factor_sh * (255.0 - y) * shadow_mask
        else:
            y = y + factor_sh * y * shadow_mask

    y = np.clip(y, 0, 255)
    arr[..., 0] = y
    return Image.fromarray(arr.astype(np.uint8), "YCbCr").convert("RGB")


def _detail_merge_legacy(
    original,
    ai_image,
    strength,
    denoise=True,
    align=True,
    restore=True,
    detail_mode="normal",
    white_balance=True,
    highlight=0.0,
    shadow=0.0,
):
    original, alignment_ok = _prepare_original_for_ai_canvas(original, ai_image, align=align)
    if white_balance:
        original = _white_balance_image(original, reference_image=ai_image)
    detail_strength = strength if alignment_ok else max(0.25, strength * 0.7)

    y_ai, cb_ai, cr_ai = ai_image.convert("YCbCr").split()
    y_orig = original.convert("YCbCr").split()[0]

    align_mask = _local_alignment_mask(y_orig, y_ai) if alignment_ok else None
    ai_structure_guard = _structure_guard(y_ai, expansion=7, align_mask=align_mask)
    exposure_guard = _exposure_detail_guard(y_orig, y_ai)
    source_edge_guard = _source_edge_guard(y_orig)
    source_object_guard = _source_object_guard(y_orig)
    white_object_guard = _white_object_guard(y_orig)

    if align_mask is not None:
        source_edge_guard = ImageChops.multiply(source_edge_guard, align_mask)
        source_object_guard = ImageChops.multiply(source_object_guard, align_mask)
        white_object_guard = ImageChops.multiply(white_object_guard, align_mask)

    source_detail_guard = ImageChops.lighter(
        source_edge_guard,
        ImageChops.lighter(source_object_guard, white_object_guard),
    )
    structure_guard = ImageChops.lighter(
        _multiply_masks(ai_structure_guard, exposure_guard),
        source_detail_guard,
    )

    detail_source = y_orig.filter(ImageFilter.MedianFilter(size=3)) if denoise else y_orig
    blur = detail_source.filter(ImageFilter.GaussianBlur(radius=1.5))
    high_pass = ImageChops.subtract(detail_source, blur, scale=1.0, offset=128)
    if denoise:
        high_pass = _denoise_detail_layer(high_pass)
    high_pass = _suppress_unpaired_bright_detail(high_pass, source_detail_guard)

    if np is not None:
        y_orig_arr = np.array(y_orig).astype(np.float32)
        y_ai_arr = np.array(y_ai).astype(np.float32)
        luma_diff = np.abs(y_orig_arr - y_ai_arr)
        halo_mask_arr = (np.clip(1.0 - (luma_diff - 50.0) / 80.0, 0.0, 1.0) * 255).astype(np.uint8)
        halo_mask = Image.fromarray(halo_mask_arr, mode="L")
    else:
        halo_mask = Image.new("L", y_orig.size, 255)
    halo_mask = halo_mask.filter(ImageFilter.GaussianBlur(radius=3.0))

    neutral = Image.new("L", high_pass.size, 128)
    high_pass_strength = min(0.78, max(0.06, detail_strength * 1.1 + 0.06))
    scaled_high_pass = Image.blend(neutral, high_pass, high_pass_strength)
    scaled_high_pass = Image.composite(scaled_high_pass, neutral, structure_guard)
    scaled_high_pass = Image.composite(scaled_high_pass, neutral, halo_mask)

    y_merged = ImageChops.add(y_ai, scaled_high_pass, scale=1.0, offset=-128)
    merged = Image.merge("YCbCr", (y_merged, cb_ai, cr_ai)).convert("RGB")

    if restore and alignment_ok:
        restore_mask = _multiply_masks(
            _detail_mask(high_pass, detail_strength),
            structure_guard,
        )
        merged = _restore_original_pixels(
            merged,
            original,
            restore_mask,
            detail_strength,
            max_positive=12,
            max_negative=28,
        )
    if alignment_ok:
        merged = _restore_fine_details(
            merged,
            original,
            high_pass,
            y_orig,
            structure_guard=structure_guard,
        )
        merged = _restore_text_details(merged, original, y_orig, structure_guard=structure_guard)
        merged = _restore_white_objects(merged, original, white_object_guard, detail_strength)

    merged = _sharpen_luminance_edges(merged, detail_strength)
    merged = _suppress_final_bright_speckles(merged, protect_mask=white_object_guard)
    if highlight != 0.0 or shadow != 0.0:
        merged = _adjust_highlights_shadows(merged, highlight, shadow)
    return merged


def _smoothstep(edge0, edge1, values):
    """Return a smooth 0..1 transition without hard mask boundaries."""
    width = max(1e-6, float(edge1 - edge0))
    values = np.clip((values - edge0) / width, 0.0, 1.0)
    return values * values * (3.0 - 2.0 * values)


def _srgb_luma_to_linear(values):
    values = np.asarray(values, dtype=np.float32) / 255.0
    return np.where(
        values <= 0.04045,
        values / 12.92,
        np.power((values + 0.055) / 1.055, 2.4),
    ).astype(np.float32)


def _linear_luma_to_srgb(values):
    values = np.clip(values, 0.0, 1.0)
    return np.where(
        values <= 0.0031308,
        values * 12.92,
        1.055 * np.power(values, 1.0 / 2.4) - 0.055,
    ).astype(np.float32)


def _detail_confidence_mask(y_orig, y_ai, alignment_ok):
    """Build a conservative mask for exposure, noise and structural agreement.

    The mask is calculated on a smaller canvas. It deliberately uses products
    rather than unions, so a source edge can never reopen a clipped or
    misaligned area that another guard has rejected.
    """
    max_side = max(1, max(y_orig.size))
    scale = min(1.0, 1800.0 / float(max_side))
    if scale < 1.0:
        size = (max(1, round(y_orig.size[0] * scale)), max(1, round(y_orig.size[1] * scale)))
        orig_small = np.asarray(y_orig.resize(size, _resample_lanczos()), dtype=np.uint8)
        ai_small = np.asarray(y_ai.resize(size, _resample_lanczos()), dtype=np.uint8)
    else:
        orig_small = np.asarray(y_orig, dtype=np.uint8)
        ai_small = np.asarray(y_ai, dtype=np.uint8)

    orig_unit = orig_small.astype(np.float32) / 255.0
    ai_unit = ai_small.astype(np.float32) / 255.0

    source_shadow = _smoothstep(7.0 / 255.0, 38.0 / 255.0, orig_unit)
    source_highlight = 1.0 - _smoothstep(222.0 / 255.0, 250.0 / 255.0, orig_unit)
    ai_shadow = _smoothstep(3.0 / 255.0, 24.0 / 255.0, ai_unit)
    ai_highlight = 1.0 - _smoothstep(232.0 / 255.0, 253.0 / 255.0, ai_unit)
    exposure_confidence = source_shadow * source_highlight * ai_shadow * ai_highlight

    log_orig = np.log(_srgb_luma_to_linear(orig_small) + 1e-4)
    log_ai = np.log(_srgb_luma_to_linear(ai_small) + 1e-4)

    base_orig = cv2.GaussianBlur(log_orig, (0, 0), 3.2)
    base_ai = cv2.GaussianBlur(log_ai, (0, 0), 3.2)

    grad_orig_x = cv2.Sobel(base_orig, cv2.CV_32F, 1, 0, ksize=3)
    grad_orig_y = cv2.Sobel(base_orig, cv2.CV_32F, 0, 1, ksize=3)
    grad_ai_x = cv2.Sobel(base_ai, cv2.CV_32F, 1, 0, ksize=3)
    grad_ai_y = cv2.Sobel(base_ai, cv2.CV_32F, 0, 1, ksize=3)
    magnitude_orig = cv2.magnitude(grad_orig_x, grad_orig_y)
    magnitude_ai = cv2.magnitude(grad_ai_x, grad_ai_y)

    dot = grad_orig_x * grad_ai_x + grad_orig_y * grad_ai_y
    cosine = dot / (magnitude_orig * magnitude_ai + 1e-4)
    orientation = np.clip(cosine, 0.0, 1.0)
    magnitude_balance = 1.0 - np.abs(magnitude_orig - magnitude_ai) / (magnitude_orig + magnitude_ai + 0.08)
    edge_strength = np.maximum(magnitude_orig, magnitude_ai)
    edge_confidence = np.where(
        edge_strength < 0.055,
        1.0,
        np.clip(magnitude_balance, 0.0, 1.0) * (orientation * 0.82 + 0.18),
    )

    illumination_orig = cv2.GaussianBlur(base_orig, (0, 0), 12.0)
    illumination_ai = cv2.GaussianBlur(base_ai, (0, 0), 12.0)
    shape_delta = np.abs((base_orig - illumination_orig) - (base_ai - illumination_ai))
    shape_confidence = np.exp(-np.maximum(shape_delta - 0.035, 0.0) * 4.2)

    structural_confidence = np.clip(edge_confidence * shape_confidence, 0.0, 1.0)

    mismatch = ((1.0 - structural_confidence) * 255.0).astype(np.uint8)
    mismatch_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    strong_mismatch = (mismatch >= 96).astype(np.uint8) * 255
    strong_mismatch = cv2.morphologyEx(strong_mismatch, cv2.MORPH_CLOSE, mismatch_kernel)
    contours, _hierarchy = cv2.findContours(
        strong_mismatch,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )
    filled_regions = np.zeros(mismatch.shape, dtype=np.uint8)
    total_area = float(mismatch.shape[0] * mismatch.shape[1])
    min_region_area = max(32.0, total_area * 2e-5)
    max_region_area = total_area * 0.28
    for contour in contours:
        contour_area = float(cv2.contourArea(contour))
        if min_region_area <= contour_area <= max_region_area:
            cv2.drawContours(filled_regions, [contour], -1, 224, thickness=cv2.FILLED)

    mismatch = cv2.max(mismatch, filled_regions)
    mismatch = cv2.dilate(mismatch, mismatch_kernel, iterations=1)
    mismatch = cv2.GaussianBlur(mismatch, (0, 0), 1.2)
    structural_confidence = 1.0 - mismatch.astype(np.float32) / 255.0

    median_orig = cv2.medianBlur(orig_small, 3).astype(np.float32)
    impulse = np.abs(orig_small.astype(np.float32) - median_orig)
    darkness = 1.0 - _smoothstep(20.0 / 255.0, 80.0 / 255.0, orig_unit)
    noise_confidence = np.exp(-np.maximum(impulse - 3.0, 0.0) / 9.0 * darkness)

    confidence = exposure_confidence * structural_confidence * noise_confidence
    if not alignment_ok:
        confidence = confidence * 0.55
    confidence = cv2.GaussianBlur(np.clip(confidence, 0.0, 1.0), (0, 0), 0.65)
    return np.clip(confidence, 0.0, 1.0).astype(np.float32)


def _safe_log_detail_layer(y_orig, denoise=True):
    """Extract illumination-independent mid/fine-frequency source texture."""
    original_linear = _srgb_luma_to_linear(np.asarray(y_orig, dtype=np.uint8))
    log_original = np.log(original_linear + 1e-4)

    fine_sigma = 0.62 if denoise else 0.38
    fine = cv2.GaussianBlur(log_original, (0, 0), fine_sigma)
    base = cv2.GaussianBlur(log_original, (0, 0), 2.45)
    detail = fine - base

    local_energy = np.sqrt(np.maximum(cv2.GaussianBlur(detail * detail, (0, 0), 2.2), 0.0))
    local_limit = np.clip(local_energy * 2.8 + 0.006, 0.018, 0.105)
    detail = local_limit * np.tanh(detail / np.maximum(local_limit, 1e-5))
    return detail.astype(np.float32)


def _merge_safe_detail_into_ai(ai_image, detail, confidence, strength, detail_mode):
    y_ai, cb_ai, cr_ai = ai_image.convert("YCbCr").split()
    ai_srgb = np.asarray(y_ai, dtype=np.float32) / 255.0
    ai_linear = _srgb_luma_to_linear(np.asarray(y_ai, dtype=np.uint8))

    if confidence.shape != detail.shape:
        confidence = cv2.resize(
            confidence,
            (detail.shape[1], detail.shape[0]),
            interpolation=cv2.INTER_LINEAR,
        )

    mode_gain = {
        "soft": 0.82,
        "normal": 1.0,
        "natural": 1.0,
        "crisp": 1.12,
    }.get(str(detail_mode or "normal").lower(), 1.0)
    transfer_gain = min(0.82, max(0.045, 0.045 + float(strength) * 1.12)) * mode_gain
    log_delta = detail * confidence * transfer_gain
    log_limit = 0.075 + 0.035 * float(strength)
    log_delta = log_limit * np.tanh(log_delta / max(log_limit, 1e-5))

    proposed_linear = np.clip((ai_linear + 1e-4) * np.exp(log_delta) - 1e-4, 0.0, 1.0)
    proposed_srgb = _linear_luma_to_srgb(proposed_linear)
    delta_srgb = proposed_srgb - ai_srgb

    max_change = 0.052 + 0.032 * float(strength)
    positive_limit = np.maximum(np.minimum(max_change, (1.0 - ai_srgb) * 0.34), 1e-5)
    negative_limit = np.maximum(np.minimum(max_change, ai_srgb * 0.34), 1e-5)
    delta_srgb = np.where(
        delta_srgb >= 0.0,
        positive_limit * np.tanh(delta_srgb / positive_limit),
        negative_limit * np.tanh(delta_srgb / negative_limit),
    )

    merged_y = np.clip((ai_srgb + delta_srgb) * 255.0 + 0.5, 0, 255).astype(np.uint8)
    ai_y_uint8 = np.asarray(y_ai, dtype=np.uint8)
    merged_y = np.where((merged_y == 0) & (ai_y_uint8 > 0), 1, merged_y)
    merged_y = np.where((merged_y == 255) & (ai_y_uint8 < 255), 254, merged_y).astype(np.uint8)
    merged = Image.merge("YCbCr", (Image.fromarray(merged_y, mode="L"), cb_ai, cr_ai)).convert("RGB")

    merged_luma = np.asarray(merged.convert("L"), dtype=np.uint8)
    ai_reference_luma = np.asarray(ai_image.convert("L"), dtype=np.uint8)
    new_clip = ((merged_luma == 0) & (ai_reference_luma > 0)) | ((merged_luma == 255) & (ai_reference_luma < 255))
    if np.any(new_clip):
        clip_mask = Image.fromarray((new_clip.astype(np.uint8) * 255), mode="L")
        merged = Image.composite(ai_image, merged, clip_mask)
    return merged


def _detail_merge(
    original,
    ai_image,
    strength,
    denoise=True,
    align=True,
    restore=True,
    detail_mode="normal",
    white_balance=True,
    highlight=0.0,
    shadow=0.0,
):
    """Transfer source texture without transferring its exposure or clipping."""
    if cv2 is None or np is None or str(detail_mode or "").lower() == "legacy":
        return _detail_merge_legacy(
            original,
            ai_image,
            strength,
            denoise=denoise,
            align=align,
            restore=restore,
            detail_mode=detail_mode,
            white_balance=white_balance,
            highlight=highlight,
            shadow=shadow,
        )

    original, alignment_ok = _prepare_original_for_ai_canvas(original, ai_image, align=align)
    if white_balance:
        original = _white_balance_image(original, reference_image=ai_image)

    y_orig = original.convert("YCbCr").split()[0]
    y_ai = ai_image.convert("YCbCr").split()[0]
    confidence = _detail_confidence_mask(y_orig, y_ai, alignment_ok)
    detail = _safe_log_detail_layer(y_orig, denoise=denoise)

    effective_strength = min(1.0, max(0.0, float(strength)))
    if not restore:
        effective_strength *= 0.82

    merged = _merge_safe_detail_into_ai(
        ai_image,
        detail,
        confidence,
        effective_strength,
        detail_mode,
    )
    if highlight != 0.0 or shadow != 0.0:
        merged = _adjust_highlights_shadows(merged, highlight, shadow)
    return merged


def _ascii_fold(value):
    normalized = unicodedata.normalize("NFKD", str(value).replace("đ", "d").replace("Đ", "D"))
    return normalized.encode("ascii", "ignore").decode("ascii")


def _normalize_match_name(value):
    text = _ascii_fold(unquote(str(value or ""))).lower()
    parts = [part for part in re.split(r"[^a-z0-9]+", text) if part]
    while parts and parts[-1] in GENERATED_NAME_SUFFIXES:
        parts = parts[:-1]
    compact = "".join(parts)
    return compact, set(parts)


def _match_score(ai_stem, original_stem):
    ai_compact, ai_tokens = _normalize_match_name(ai_stem)
    orig_compact, orig_tokens = _normalize_match_name(original_stem)
    if not ai_compact or not orig_compact:
        return 0.0
    if ai_compact == orig_compact:
        return 1.0
    if ai_compact.startswith(orig_compact) or orig_compact.startswith(ai_compact):
        return 0.92
    common_tokens = ai_tokens & orig_tokens
    token_score = len(common_tokens) / max(len(ai_tokens), len(orig_tokens)) if ai_tokens and orig_tokens else 0.0
    sequence_score = SequenceMatcher(None, ai_compact, orig_compact).ratio()
    return max(token_score, sequence_score)


def _stem_key(path):
    return _normalize_match_name(path.stem)[0]


def _number_key(path):
    groups = re.findall(r"\d+", path.stem)
    if not groups:
        return None
    key = groups[-1].lstrip("0")
    return key or "0"


def _unique_number_map(paths):
    by_number = {}
    duplicates = set()
    for path in paths:
        key = _number_key(path)
        if not key:
            continue
        if key in by_number:
            duplicates.add(key)
        else:
            by_number[key] = path
    for key in duplicates:
        del by_number[key]
    return by_number


def _visual_features(path):
    if cv2 is None or np is None:
        return None
    try:
        image = _load_rgb(path)
        scale = _scale_for_alignment(image.size, max_side=900)
        gray = _gray_for_alignment(image, scale)
        if gray is None:
            return None
        detector = cv2.ORB_create(nfeatures=1800, fastThreshold=7)
        keypoints, descriptors = detector.detectAndCompute(gray, None)
        if descriptors is None or len(keypoints) == 0:
            return None
        return descriptors
    except Exception:
        return None


def _build_visual_cache(paths):
    return {path: features for path in paths if (features := _visual_features(path)) is not None}


def _visual_match_score(ai_features, original_features):
    if cv2 is None or ai_features is None or original_features is None:
        return 0
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    raw_matches = matcher.knnMatch(ai_features, original_features, k=2)
    good = 0
    for pair in raw_matches:
        if len(pair) < 2:
            continue
        first, second = pair
        if first.distance < 0.76 * second.distance and first.distance <= 72:
            good += 1
    return good


def _find_visual_original(original_visual_cache, ai_path):
    if not original_visual_cache:
        return None, 0
    ai_features = _visual_features(ai_path)
    if ai_features is None:
        return None, 0

    scored = [
        (_visual_match_score(ai_features, original_features), path)
        for path, original_features in original_visual_cache.items()
    ]
    scored = sorted(scored, key=lambda item: item[0], reverse=True)
    if not scored:
        return None, 0

    best_score, best_path = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0
    if best_score < 18 or (second_score > 0 and best_score < second_score * 1.35):
        return None, best_score
    return best_path, best_score


def _find_original(original_by_stem, original_by_key, original_by_number, ai_path):
    exact = original_by_stem.get(ai_path.stem.lower())
    if exact:
        return exact
    ai_stem = ai_path.stem
    best_path = None
    best_score = 0.0
    for stem, path in original_by_stem.items():
        score = _match_score(ai_stem, stem)
        if score > best_score:
            best_score = score
            best_path = path
        if score >= 0.98:
            return path
    if best_score >= 0.72:
        return best_path
    ai_key = _stem_key(ai_path)
    if ai_key:
        keyed = original_by_key.get(ai_key)
        if keyed:
            return keyed
        for stem, path in original_by_key.items():
            if ai_key.startswith(stem) or stem.startswith(ai_key):
                return path
    ai_number = _number_key(ai_path)
    if ai_number:
        return original_by_number.get(ai_number)
    return None


def _merge_worker(
    job,
    strength,
    denoise,
    align,
    restore,
    detail_mode,
    white_balance,
    highlight,
    shadow,
    log=None,
):
    ai_path = job["ai_path"]
    original_path = job["original_path"]
    original_entry = job["original_entry"]
    output_path = job["output_path"]
    matched_by_order = bool(job.get("matched_by_order"))

    original = _load_original_entry(original_entry, log=log)
    hdr_path = job.get("hdr_path")
    if hdr_path is not None and isinstance(original_entry, (list, tuple)) and len(original_entry) > 1:
        _save_image(original, hdr_path)

    ai_image = _load_rgb(ai_path)
    merged_image = _detail_merge(
        original,
        ai_image,
        strength,
        denoise=denoise,
        align=align,
        restore=restore,
        detail_mode=detail_mode,
        white_balance=white_balance,
        highlight=highlight,
        shadow=shadow,
    )
    _save_image(merged_image, output_path)

    if isinstance(original_entry, (list, tuple)) and len(original_entry) > 1:
        message = f"Merged detail: {output_path.name} + bracket {original_path.name} ({len(original_entry)} ảnh)"
    elif matched_by_order:
        message = f"Merged detail: {output_path.name} + {original_path.name} (theo thứ tự)"
    else:
        message = f"Merged detail: {output_path.name}"

    return {
        "ok": True,
        "ai_path": ai_path,
        "output_path": output_path,
        "message": message,
    }


def _merge_worker_count(total, original_groups):
    if total <= 1:
        return 1
    override = os.environ.get("AUTOFOTELLO_MERGE_WORKERS")
    if override:
        try:
            return max(1, min(total, int(override)))
        except Exception:
            pass
    cpu_count = os.cpu_count() or 1
    max_group_size = max((len(group) for group in original_groups), default=1)
    cap = 2 if max_group_size > 1 else 4
    return max(1, min(total, cpu_count, cap))


def merge_folders(
    original_dir,
    ai_dir,
    output_dir,
    strength=0.38,
    denoise=True,
    align=True,
    restore=True,
    detail_mode="normal",
    white_balance=True,
    stop_event=None,
    log=None,
    progress_fn=None,
    highlight=0.0,
    shadow=0.0,
):
    original_groups = _image_groups(original_dir)
    originals = [_group_lead(group) for group in original_groups]
    ai_files = _image_files(ai_dir)
    out_dir = Path(output_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    group_by_lead = {_group_lead(group): group for group in original_groups}
    original_by_stem = {p.stem.lower(): p for p in originals}
    original_by_key = {_stem_key(p): p for p in originals if _stem_key(p)}
    original_by_number = _unique_number_map(originals)
    original_visual_cache = None

    total = len(ai_files)
    merged = 0
    skipped = 0
    failed = 0
    saved_paths = []
    completed = 0
    jobs = []

    if progress_fn:
        progress_fn(0, total)
    if log:
        log("info", f"Merge input: {len(originals)} ảnh gốc, {total} ảnh AI.")

    for index, ai_path in enumerate(ai_files, start=1):
        if stop_event is not None and stop_event.is_set():
            break

        original_path = _find_original(original_by_stem, original_by_key, original_by_number, ai_path)
        matched_by_order = False
        if original_path is None and cv2 is not None and np is not None:
            if original_visual_cache is None:
                original_visual_cache = _build_visual_cache(originals)
            original_path, visual_score = _find_visual_original(original_visual_cache, ai_path)
            if original_path is not None and log:
                log("info", f"Visual match: {ai_path.name} -> {original_path.name} ({visual_score})")

        if original_path is None and len(originals) == total and index - 1 < len(originals):
            original_path = originals[index - 1]
            matched_by_order = True
            if log:
                log("info", f"Order match: {ai_path.name} -> {original_path.name}")

        if original_path is None:
            skipped += 1
            completed += 1
            if log:
                log("warn", f"Skipped: không tìm thấy ảnh gốc cho {ai_path.name}")
            if progress_fn:
                progress_fn(completed, total)
            continue

        original_entry = group_by_lead.get(original_path, [original_path])
        output_path = _output_path_for_ai(out_dir, ai_path)
        hdr_path = None
        if isinstance(original_entry, (list, tuple)) and len(original_entry) > 1:
            hdr_dir = out_dir / "Original_HDR"
            hdr_dir.mkdir(exist_ok=True)
            hdr_path = _output_path_for_ai(hdr_dir, original_path)

        jobs.append(
            {
                "index": index,
                "ai_path": ai_path,
                "original_path": original_path,
                "original_entry": original_entry,
                "output_path": output_path,
                "hdr_path": hdr_path,
                "matched_by_order": matched_by_order,
            }
        )

    worker_count = _merge_worker_count(len(jobs), original_groups)
    if log and worker_count > 1 and len(jobs) > 1:
        log("info", f"Merge song song: {worker_count} luồng xử lý cho {len(jobs)} ảnh.")

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {}
        for job in jobs:
            if stop_event is not None and stop_event.is_set():
                break
            future = executor.submit(
                _merge_worker,
                job,
                strength,
                bool(denoise),
                bool(align),
                bool(restore),
                str(detail_mode or "normal"),
                bool(white_balance),
                float(highlight),
                float(shadow),
                log,
            )
            futures[future] = job

        for future in as_completed(futures):
            job = futures[future]
            if stop_event is not None and stop_event.is_set():
                for pending in futures:
                    pending.cancel()
                break
            try:
                result = future.result()
                merged += 1
                saved_paths.append((job["index"], result["output_path"]))
                if log and result.get("message"):
                    log("success", result["message"])
            except Exception as exc:
                failed += 1
                if log:
                    log("error", f"Lỗi merge {job['ai_path'].name}: {exc}")
            completed += 1
            if progress_fn:
                progress_fn(completed, total)

    saved_paths = [str(job[1]) for job in sorted(saved_paths)]
    return {
        "ok": failed == 0,
        "total": total,
        "merged": merged,
        "skipped": skipped,
        "failed": failed,
        "output_dir": str(out_dir),
        "saved_paths": saved_paths,
    }
