# SPDX-License-Identifier: MIT
"""Find a library stamp and erase it — before the binarizer ever sees it.

A library stamp is the one mark on a page that is *reliably the same shape
every time*. That is what makes it findable: give the plugin one clean snippet
of the stamp and trace the zone to remove, and SIFT will find that shape again
on every other page, at whatever rotation, scale and perspective the camera
gave it.

## How it works

**The library** lives in the plugin's own data directory: one PNG per stamp,
plus the exclusion polygon you traced on it and a cached descriptor set. Open
it from *Plugins → Stamp remover → Stamp library*.

**Matching**, per page: SIFT keypoints on the page, matched against each
stamp's descriptors with Lowe's ratio test, then `estimateAffinePartial2D`
under RANSAC. Partial-affine rather than a full homography on purpose — a
stamp is a small, rigid mark, and a full homography over a handful of inliers
happily folds a stamp into a sliver. Rotation, scale and translation is the
transform a stamp actually undergoes; refusing the extra freedom refuses the
failure mode with it.

**The output is not pixels.** The matched exclusion polygon is appended to
`meta["erase"]` and nothing else happens here. The pipeline already knows what
to do with an erase mask: it rides the geometry through deskew, keystone and
dewarp, the binarizer fills it with the paper tone measured around it *before*
thresholding — so the stamp is out of Wolf's statistics, local and global —
and whitens it after; replay punches it out of the keep-mask so the final
`wolf_masked` excludes it properly.

Doing it that way is the whole reason this plugin is small. It finds; the host
removes.

## Where to put it

Right after the layout split (`PageDetector`), before deskew. Two reasons: the
page is still un-warped, which is the frame a stamp snippet was cut from; and
replay folds erase regions into its mask at the anchor, so a producer sitting
after a geometric step has its polygons skipped rather than applied to the
wrong pixels.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from aglaia.plugin_api import (
    AbstractImageProcessor, AbstractProcessorOption, ImageBuffer, PluginWindow,
    ReplayTrait, add_erase, manual_erase, option_bool, option_float,
    option_int, register_debug_renderer, register_window, to_gray,
)

SLUG = "stamp-remover"

#: Lowe's ratio. 0.75 is the classic value and it is the right default here:
#: a stamp is repetitive (rules, circles, repeated letters), so a looser ratio
#: floods the match set with ambiguous pairs and RANSAC then fits noise.
DEFAULT_RATIO = 0.75


# ── the library ───────────────────────────────────────────────────────

@dataclass
class Stamp:
    """One reference stamp: a picture, a polygon, and its descriptors."""

    stamp_id: str
    image: np.ndarray                 # grayscale snippet
    polygon: list                     # [[x, y], …] in snippet coords
    keypoints: Optional[list] = None
    descriptors: Optional[np.ndarray] = None
    label: str = ""

    @property
    def usable(self) -> bool:
        return (self.descriptors is not None and len(self.descriptors) >= 8
                and len(self.polygon) >= 3)


def library_dir(ctx) -> Path:
    d = ctx.data_dir / "stamps"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sift():
    """SIFT, or None on a build without it.

    It is patent-free and in the main OpenCV distribution since 4.4, but a
    stripped build can still lack it — and a plugin that raises on import
    takes the whole processor registry down with it."""
    try:
        return cv2.SIFT_create()
    except Exception:
        return None


def load_library(ctx) -> list[Stamp]:
    """Every stamp in the library, with descriptors computed on the way.

    Descriptors are cached beside the image: SIFT on a snippet is cheap, but
    it is not free, and this runs per page in a worker process."""
    sift = _sift()
    out: list[Stamp] = []
    d = library_dir(ctx)
    for meta_path in sorted(d.glob("*.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        img_path = d / f"{meta_path.stem}.png"
        if not img_path.is_file():
            continue
        img = cv2.imread(str(img_path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            continue
        stamp = Stamp(stamp_id=meta_path.stem, image=img,
                      polygon=list(meta.get("polygon") or []),
                      label=str(meta.get("label") or meta_path.stem))
        if sift is not None:
            cache = d / f"{meta_path.stem}.npz"
            desc = None
            if cache.is_file():
                try:
                    with np.load(str(cache)) as z:
                        desc = z["desc"]
                        pts = z["pts"]
                    stamp.keypoints = [cv2.KeyPoint(float(x), float(y), 1.0)
                                       for x, y in pts]
                except Exception:
                    desc = None
            if desc is None:
                kps, desc = sift.detectAndCompute(img, None)
                stamp.keypoints = list(kps)
                if desc is not None and len(kps):
                    try:
                        np.savez_compressed(
                            str(cache), desc=desc,
                            pts=np.array([k.pt for k in kps], np.float32))
                    except Exception:
                        pass
            stamp.descriptors = desc
        out.append(stamp)
    return out


def save_stamp(ctx, image: np.ndarray, polygon: list, label: str = "") -> str:
    """Add one stamp to the library. Returns its id."""
    import uuid
    d = library_dir(ctx)
    sid = uuid.uuid4().hex[:12]
    gray = image if image.ndim == 2 else cv2.cvtColor(image,
                                                      cv2.COLOR_BGR2GRAY)
    cv2.imwrite(str(d / f"{sid}.png"), gray)
    (d / f"{sid}.json").write_text(
        json.dumps({"polygon": [[float(x), float(y)] for x, y in polygon],
                    "label": label or "stamp"}, ensure_ascii=False),
        encoding="utf-8")
    return sid


def rename_stamp(ctx, stamp_id: str, label: str) -> None:
    """Give a stamp a name.

    Everything was called "stamp", which is fine with one and useless with
    four: the name is what the log line says when a match is found, what the
    debug view labels the mask with, and how you know which entry to remove
    when a library has an ex-libris, a date stamp and two accession marks in
    it."""
    d = library_dir(ctx)
    meta_path = d / f"{stamp_id}.json"
    if not meta_path.is_file():
        return
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return
    meta["label"] = str(label).strip() or stamp_id
    meta_path.write_text(json.dumps(meta, ensure_ascii=False),
                         encoding="utf-8")


def delete_stamp(ctx, stamp_id: str) -> None:
    d = library_dir(ctx)
    for suffix in (".png", ".json", ".npz"):
        f = d / f"{stamp_id}{suffix}"
        if f.is_file():
            f.unlink()


# ── import / export ───────────────────────────────────────────────────
#
# One JSON file carrying everything: a library is worth moving between
# machines, sharing with whoever scans the other half of a collection, and
# keeping a copy of before an experiment. A directory of PNGs and sidecars is
# not something anyone will zip by hand reliably, so the images travel inside
# the file as base64 — bigger than a zip and openable in a text editor, which
# for a handful of small snippets is the better trade.
#
# Descriptors are NOT exported: they are a cache, they are large, and they are
# recomputed on first use. An export should carry what cannot be derived.

EXPORT_VERSION = 1


def export_library(ctx, path: Path) -> int:
    """Write the whole library to one JSON file. Returns the stamp count."""
    import base64
    d = library_dir(ctx)
    stamps = []
    for meta_path in sorted(d.glob("*.json")):
        img_path = d / f"{meta_path.stem}.png"
        if not img_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        stamps.append({
            "id": meta_path.stem,
            "label": str(meta.get("label") or meta_path.stem),
            "polygon": [[float(x), float(y)]
                        for x, y in (meta.get("polygon") or [])],
            "png_base64": base64.b64encode(
                img_path.read_bytes()).decode("ascii"),
        })
    Path(path).write_text(
        json.dumps({"kind": "aglaia.stamp-library",
                    "version": EXPORT_VERSION,
                    "stamps": stamps}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    return len(stamps)


def import_library(ctx, path: Path, *, replace: bool = False) -> tuple[int, str]:
    """Read stamps from a JSON file. Returns `(imported, error)`.

    Adds by default rather than replacing: importing a colleague's library
    should not silently discard your own. `replace=True` is the explicit
    other choice.

    Every stamp is validated BEFORE anything is written — a file that is going
    to be rejected should not leave half a library behind."""
    import base64
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return 0, f"This file could not be read: {type(e).__name__}."
    if not isinstance(data, dict) or data.get("kind") != "aglaia.stamp-library":
        return 0, "This is not a stamp library file."
    if int(data.get("version") or 0) > EXPORT_VERSION:
        return 0, ("This library was written by a newer version of the "
                   "plugin. Update it and try again.")

    staged = []
    for entry in (data.get("stamps") or []):
        if not isinstance(entry, dict):
            continue
        poly = [[float(x), float(y)] for x, y in (entry.get("polygon") or [])]
        if len(poly) < 3:
            continue
        try:
            raw = base64.b64decode(entry.get("png_base64") or "", validate=True)
            img = cv2.imdecode(np.frombuffer(raw, np.uint8),
                               cv2.IMREAD_GRAYSCALE)
        except Exception:
            img = None
        if img is None or img.size == 0:
            continue
        staged.append((img, poly, str(entry.get("label") or "stamp")))
    if not staged:
        return 0, "This file contains no usable stamps."

    if replace:
        for f in library_dir(ctx).glob("*"):
            if f.suffix in (".png", ".json", ".npz"):
                f.unlink(missing_ok=True)
    for img, poly, label in staged:
        save_stamp(ctx, img, poly, label)
    return len(staged), ""


# ── matching ──────────────────────────────────────────────────────────

def find_stamp(page_gray: np.ndarray, stamp: Stamp, *, ratio: float,
               min_inliers: int) -> tuple[Optional[list], int]:
    """The stamp's exclusion polygon in PAGE coordinates, and the inlier count.

    `estimateAffinePartial2D`, not `findHomography`: a stamp is a small rigid
    mark, and the transform it really undergoes is rotation + uniform scale +
    translation. A full homography has eight degrees of freedom to spend on a
    handful of inliers, and it spends them — folding a stamp into a sliver
    that then erases a strip of text. Refusing the freedom refuses the failure.
    """
    sift = _sift()
    if sift is None or not stamp.usable:
        return None, 0
    kp_page, desc_page = sift.detectAndCompute(page_gray, None)
    if desc_page is None or len(desc_page) < min_inliers:
        return None, 0

    matcher = cv2.BFMatcher()
    try:
        pairs = matcher.knnMatch(stamp.descriptors, desc_page, k=2)
    except cv2.error:
        return None, 0
    good = [m for m, n in (p for p in pairs if len(p) == 2)
            if m.distance < ratio * n.distance]
    if len(good) < min_inliers:
        return None, len(good)

    src = np.float32([stamp.keypoints[m.queryIdx].pt
                      for m in good]).reshape(-1, 1, 2)
    dst = np.float32([kp_page[m.trainIdx].pt
                      for m in good]).reshape(-1, 1, 2)
    M, inliers = cv2.estimateAffinePartial2D(
        src, dst, method=cv2.RANSAC, ransacReprojThreshold=4.0,
        maxIters=4000, confidence=0.995)
    n_in = int(inliers.sum()) if inliers is not None else 0
    if M is None or n_in < min_inliers:
        return None, n_in

    poly = np.float32(stamp.polygon).reshape(-1, 1, 2)
    return cv2.transform(poly, M).reshape(-1, 2).tolist(), n_in


def expand_polygon(poly, px: float, shape) -> list:
    """`poly` grown outward by `px` pixels, as a polygon.

    Done by dilating a mask rather than by offsetting the vertices from a
    centroid: a centroid offset is only correct for a convex shape, and it
    turns a concave one inside out — which is exactly what a hand-traced
    stamp outline is once someone has clipped a corner.

    Returns the input unchanged if there is nothing to do, or if the dilation
    somehow yields no contour: a margin that fails is not worth losing the
    mask over.
    """
    if px <= 0.5 or len(poly) < 3:
        return list(poly)
    h, w = int(shape[0]), int(shape[1])
    pts = np.int32(poly)
    # Work in a padded local window, not the whole page: the page can be
    # 2000x3000 and the stamp 250x250, and the mask is allocated per call.
    pad = int(px) + 4
    x0 = max(0, int(pts[:, 0].min()) - pad)
    y0 = max(0, int(pts[:, 1].min()) - pad)
    x1 = min(w, int(pts[:, 0].max()) + pad)
    y1 = min(h, int(pts[:, 1].max()) + pad)
    if x1 <= x0 or y1 <= y0:
        return list(poly)
    mask = np.zeros((y1 - y0, x1 - x0), np.uint8)
    cv2.fillPoly(mask, [pts - [x0, y0]], 255)
    k = int(px) * 2 + 1
    mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                                      (k, k)))
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return list(poly)
    big = max(cnts, key=cv2.contourArea)
    # Simplify. A traced contour follows the mask pixel by pixel — a 1 mm
    # margin on a 240 px stamp came back with 38 vertices and a 3 mm one with
    # 98, which is a fringe of overlapping drag handles rather than a shape
    # anyone can edit. The tolerance is a fraction of the perimeter, so it
    # scales with the stamp instead of being a pixel count that means
    # something different at every DPI.
    eps = max(1.0, _SIMPLIFY_FRAC * cv2.arcLength(big, True))
    big = cv2.approxPolyDP(big, eps, True)
    pts = big.reshape(-1, 2) + [x0, y0]
    if len(pts) < 3:
        return list(poly)
    return [[float(x), float(y)] for x, y in pts]


#: Douglas-Peucker tolerance, as a fraction of the polygon's perimeter. 1%
#: keeps a circle recognisably round (~20 vertices) while dropping the
#: pixel-staircase between them.
_SIMPLIFY_FRAC = 0.01


# ── the processor ─────────────────────────────────────────────────────

@dataclass
class StampRemoverOption(AbstractProcessorOption):
    ratio: float = DEFAULT_RATIO
    min_inliers: int = 12
    max_area_frac: float = 0.25
    margin_mm: float = 1.0
    enabled: bool = True


class StampRemover(AbstractImageProcessor):
    name = "StampRemover"
    SUMMARY = ("Find library stamps by SIFT and mark them for erasure "
               "(meta.erase). Changes no pixels.")
    OPTION_CLASS = StampRemoverOption
    # It does not move coordinates and does not change pixel values — but a
    # trait is required, and PIXEL_VALUE is the honest one: it contributes to
    # what the binarizer produces. Replay does not re-run it; it folds the
    # erase regions into its keep-mask at the anchor instead.
    REPLAY_TRAIT = ReplayTrait.PIXEL_VALUE
    PROVIDES_META = {
        "erase": "polygons to remove, appended for the Binarizer",
        "stamps_found": "how many library stamps matched this page",
    }
    OPTIONS = {
        "ratio": option_float(
            DEFAULT_RATIO, 0.5, 0.95, 0.05,
            "Lowe's ratio test. Lower is stricter. A stamp is repetitive "
            "(rules, circles, repeated letters), so a loose ratio floods the "
            "match set with ambiguous pairs and RANSAC then fits noise."),
        "min_inliers": option_int(
            12, 4, 200,
            "Matches that must survive RANSAC before a stamp counts as found. "
            "The single knob between 'missed it' and 'erased a paragraph'."),
        "max_area_frac": option_float(
            0.25, 0.01, 1.0, 0.05,
            "Refuse a match whose polygon covers more than this fraction of "
            "the page. A degenerate fit is large before it is anything else, "
            "so this catches one before it erases the text.", advanced=True),
        "margin_mm": option_float(
            1.0, 0.0, 10.0, 0.5,
            "Widen the removal by this much all round, in millimetres. A "
            "stamp is rarely as crisp as the snippet it was traced from — it "
            "is inked heavier on one page, printed at a slight angle on "
            "another — and the last fraction of a millimetre of its rim is "
            "what the binariser turns into a ring of specks. In millimetres "
            "rather than pixels so it means the same thing at every DPI."),
        "enabled": option_bool(
            True, "Off: the step passes through, so it can be disabled per "
                  "page from the scan views like any other."),
    }

    ctx = None      # set by the host after construction

    def __init__(self, options: StampRemoverOption):
        super().__init__(options)
        self.opt = options
        self._library: Optional[list] = None
        self.uses_gpu = False

    def _stamps(self) -> list:
        if self._library is None:
            self._library = load_library(self.ctx) if self.ctx else []
        return self._library

    def process(self, buf: ImageBuffer) -> ImageBuffer:
        if not self.opt.enabled:
            return buf

        gray = to_gray(buf.buffer)
        h, w = gray.shape[:2]

        # A hand-edited set REPLACES detection for this page. That is what
        # makes an edit stick: a region the user deleted must not be found
        # again on the next run, and one they drew must survive it. An empty
        # list is a decision too — it means "nothing here" — so it is
        # honoured, while None means "no override, decide for yourself".
        manual = manual_erase(buf.meta, frame_wh=(w, h))
        if manual is not None:
            for poly in manual:
                self._add_erase(buf, poly, "manual")
            buf.meta["stamps_found"] = 0
            self._log(f"{len(manual)} region(s) set by hand; detection skipped")
            return buf

        stamps = self._stamps()
        if not stamps:
            return buf
        page_area = float(h * w)
        found = 0
        for stamp in stamps:
            try:
                poly, n_in = find_stamp(
                    gray, stamp, ratio=float(self.opt.ratio),
                    min_inliers=int(self.opt.min_inliers))
            except Exception as e:  # noqa: BLE001 — one bad stamp, not a dead page
                self._log(f"{stamp.label}: {type(e).__name__}: {e}")
                continue
            if poly is None:
                continue
            area = abs(cv2.contourArea(np.float32(poly)))
            if area > page_area * float(self.opt.max_area_frac):
                # A degenerate fit is large before it is anything else.
                self._log(f"{stamp.label}: match covers "
                          f"{area / page_area:.0%} of the page — refused")
                continue
            self._add_erase(buf, poly, stamp.label)
            found += 1
            self._log(f"{stamp.label}: found, {n_in} inliers")

        buf.meta["stamps_found"] = found
        if found:
            buf.meta.setdefault("manual", [])
        return buf

    # -- host plumbing, kept behind one method each so the pipeline API can
    #    move without touching the matching code above.
    def _add_erase(self, buf, poly, label: str) -> None:
        # The margin belongs to the REMOVAL, not to the detection, so it is
        # applied here — to a hand-drawn region as much as to a matched one.
        # Someone tracing a polygon by eye leaves the same sliver of rim
        # behind that a slightly-off match does.
        mm = float(getattr(self.opt, "margin_mm", 0.0) or 0.0)
        dpi = float(getattr(buf, "dpi", 0) or 0)
        if mm > 0 and dpi > 0:
            poly = expand_polygon(poly, mm / 25.4 * dpi, buf.buffer.shape[:2])
        add_erase(buf.meta, poly, source=f"stamp:{label}")

    def _log(self, msg: str) -> None:
        if self.ctx is not None and self.ctx.log:
            self.ctx.log(msg)


# ── the library window ────────────────────────────────────────────────

def _make_window(ctx):
    """Build the library window.

    Qt is imported HERE, not at module scope. This module is imported by every
    spawned worker process to get the processor, and a worker has no display —
    importing PySide6 there costs seconds and a warning per worker, for a
    window it will never show."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QImage, QPainter, QPen, QPixmap, QPolygonF, QColor
    from PySide6.QtWidgets import (
        QFileDialog, QHBoxLayout, QInputDialog, QLabel, QListWidget,
        QListWidgetItem,
        QMessageBox, QPushButton, QVBoxLayout, QWidget)

    class PolygonCanvas(QLabel):
        """Show a stamp snippet and let the user trace the zone to erase.

        Click to drop a point, double-click (or the button) to close the
        polygon. Deliberately not draggable-after-the-fact: tracing a stamp
        outline is a ten-second job, and Undo + Clear cover the mistakes
        without a vertex-editing mode nobody would learn."""

        def __init__(self) -> None:
            super().__init__()
            self.setMinimumSize(360, 360)
            self.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.setStyleSheet("background: #111; border: 1px solid #333;")
            self._img: Optional[np.ndarray] = None
            self._pix: Optional[QPixmap] = None
            self.points: list = []

        def set_image(self, gray: np.ndarray) -> None:
            self._img = gray
            h, w = gray.shape[:2]
            qim = QImage(gray.data, w, h, w, QImage.Format.Format_Grayscale8)
            self._pix = QPixmap.fromImage(qim.copy())
            self.points = []
            self.update()

        def _fit(self):
            if self._pix is None:
                return None
            sz = self._pix.size().scaled(self.size(),
                                         Qt.AspectRatioMode.KeepAspectRatio)
            x = (self.width() - sz.width()) // 2
            y = (self.height() - sz.height()) // 2
            return x, y, sz.width(), sz.height()

        def _to_img(self, pos) -> Optional[tuple]:
            fit = self._fit()
            if fit is None or self._img is None:
                return None
            x, y, w, h = fit
            if not (x <= pos.x() <= x + w and y <= pos.y() <= y + h) or not w:
                return None
            ih, iw = self._img.shape[:2]
            return ((pos.x() - x) * iw / w, (pos.y() - y) * ih / h)

        def _to_view(self, px, py):
            fit = self._fit()
            ih, iw = self._img.shape[:2]
            x, y, w, h = fit
            return QPointF(x + px * w / iw, y + py * h / ih)

        def mousePressEvent(self, ev):  # noqa: N802
            p = self._to_img(ev.position().toPoint())
            if p is not None:
                self.points.append([p[0], p[1]])
                self.update()

        def undo(self):
            if self.points:
                self.points.pop()
                self.update()

        def clear(self):
            self.points = []
            self.update()

        def paintEvent(self, ev):  # noqa: N802
            super().paintEvent(ev)
            if self._pix is None:
                return
            fit = self._fit()
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            x, y, w, h = fit
            p.drawPixmap(x, y, self._pix.scaled(
                w, h, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
            if self.points:
                pts = [self._to_view(*q) for q in self.points]
                p.setPen(QPen(QColor(255, 90, 200), 2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                if len(pts) >= 3:
                    p.drawPolygon(QPolygonF(pts))
                elif len(pts) == 2:
                    p.drawLine(pts[0], pts[1])
                p.setBrush(QColor(255, 255, 255))
                for q in pts:
                    p.drawEllipse(q, 4, 4)
            p.end()

    class StampLibraryWindow(QWidget):
        """The library: what is in it, and the tracing pane for adding one."""

        def __init__(self, ctx) -> None:
            super().__init__()
            self.ctx = ctx
            self.setWindowTitle("Stamp library")
            root = QHBoxLayout(self)

            left = QVBoxLayout()
            left.addWidget(QLabel("<b>Stamps</b>"))
            self.list = QListWidget()
            self.list.setMinimumWidth(200)
            left.addWidget(self.list, 1)
            row = QHBoxLayout()
            add_btn = QPushButton("Add from image…")
            add_btn.clicked.connect(self._open_image)
            row.addWidget(add_btn)
            del_btn = QPushButton("Remove")
            del_btn.clicked.connect(self._delete)
            row.addWidget(del_btn)
            left.addLayout(row)
            row2 = QHBoxLayout()
            ren_btn = QPushButton("Rename…")
            ren_btn.clicked.connect(self._rename)
            row2.addWidget(ren_btn)
            imp_btn = QPushButton("Import…")
            imp_btn.clicked.connect(self._import)
            row2.addWidget(imp_btn)
            exp_btn = QPushButton("Export…")
            exp_btn.clicked.connect(self._export)
            row2.addWidget(exp_btn)
            left.addLayout(row2)
            root.addLayout(left)

            right = QVBoxLayout()
            self.hint = QLabel(
                "Open a snippet containing the stamp, then click around the "
                "zone to erase. Trace a little OUTSIDE the ink: the binarizer "
                "fills the polygon with the surrounding paper tone, and a "
                "polygon that clips the stamp leaves its edge behind.")
            self.hint.setWordWrap(True)
            self.hint.setStyleSheet("color: #9aa; font-size: 11px;")
            right.addWidget(self.hint)
            self.canvas = PolygonCanvas()
            right.addWidget(self.canvas, 1)
            trow = QHBoxLayout()
            for label, fn in (("Undo point", self.canvas.undo),
                              ("Clear", self.canvas.clear)):
                b = QPushButton(label)
                b.clicked.connect(fn)
                trow.addWidget(b)
            trow.addStretch(1)
            self.save_btn = QPushButton("Save to library")
            self.save_btn.clicked.connect(self._save)
            trow.addWidget(self.save_btn)
            right.addLayout(trow)
            root.addLayout(right, 1)

            self._pending: Optional[np.ndarray] = None
            self.refresh()

        def refresh(self) -> None:
            self.list.clear()
            for st in load_library(self.ctx):
                item = QListWidgetItem(
                    f"{st.label}  ({len(st.polygon)} pts"
                    + (f", {len(st.descriptors)} features)"
                       if st.descriptors is not None else ", no features)"))
                item.setData(Qt.ItemDataRole.UserRole, st.stamp_id)
                self.list.addItem(item)

        def _open_image(self) -> None:
            path, _ = QFileDialog.getOpenFileName(
                self, "A snippet showing the stamp", "",
                "Images (*.png *.jpg *.jpeg *.tif *.tiff)")
            if not path:
                return
            img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
            if img is None:
                QMessageBox.warning(self, "Stamp library",
                                    f"Could not read {path}")
                return
            self._pending = img
            self.canvas.set_image(img)

        def _save(self) -> None:
            if self._pending is None:
                QMessageBox.information(self, "Stamp library",
                                        "Open a snippet first.")
                return
            if len(self.canvas.points) < 3:
                QMessageBox.information(
                    self, "Stamp library",
                    "Trace the zone to erase — at least three points. The "
                    "stamp is what gets FOUND; this polygon is what gets "
                    "REMOVED, and they are not the same thing: trace a little "
                    "outside the ink.")
                return
            sift = _sift()
            if sift is not None:
                kps, desc = sift.detectAndCompute(self._pending, None)
                if desc is None or len(desc) < 8:
                    QMessageBox.warning(
                        self, "Stamp library",
                        f"Only {0 if desc is None else len(desc)} features in "
                        f"this snippet — too few to match reliably. Use a "
                        f"larger or sharper crop of the stamp.")
                    return
            # Named at the moment it is created, when the user is looking at
            # it and knows what it is. Asking later never happens, and a
            # library of four things all called "stamp" is a library you
            # cannot prune.
            label, ok = QInputDialog.getText(
                self, "Name this stamp",
                "A name you will recognise later — it appears in the log and "
                "on the debug view:", text="stamp")
            if not ok:
                return
            save_stamp(self.ctx, self._pending, self.canvas.points,
                       label.strip() or "stamp")
            self._pending = None
            self.canvas.clear()
            self.refresh()

        def _delete(self) -> None:
            item = self.list.currentItem()
            if item is None:
                return
            delete_stamp(self.ctx, item.data(Qt.ItemDataRole.UserRole))
            self.refresh()

        def _rename(self) -> None:
            item = self.list.currentItem()
            if item is None:
                QMessageBox.information(self, "Stamp library",
                                        "Select a stamp first.")
                return
            sid = item.data(Qt.ItemDataRole.UserRole)
            current = next((st.label for st in load_library(self.ctx)
                            if st.stamp_id == sid), "")
            label, ok = QInputDialog.getText(self, "Rename stamp",
                                             "Name:", text=current)
            if ok and label.strip():
                rename_stamp(self.ctx, sid, label)
                self.refresh()

        def _export(self) -> None:
            path, _ = QFileDialog.getSaveFileName(
                self, "Export stamp library", "stamps.json",
                "Stamp library (*.json)")
            if not path:
                return
            try:
                n = export_library(self.ctx, Path(path))
            except OSError as e:
                QMessageBox.warning(self, "Stamp library",
                                    f"Could not write the file: {e.strerror}.")
                return
            QMessageBox.information(
                self, "Stamp library",
                f"{n} stamp(s) exported." if n
                else "The library is empty — nothing was exported.")

        def _import(self) -> None:
            path, _ = QFileDialog.getOpenFileName(
                self, "Import stamp library", "", "Stamp library (*.json)")
            if not path:
                return
            replace = False
            if load_library(self.ctx):
                # Adding is the default because it cannot lose anything;
                # replacing is offered, and named for what it does.
                box = QMessageBox(self)
                box.setWindowTitle("Import stamp library")
                box.setText("You already have stamps in this library.")
                add = box.addButton("Add to it", QMessageBox.ButtonRole.AcceptRole)
                rep = box.addButton("Replace it", QMessageBox.ButtonRole.DestructiveRole)
                box.addButton(QMessageBox.StandardButton.Cancel)
                box.setDefaultButton(add)
                box.exec()
                if box.clickedButton() is rep:
                    replace = True
                elif box.clickedButton() is not add:
                    return
            n, err = import_library(self.ctx, Path(path), replace=replace)
            if err:
                QMessageBox.warning(self, "Stamp library", err)
                return
            self.refresh()
            QMessageBox.information(self, "Stamp library",
                                    f"{n} stamp(s) imported.")

    return StampLibraryWindow(ctx)


# ── the debug pane ────────────────────────────────────────────────────

def _debug_renderer(img, parent, meta):
    """What the matcher saw and what it decided.

    Three things, in the order they matter. The erase polygons, filled and
    outlined, because that is the output. The SIFT keypoints, as small
    crosshairs, because when a stamp is NOT found the question is always
    whether there were features to match at all — a blurred or over-exposed
    crop gives twenty, a good one gives three hundred, and that is visible at
    a glance and invisible any other way. Then a one-line verdict.

    Keypoints are recomputed here rather than carried in meta: they are large,
    they would ride the whole pipeline in every node's JSON, and the debug
    view is the only thing that ever wants them.
    """
    from aglaia.plugin_api import debug_pane, to_gray

    canvas = img if img.ndim == 3 else cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    canvas = canvas.copy()
    gray = to_gray(img)
    h, w = gray.shape[:2]

    sift = _sift()
    n_kp = 0
    if sift is not None:
        try:
            kps = list(sift.detect(gray, None))
            # Report the TRUE count and draw a capped subset: the number is
            # what tells you whether this page has anything to match on, and
            # capping it silently would have reported 400 for every page with
            # more than 400. Strongest first, so the ones drawn are the ones
            # that carry the match — detection order is arbitrary.
            n_kp = len(kps)
            kps.sort(key=lambda k: -k.response)
            for kp in kps[:_DEBUG_MAX_KEYPOINTS]:
                x, y = int(kp.pt[0]), int(kp.pt[1])
                r = max(3, int(kp.size / 4))
                cv2.line(canvas, (x - r, y), (x + r, y), (90, 190, 255), 1,
                         cv2.LINE_AA)
                cv2.line(canvas, (x, y - r), (x, y + r), (90, 190, 255), 1,
                         cv2.LINE_AA)
        except cv2.error:
            pass

    polys = [e["polygon"] if isinstance(e, dict) else e
             for e in (meta.get("erase") or [])]
    if polys:
        # Filled at low alpha so the ink underneath stays readable — the
        # question the user is answering is whether the polygon covers the
        # stamp AND clears its edge, and a solid fill hides exactly that.
        overlay = canvas.copy()
        for poly in polys:
            cv2.fillPoly(overlay, [np.int32(poly)], (60, 90, 230))
        canvas = cv2.addWeighted(overlay, 0.28, canvas, 0.72, 0)
        for poly in polys:
            cv2.polylines(canvas, [np.int32(poly)], True, (60, 90, 230), 2,
                          cv2.LINE_AA)

    found = int(meta.get("stamps_found") or 0)
    if found:
        verdict = f"{found} stamp(s) found - {n_kp} keypoints on this page"
    elif polys:
        verdict = f"{len(polys)} region(s) set by hand - {n_kp} keypoints"
    else:
        verdict = f"no stamp found - {n_kp} keypoints on this page"
    cv2.rectangle(canvas, (0, 0), (w, 26), (20, 20, 20), -1)
    cv2.putText(canvas, verdict, (8, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                (235, 235, 235), 1, cv2.LINE_AA)

    # `erase=` makes the regions editable: a trash badge on each, an add
    # badge in the corner, stored as a manual override for this page.
    return [debug_pane(canvas, "stamps", erase=polys, frame_wh=(w, h))]


#: How many crosshairs to draw. The COUNT in the verdict is always the true
#: one; this only caps the drawing, past which it is a fog that hides the page.
_DEBUG_MAX_KEYPOINTS = 400

register_debug_renderer("StampRemover", _debug_renderer)


register_window(SLUG, PluginWindow(
    key="stamp-library", title="Stamp library", factory=_make_window,
    summary="Add a stamp snippet and trace the zone to erase."))
