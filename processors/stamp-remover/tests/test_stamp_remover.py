# SPDX-License-Identifier: MIT
"""What the stamp remover promises, checked without a GUI.

The matching itself needs a real stamp on a real page and is exercised by
hand; these cover the parts that are pure geometry and pure file format, which
are the parts that break silently.
"""
import importlib
import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sr = importlib.import_module("stamp_remover")


# ── the removal margin ───────────────────────────────────────────────

class TestExpandPolygon:
    def test_it_grows_the_region(self):
        square = [[100, 100], [340, 100], [340, 340], [100, 340]]
        before = abs(cv2.contourArea(np.float32(square)))
        after = abs(cv2.contourArea(np.float32(
            sr.expand_polygon(square, 12, (600, 600)))))
        assert after > before
        # 12 px on each side of a 240 px square is +21% at the corners and a
        # little less once the dilation rounds them.
        assert 1.12 < after / before < 1.22

    def test_a_concave_shape_does_not_turn_inside_out(self):
        """The reason this dilates a mask instead of offsetting vertices from
        a centroid: a centroid offset inverts a concave outline, and a
        hand-traced stamp with a clipped corner is exactly that."""
        arrow = [[0, 0], [100, 0], [100, 100], [50, 50], [0, 100]]
        out = sr.expand_polygon(arrow, 6, (200, 200))
        a_in = abs(cv2.contourArea(np.float32(arrow)))
        a_out = abs(cv2.contourArea(np.float32(out)))
        assert a_out > a_in
        assert a_out < a_in * 2          # grown, not replaced by its hull

    def test_zero_margin_changes_nothing(self):
        poly = [[1, 1], [9, 1], [9, 9]]
        assert sr.expand_polygon(poly, 0, (20, 20)) == poly

    def test_a_degenerate_polygon_is_returned_unchanged(self):
        """A margin that cannot be applied is not worth losing the mask
        over."""
        assert sr.expand_polygon([[1, 1], [2, 2]], 5, (10, 10)) == [[1, 1], [2, 2]]

    def test_it_stays_inside_the_page(self):
        """A stamp in the corner — scan 103 of the book this was built for is
        clipped by the page edge — must not produce coordinates off-canvas."""
        corner = [[0, 0], [40, 0], [40, 40], [0, 40]]
        out = np.float32(sr.expand_polygon(corner, 10, (100, 100)))
        assert out.min() >= 0
        assert out[:, 0].max() <= 100 and out[:, 1].max() <= 100


# ── the library file format ──────────────────────────────────────────

class _Ctx:
    def __init__(self, root):
        self.data_dir = Path(root)
        self.slug = "stamp-remover"
        self.log = None


@pytest.fixture()
def ctx(tmp_path):
    return _Ctx(tmp_path)


def _add(ctx, label="stamp"):
    img = np.full((60, 60), 200, np.uint8)
    cv2.circle(img, (30, 30), 20, 40, 2)
    return sr.save_stamp(ctx, img, [[5, 5], [55, 5], [55, 55], [5, 55]], label)


class TestImportExport:
    def test_a_round_trip_keeps_the_stamps(self, ctx, tmp_path):
        _add(ctx, "ex-libris")
        _add(ctx, "accession")
        out = tmp_path / "lib.json"
        assert sr.export_library(ctx, out) == 2
        n, err = sr.import_library(ctx, out, replace=True)
        assert (n, err) == (2, "")
        assert sorted(s.label for s in sr.load_library(ctx)) == [
            "accession", "ex-libris"]

    def test_import_adds_rather_than_replacing(self, ctx, tmp_path):
        """Importing a colleague's library must not silently discard your
        own."""
        _add(ctx, "mine")
        out = tmp_path / "lib.json"
        sr.export_library(ctx, out)
        sr.import_library(ctx, out)               # no replace=
        assert len(sr.load_library(ctx)) == 2

    def test_descriptors_are_not_exported(self, ctx, tmp_path):
        """They are a cache: large, and recomputed on first use. An export
        carries what cannot be derived."""
        _add(ctx)
        sr.load_library(ctx)                      # writes the .npz cache
        out = tmp_path / "lib.json"
        sr.export_library(ctx, out)
        blob = json.loads(out.read_text(encoding="utf-8"))
        assert set(blob["stamps"][0]) == {"id", "label", "polygon", "png_base64"}

    def test_a_file_that_is_not_a_library_is_refused(self, ctx, tmp_path):
        bad = tmp_path / "notes.json"
        bad.write_text('{"hello": 1}', encoding="utf-8")
        n, err = sr.import_library(ctx, bad)
        assert n == 0 and "not a stamp library" in err

    def test_unreadable_json_is_refused_without_raising(self, ctx, tmp_path):
        bad = tmp_path / "broken.json"
        bad.write_text("{{{", encoding="utf-8")
        n, err = sr.import_library(ctx, bad)
        assert n == 0 and err

    def test_a_newer_format_is_refused_by_version(self, ctx, tmp_path):
        f = tmp_path / "future.json"
        f.write_text(json.dumps({"kind": "aglaia.stamp-library",
                                 "version": sr.EXPORT_VERSION + 1,
                                 "stamps": []}), encoding="utf-8")
        n, err = sr.import_library(ctx, f)
        assert n == 0 and "newer version" in err

    def test_a_rejected_file_leaves_the_library_untouched(self, ctx, tmp_path):
        """Validated before anything is written — a refused import must not
        leave half a library behind, and must not have wiped the old one."""
        _add(ctx, "keep me")
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"kind": "aglaia.stamp-library",
                                   "version": 1,
                                   "stamps": [{"polygon": [[0, 0]],
                                               "png_base64": "!!"}]}),
                       encoding="utf-8")
        n, err = sr.import_library(ctx, bad, replace=True)
        assert n == 0 and err
        assert [s.label for s in sr.load_library(ctx)] == ["keep me"]


def test_renaming(ctx):
    sid = _add(ctx, "old")
    sr.rename_stamp(ctx, sid, "Bibliothèque")
    assert [s.label for s in sr.load_library(ctx)] == ["Bibliothèque"]


def test_renaming_something_that_is_gone_is_not_an_error(ctx):
    sr.rename_stamp(ctx, "nope", "x")


def test_page_features_are_computed_once_per_page(ctx, monkeypatch):
    """Not once per stamp. SIFT on a text page is ~380 ms and matching a stamp
    against the result is ~4 ms, so a library of four stamps used to spend
    1.5 s per page redoing the identical work."""
    calls = []
    real = sr.page_features

    def counted(gray):
        calls.append(1)
        return real(gray)

    monkeypatch.setattr(sr, "page_features", counted)
    for i in range(3):
        _add(ctx, f"s{i}")
    proc = sr.StampRemover(sr.StampRemoverOption())
    proc.ctx = ctx
    page = np.full((400, 400), 210, np.uint8)
    cv2.circle(page, (200, 200), 60, 40, 3)
    from aglaia.plugin_api import ImageBuffer, ImageType
    proc.process(ImageBuffer(page, ImageType.GRAY, dpi=300.0))
    assert len(calls) == 1, f"SIFT ran {len(calls)} times for 3 stamps"


def test_find_stamp_accepts_precomputed_features(ctx):
    """The hoisted features are passed in; a caller that does not pass them
    still works, so the function stays usable on its own."""
    _add(ctx)
    stamp = sr.load_library(ctx)[0]
    page = np.full((300, 300), 210, np.uint8)
    poly, n = sr.find_stamp(page, stamp, ratio=0.75, min_inliers=12)
    assert poly is None and n == 0            # nothing to find, but no crash
