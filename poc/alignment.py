"""Register photographed or scanned pages to a fixed form reference."""
from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageOps


class AlignmentError(ValueError):
    pass


@lru_cache(maxsize=8)
def _bundled_reference(template_id, page_index):
    root = Path(__file__).resolve().parent.parent / "data" / "sample"
    if template_id == "bank_account" and page_index == 0:
        path = root / "bank-1.png"
        if path.exists():
            with Image.open(path) as image:
                return image.convert("RGB").copy()
    if template_id == "intake" and page_index == 0:
        path = root / "intake-1.pdf"
        if path.exists():
            from .core import render_pdf
            return render_pdf(path.read_bytes())[0]
    return None


def reference_page(template, page_index, source_dir=None):
    source = template.get("source_document_id")
    if source and source.isalnum():
        if source_dir is None:
            from .core import DATA_DIR
            source_dir = DATA_DIR
        path = Path(source_dir) / source / f"page-{page_index + 1}.png"
        if path.exists():
            try:
                with Image.open(path) as image:
                    return image.convert("RGB").copy()
            except (OSError, ValueError) as exc:
                raise AlignmentError("The saved reference page cannot be read. Upload the reference form again.") from exc
        return None
    return _bundled_reference(template.get("id"), page_index)


def _ordered(points, np):
    sums = points.sum(axis=1)
    differences = np.diff(points, axis=1).reshape(-1)
    return np.array([points[np.argmin(sums)], points[np.argmin(differences)],
                     points[np.argmax(sums)], points[np.argmax(differences)]], dtype="float32")


def _page_outline(array, target, settings, cv2, np):
    source_height, source_width = array.shape[:2]
    scale = min(1.0, 1800 / max(source_width, source_height))
    detection = cv2.resize(array, (round(source_width * scale), round(source_height * scale))) if scale < 1 else array
    gray = cv2.cvtColor(detection, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0),
                      int(settings.get("canny_low", 50)), int(settings.get("canny_high", 150)))
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    h, w = gray.shape
    wanted = target[0] / target[1]
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:25]:
        perimeter = cv2.arcLength(contour, True)
        corners = cv2.approxPolyDP(contour, .02 * perimeter, True)
        if len(corners) != 4 or cv2.contourArea(corners) < settings.get("min_page_area", .3) * w * h:
            continue
        points = _ordered(corners.reshape(4, 2).astype("float32") / scale, np)
        if len(np.unique(points, axis=0)) != 4 or not cv2.isContourConvex(points):
            continue
        top = np.linalg.norm(points[1] - points[0])
        bottom = np.linalg.norm(points[2] - points[3])
        left = np.linalg.norm(points[3] - points[0])
        right = np.linalg.norm(points[2] - points[1])
        ratio = (top + bottom) / max(1, left + right)
        if not .65 * wanted <= ratio <= 1.4 * wanted:
            continue
        if min(top, bottom) < 600 or min(left, right) < 750:
            continue
        dst = np.float32([[0, 0], [target[0]-1, 0], [target[0]-1, target[1]-1], [0, target[1]-1]])
        matrix = cv2.getPerspectiveTransform(points, dst)
        return cv2.warpPerspective(array, matrix, target, borderValue=(255, 255, 255)), "page_outline"
    return None, None


def _register(array, reference, target, fields, cv2, np):
    gray = cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    source_scale = min(1.0, 1600 / max(w, h))
    reference_scale = min(1.0, 1600 / max(target))
    detection = cv2.resize(gray, (round(w * source_scale), round(h * source_scale))) if source_scale < 1 else gray
    ref_size = (round(target[0] * reference_scale), round(target[1] * reference_scale))
    ref_gray = cv2.cvtColor(np.asarray(reference.resize(ref_size).convert("RGB")), cv2.COLOR_RGB2GRAY)
    detector = cv2.ORB_create(nfeatures=4000, fastThreshold=12)
    keypoints, descriptors = detector.detectAndCompute(detection, None)
    ref_points, ref_descriptors = detector.detectAndCompute(ref_gray, None)
    if descriptors is None or ref_descriptors is None:
        return None
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(descriptors, ref_descriptors, k=2)
    good = [pair[0] for pair in pairs if len(pair) == 2 and pair[0].distance < .72 * pair[1].distance]
    if len(good) < 18:
        return None
    src = np.float32([keypoints[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst = np.float32([ref_points[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    try:
        detected_matrix, mask = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
    except cv2.error:
        return None
    if detected_matrix is None or mask is None:
        return None
    inliers = mask.ravel().astype(bool)
    # Filled fields can lower the ratio even when many matches cover the page.
    strong_match = inliers.sum() >= 50 and inliers.mean() >= .35
    if inliers.sum() < 15 or (inliers.mean() < .42 and not strong_match):
        return None
    matched = dst[inliers, 0, :]
    spread = np.ptp(matched, axis=0)
    if spread[0] < .35 * ref_size[0] or spread[1] < .35 * ref_size[1]:
        return None
    projected = cv2.perspectiveTransform(src[inliers], detected_matrix)[:, 0, :]
    error = np.median(np.linalg.norm(projected - matched, axis=1))
    if error > 4:
        return None
    matrix = np.diag([1 / reference_scale, 1 / reference_scale, 1.0]) @ detected_matrix @ np.diag([source_scale, source_scale, 1.0])
    corners = np.float32([[[0, 0], [w-1, 0], [w-1, h-1], [0, h-1]]])
    mapped = cv2.perspectiveTransform(corners, matrix)[0]
    if (not np.isfinite(mapped).all() or np.max(np.abs(mapped)) > 20 * max(target)
            or cv2.contourArea(mapped.astype("float32"), oriented=True) < .12 * target[0] * target[1]):
        return None
    try:
        inverse = np.linalg.inv(matrix)
    except np.linalg.LinAlgError:
        return None
    page_corners = np.float32([[[0, 0], [target[0]-1, 0],
                                [target[0]-1, target[1]-1], [0, target[1]-1]]])
    source_corners = cv2.perspectiveTransform(page_corners, inverse)[0]
    if not np.isfinite(source_corners).all():
        return None
    widths = [np.linalg.norm(source_corners[1]-source_corners[0]),
              np.linalg.norm(source_corners[2]-source_corners[3])]
    heights = [np.linalg.norm(source_corners[3]-source_corners[0]),
               np.linalg.norm(source_corners[2]-source_corners[1])]
    if min(widths) < 600 or min(heights) < 750:
        raise AlignmentError("The form is too small in this image. Capture it closer or at higher resolution.")
    coverage = np.zeros((target[1], target[0]), dtype="uint8")
    cv2.fillConvexPoly(coverage, np.round(mapped).astype("int32"), 1)
    for field in fields:
        boxes = [option["region"] for option in field.get("options", [])] if field["kind"] == "choice" else [field["region"]]
        for x1, y1, x2, y2 in boxes:
            left, top = math.floor(x1), math.floor(y1)
            right, bottom = math.ceil(x2), math.ceil(y2)
            if coverage[top:bottom, left:right].mean() < .95:
                raise AlignmentError("Part of a required form field is outside the image. Capture the entire form and retry.")
    warped = cv2.warpPerspective(array, matrix, target, borderValue=(255, 255, 255))
    sharpness_image = cv2.cvtColor(warped, cv2.COLOR_RGB2GRAY)
    if max(target) > 1600:
        sharpness_image = cv2.resize(sharpness_image, ref_size)
    sharpness = cv2.Laplacian(sharpness_image, cv2.CV_64F).var()
    if sharpness < 12:
        raise AlignmentError("The form is too blurry to read reliably. Capture a sharper image and retry.")
    return warped, {"method": "reference", "inliers": int(inliers.sum()),
                    "inlier_ratio": round(float(inliers.mean()), 3), "median_error_px": round(float(error), 2),
                    "sharpness": round(float(sharpness), 1)}


def align(image, template, page_index=0, source_dir=None):
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise AlignmentError("OpenCV is required to verify and align this form.") from exc
    target = tuple(template["page_size"])
    image = ImageOps.exif_transpose(image).convert("RGB")
    array = np.asarray(image)
    if min(image.size) < 600 or max(image.size) < 750:
        raise AlignmentError("The image resolution is too low. Use a sharper scan or photograph.")
    reference = reference_page(template, page_index, source_dir)
    if reference is None:
        raise AlignmentError("This template needs a saved reference form before scanned pages can be aligned.")
    fields = template["pages"][page_index]["fields"]
    result = _register(array, reference, target, fields, cv2, np)
    if result is not None:
        return Image.fromarray(result[0]), result[1]
    candidate, method = _page_outline(array, target, template.get("settings", {}), cv2, np)
    if candidate is not None:
        result = _register(candidate, reference, target, fields, cv2, np)
        if result is not None:
            return Image.fromarray(result[0]), {**result[1], "page_detection": method}
    raise AlignmentError("Could not match this page to the selected form. Check the template, capture the whole page clearly, and retry.")
