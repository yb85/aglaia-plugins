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
    ReplayTrait, add_erase, option_bool, option_float, option_int,
    register_window, to_gray,
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


def delete_stamp(ctx, stamp_id: str) -> None:
    d = library_dir(ctx)
    for suffix in (".png", ".json", ".npz"):
        f = d / f"{stamp_id}{suffix}"
        if f.is_file():
            f.unlink()


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


# ── the processor ─────────────────────────────────────────────────────

@dataclass
class StampRemoverOption(AbstractProcessorOption):
    ratio: float = DEFAULT_RATIO
    min_inliers: int = 12
    max_area_frac: float = 0.25
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
        stamps = self._stamps()
        if not stamps:
            return buf

        gray = to_gray(buf.buffer)
        h, w = gray.shape[:2]
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
        QFileDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
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
            save_stamp(self.ctx, self._pending, self.canvas.points)
            self._pending = None
            self.canvas.clear()
            self.refresh()

        def _delete(self) -> None:
            item = self.list.currentItem()
            if item is None:
                return
            delete_stamp(self.ctx, item.data(Qt.ItemDataRole.UserRole))
            self.refresh()

    return StampLibraryWindow(ctx)


register_window(SLUG, PluginWindow(
    key="stamp-library", title="Stamp library", factory=_make_window,
    summary="Add a stamp snippet and trace the zone to erase."))
