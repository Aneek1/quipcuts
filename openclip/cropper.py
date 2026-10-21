"""Module D - Face tracking and 9:16 reframing keyframes.

Pipeline per clip:
  1. Sample frames at `sample_fps` inside [start_time, end_time].
  2. Detect faces (MediaPipe if its legacy API is installed, else OpenCV's YuNet DNN detector,
     else a Haar cascade if the OpenCV build ships one) and pick the
     primary speaker: the largest face, with ties broken by closeness to the previous position.
  3. Detect hard scene cuts (HSV histogram correlation) so tracking and smoothing restart per shot
     and the virtual camera cuts with the edit instead of panning across it.
  4. Fill gaps, apply a dead-zone (the camera ignores small head movements), then a centred
     moving average so the remaining motion becomes a smooth pan.

Two layouts are computed from the same face scan (scan_faces):
  - single: calculate_crop_keyframes -> one 9:16 window following the primary speaker.
  - split:  calculate_split_keyframes -> two windows following Face_1 / Face_2 (identities kept
            by nearest-distance matching between consecutive samples), stacked top/bottom.

Output: keyframes with times relative to the clip start, consumed by renderer.render_final_clip.
"""

from __future__ import annotations

import os
import urllib.request
from typing import Callable

import cv2
import numpy as np

Box = tuple[float, float, float, float]  # x, y, w, h in source pixels
Detector = Callable[[np.ndarray], list[Box]]


class CropError(RuntimeError):
    pass


def _even(v: float) -> int:
    return int(v) // 2 * 2


YUNET_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/"
             "face_detection_yunet/face_detection_yunet_2023mar.onnx")
YUNET_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "models", "face_detection_yunet_2023mar.onnx")


def _ensure_yunet_model() -> str | None:
    """Return the YuNet ONNX path, downloading it (~230 KB) on first use."""
    if os.path.exists(YUNET_PATH):
        return YUNET_PATH
    try:
        os.makedirs(os.path.dirname(YUNET_PATH), exist_ok=True)
        tmp = YUNET_PATH + ".part"
        urllib.request.urlretrieve(YUNET_URL, tmp)
        os.replace(tmp, YUNET_PATH)
        return YUNET_PATH
    except OSError:
        return None


def _make_detector() -> tuple[Detector, Callable[[], None], str]:
    try:
        import mediapipe as mp

        # model_selection=1 is the full-range model: works for faces further from the camera.
        fd = mp.solutions.face_detection.FaceDetection(model_selection=1, min_detection_confidence=0.5)

        def detect_mp(frame: np.ndarray) -> list[Box]:
            h, w = frame.shape[:2]
            result = fd.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            boxes = []
            for det in result.detections or []:
                bb = det.location_data.relative_bounding_box
                boxes.append((bb.xmin * w, bb.ymin * h, bb.width * w, bb.height * h))
            return boxes

        return detect_mp, fd.close, "mediapipe"
    except (ImportError, AttributeError):
        # mediapipe missing, or a newer build without the legacy `solutions` API.
        pass

    yunet_path = _ensure_yunet_model()
    if yunet_path and hasattr(cv2, "FaceDetectorYN"):
        yunet = cv2.FaceDetectorYN.create(yunet_path, "", (320, 320), score_threshold=0.6)

        def detect_yunet(frame: np.ndarray) -> list[Box]:
            h, w = frame.shape[:2]
            yunet.setInputSize((w, h))
            _, faces = yunet.detect(frame)
            return [] if faces is None else [tuple(map(float, f[:4])) for f in faces]

        return detect_yunet, lambda: None, "opencv-yunet"

    haar_dir = getattr(getattr(cv2, "data", None), "haarcascades", "")
    cascade = cv2.CascadeClassifier(os.path.join(haar_dir, "haarcascade_frontalface_default.xml"))
    if cascade.empty():
        raise CropError(
            "No face detector available. Install mediapipe, or place the YuNet model at "
            f"{YUNET_PATH} ({YUNET_URL})."
        )

    def detect_haar(frame: np.ndarray) -> list[Box]:
        gray = cv2.equalizeHist(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
        h, w = gray.shape
        found = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(w // 30, w // 30))
        return [tuple(map(float, f)) for f in found]

    return detect_haar, lambda: None, "opencv-haar"


def _pick_primary(boxes: list[Box], prev_cx: float | None) -> float | None:
    if not boxes:
        return None
    areas = [b[2] * b[3] for b in boxes]
    biggest = max(areas)
    contenders = [b for b, a in zip(boxes, areas) if a >= 0.6 * biggest]
    if prev_cx is None or len(contenders) == 1:
        best = boxes[int(np.argmax(areas))]
    else:
        best = min(contenders, key=lambda b: abs(b[0] + b[2] / 2 - prev_cx))
    return best[0] + best[2] / 2


def _histogram(small: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [32, 32], [0, 180, 0, 256])
    return cv2.normalize(hist, hist).flatten()


def _fill_gaps(values: list[float | None], default: float) -> np.ndarray:
    out = np.array([np.nan if v is None else v for v in values], dtype=float)
    if np.all(np.isnan(out)):
        return np.full(len(out), default)
    idx = np.arange(len(out))
    good = ~np.isnan(out)
    # Linear interpolation between detections; edges hold the nearest detection.
    return np.interp(idx, idx[good], out[good])


def _dead_zone(values: np.ndarray, zone: float) -> np.ndarray:
    held = values.copy()
    anchor = values[0]
    for i, v in enumerate(values):
        if abs(v - anchor) > zone:
            # move only as far as needed to bring the subject back to the zone edge
            anchor = v - np.sign(v - anchor) * zone
        held[i] = anchor
    return held


def _moving_average(values: np.ndarray, window: int) -> np.ndarray:
    if window <= 1 or len(values) < 3:
        return values
    window = min(window, len(values) if len(values) % 2 else len(values) - 1)
    pad = window // 2
    padded = np.pad(values, pad, mode="edge")
    return np.convolve(padded, np.ones(window) / window, mode="valid")


def scan_faces(
    video_path: str,
    start_time: float,
    end_time: float,
    sample_fps: float = 5.0,
    cut_threshold: float = 0.55,
    detect_width: int = 960,
) -> dict:
    """Decode the clip once and record every detected face per sampled frame.

    detect_width: frames are downscaled to this width for detection. 960 px keeps faces in wide
    two-person shots (~40 px tall at 1080p) above YuNet's reliable minimum size.

    Returns {"width", "height", "fps", "detector", "times": [s from clip start],
             "cuts": [bool], "boxes": [[(x, y, w, h), ...] per sample]}.
    Both the single-speaker and the split-screen layouts are computed from this one scan.
    """
    if end_time <= start_time:
        raise CropError("end_time must be greater than start_time")

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise CropError(f"OpenCV could not open {video_path}")
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if width <= 0 or height <= 0:
            raise CropError("Could not read the video resolution.")

        detect, close_detector, detector_name = _make_detector()
        scale = min(1.0, detect_width / width)  # detect on a downscaled frame for speed
        frame_step = max(1, round(fps / sample_fps))
        first_frame = int(round(start_time * fps))
        last_frame = int(round(end_time * fps))
        cap.set(cv2.CAP_PROP_POS_FRAMES, first_frame)

        times: list[float] = []
        cuts: list[bool] = []
        boxes_per_sample: list[list[Box]] = []
        prev_hist = None
        try:
            for n in range(last_frame - first_frame + 1):
                if not cap.grab():
                    break
                if n % frame_step:
                    continue
                ok, frame = cap.retrieve()
                if not ok:
                    continue
                small = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else frame

                hist = _histogram(small)
                is_cut = prev_hist is not None and cv2.compareHist(prev_hist, hist, cv2.HISTCMP_CORREL) < cut_threshold
                prev_hist = hist

                boxes_per_sample.append([(x / scale, y / scale, w / scale, h / scale) for x, y, w, h in detect(small)])
                times.append(n / fps)
                cuts.append(bool(is_cut))
        finally:
            close_detector()
    finally:
        cap.release()

    if not times:
        raise CropError("No frames could be decoded for this clip interval.")
    return {"width": width, "height": height, "fps": fps, "detector": detector_name,
            "sample_fps": sample_fps, "times": times, "cuts": cuts, "boxes": boxes_per_sample}


def _shots(cuts: list[bool]) -> list[tuple[int, int]]:
    starts = [0] + [i for i, c in enumerate(cuts) if c] + [len(cuts)]
    return [(a, b) for a, b in zip(starts[:-1], starts[1:]) if b > a]


def _smooth_per_shot(raw: list[float | None], cuts: list[bool], default: float, dead_zone: float,
                     window: int, defaults_per_shot: list[float] | None = None) -> np.ndarray:
    """Gap-fill, dead-zone and moving-average a coordinate track, independently per shot."""
    out = np.empty(len(raw))
    for k, (a, b) in enumerate(_shots(cuts)):
        d = defaults_per_shot[k] if defaults_per_shot else default
        shot = _fill_gaps(raw[a:b], default=d)
        shot = _dead_zone(shot, dead_zone)
        out[a:b] = _moving_average(shot, window)
    return out


def _smoothing_window(smoothing_window_s: float, sample_fps: float) -> int:
    return max(1, int(round(smoothing_window_s * sample_fps)) | 1)


# ---------------------------------------------------------------- single speaker (9:16)

def single_keyframes_from_scan(
    scan: dict,
    smoothing_window_s: float = 1.2,
    dead_zone_ratio: float = 0.04,
    target_aspect: float = 9 / 16,
    max_faces: int = 4,
) -> list:
    width, height = scan["width"], scan["height"]
    # Crop window: full height, 9:16 width. Already-vertical sources get a centred vertical crop.
    if width / height > target_aspect:
        crop_w, crop_h = _even(height * target_aspect), _even(height)
    else:
        crop_w, crop_h = _even(width), _even(min(height, width / target_aspect))
    crop_y = _even((height - crop_h) / 2)
    if crop_w >= width - 1:
        return [{"time": 0.0, "x": 0, "y": crop_y, "w": crop_w, "h": crop_h, "cut": False}]

    raw: list[float | None] = []
    prev_cx: float | None = None
    for boxes, is_cut in zip(scan["boxes"], scan["cuts"]):
        if is_cut:
            prev_cx = None
        if len(boxes) > max_faces:  # audience/crowd cutaway: hold position instead of chasing a stranger
            boxes = []
        cx = _pick_primary(boxes, prev_cx)
        if cx is not None:
            prev_cx = cx
        raw.append(cx)

    centres = _smooth_per_shot(raw, scan["cuts"], width / 2, dead_zone_ratio * width,
                               _smoothing_window(smoothing_window_s, scan["sample_fps"]))
    half = crop_w / 2
    keyframes = []
    for t, cx, cut in zip(scan["times"], centres, scan["cuts"]):
        cx = float(np.clip(cx, half, width - half))
        keyframes.append({"time": round(t, 3), "x": _even(cx - half), "y": crop_y,
                          "w": crop_w, "h": crop_h, "cut": bool(cut)})
    return keyframes


def calculate_crop_keyframes(
    video_path: str,
    start_time: float,
    end_time: float,
    sample_fps: float = 5.0,
    smoothing_window_s: float = 1.2,
    dead_zone_ratio: float = 0.04,
    cut_threshold: float = 0.55,
    target_aspect: float = 9 / 16,
) -> list:
    """Return 9:16 crop keyframes for the clip [start_time, end_time] of `video_path`.

    Each keyframe: {"time": seconds from clip start, "x": int, "y": int, "w": int, "h": int,
                    "cut": bool (True on the first sample after a scene cut)}
    The crop window size is constant for the whole clip; only x moves.
    """
    scan = scan_faces(video_path, start_time, end_time, sample_fps, cut_threshold)
    return single_keyframes_from_scan(scan, smoothing_window_s, dead_zone_ratio, target_aspect)


# ---------------------------------------------------------------- two speakers (split screen)

def track_two_faces(scan: dict, min_relative_area: float = 0.25, max_faces: int = 4,
                    gate_ratio: float = 0.2) -> list[dict]:
    """Assign detections to two stable identities, Face_1 and Face_2, across consecutive samples.

    - Known tracks continue with the nearest qualifying face within `gate_ratio * width`
      (greedy minimum-distance matching), so a bigger face elsewhere cannot steal an identity.
    - Faces not matched to a track seed an empty one; when both tracks are empty (clip start or
      after a scene cut) the two largest faces seed them left-to-right: Face_1 = left speaker.
    - Frames with more than `max_faces` faces (audience / crowd cutaways) are ignored and the
      tracks simply hold, instead of latching onto random audience members.
    Returns one record per sample:
        {"time", "cut", "face_1_x", "face_1_y", "face_1_h", "face_2_x", "face_2_y", "face_2_h"}
    with None where a face is not visible.
    """
    width = scan["width"]
    gate = gate_ratio * width
    last: list[tuple[float, float, float] | None] = [None, None]
    samples = []
    dist = lambda f, ref: abs(f[0] - ref[0]) + 0.5 * abs(f[1] - ref[1])  # noqa: E731
    for t, is_cut, boxes in zip(scan["times"], scan["cuts"], scan["boxes"]):
        if is_cut:
            last = [None, None]
        faces = []
        if boxes and len(boxes) <= max_faces:
            biggest = max(b[2] * b[3] for b in boxes)
            keep = sorted((b for b in boxes if b[2] * b[3] >= min_relative_area * biggest),
                          key=lambda b: b[2] * b[3], reverse=True)
            faces = [(b[0] + b[2] / 2, b[1] + b[3] / 2, b[3]) for b in keep]

        assigned: list[tuple[float, float, float] | None] = [None, None]
        used: set[int] = set()
        # 1) continue known tracks with their nearest face inside the gate
        pairs = sorted((dist(f, last[k]), k, i) for k in (0, 1) if last[k] is not None
                       for i, f in enumerate(faces))
        for d, k, i in pairs:
            if d <= gate and assigned[k] is None and i not in used:
                assigned[k] = faces[i]
                used.add(i)
        # 2) unmatched faces (largest first) seed tracks that have never been seen in this shot
        free = [f for i, f in enumerate(faces) if i not in used][:2]
        empty = [k for k in (0, 1) if last[k] is None and assigned[k] is None]
        if free and len(empty) == 2:
            seeds = sorted(free, key=lambda f: f[0])
            if len(seeds) == 2:
                assigned[0], assigned[1] = seeds
            else:
                assigned[0 if seeds[0][0] < width / 2 else 1] = seeds[0]
        elif free and len(empty) == 1:
            other = last[1 - empty[0]] or assigned[1 - empty[0]]
            # only accept it as the other speaker if it is clearly elsewhere in the frame
            far = [f for f in free if other is None or abs(f[0] - other[0]) >= gate]
            if far:
                assigned[empty[0]] = far[0]

        for k in (0, 1):
            if assigned[k] is not None:
                last[k] = assigned[k]
        rec = {"time": round(t, 3), "cut": bool(is_cut)}
        for k in (0, 1):
            f = assigned[k]
            rec[f"face_{k + 1}_x"] = None if f is None else round(f[0], 1)
            rec[f"face_{k + 1}_y"] = None if f is None else round(f[1], 1)
            rec[f"face_{k + 1}_h"] = None if f is None else round(f[2], 1)
        samples.append(rec)
    return samples


def split_keyframes_from_scan(
    scan: dict,
    panel_aspect: float = 1080 / 960,
    zoom: float = 0.62,
    face_y_ratio: float = 0.42,
    smoothing_window_s: float = 1.2,
    dead_zone_ratio: float = 0.03,
) -> dict:
    """Crop keyframes for a 2-panel vertical split (each panel 1080x960 after scaling).

    zoom: panel crop height as a fraction of the source height (smaller = tighter on the face).
    face_y_ratio: where the face centre sits vertically inside its panel (0 = top).
    Returns {"face_1": [kf...], "face_2": [kf...], "samples": [...], "two_face_ratio": float}
    where kf = {"time", "x", "y", "w", "h", "cut"} like the single layout.
    """
    width, height = scan["width"], scan["height"]
    crop_h = _even(height * zoom)
    crop_w = _even(crop_h * panel_aspect)
    if crop_w > width:
        crop_w = _even(width)
        crop_h = _even(crop_w / panel_aspect)

    samples = track_two_faces(scan)
    cuts = scan["cuts"]
    window = _smoothing_window(smoothing_window_s, scan["sample_fps"])
    shots = _shots(cuts)

    tracks = {}
    for k, default_x in ((1, width * 0.3), (2, width * 0.7)):  # classic two-shot thirds
        xs = [s[f"face_{k}_x"] for s in samples]
        ys = [s[f"face_{k}_y"] for s in samples]
        cx = _smooth_per_shot(xs, cuts, default_x, dead_zone_ratio * width, window,
                              [default_x] * len(shots))
        cy = _smooth_per_shot(ys, cuts, height * 0.4, dead_zone_ratio * height, window,
                              [height * 0.4] * len(shots))
        kfs = []
        for s, x, y in zip(samples, cx, cy):
            x0 = float(np.clip(x - crop_w / 2, 0, width - crop_w))
            y0 = float(np.clip(y - face_y_ratio * crop_h, 0, height - crop_h))
            kfs.append({"time": s["time"], "x": _even(x0), "y": _even(y0),
                        "w": crop_w, "h": crop_h, "cut": s["cut"]})
        tracks[f"face_{k}"] = kfs

    both = sum(1 for s in samples if s["face_1_x"] is not None and s["face_2_x"] is not None)
    return {**tracks, "samples": samples, "two_face_ratio": round(both / len(samples), 3)}


def calculate_split_keyframes(video_path: str, start_time: float, end_time: float,
                              sample_fps: float = 5.0, cut_threshold: float = 0.55, **kwargs) -> dict:
    """Two-speaker tracking for the split-screen layout. See split_keyframes_from_scan."""
    scan = scan_faces(video_path, start_time, end_time, sample_fps, cut_threshold)
    return split_keyframes_from_scan(scan, **kwargs)
