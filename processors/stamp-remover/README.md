# stamp-remover

Find a library stamp by SIFT and erase it before the binarizer sees it.

A library stamp is the one mark on a page that is *reliably the same shape
every time*. Give the plugin one clean snippet and trace the zone to remove,
and it will find that shape again on every other page, at whatever rotation,
scale and perspective the camera gave it.

## Using it

1. **Plugins → Stamp remover → Stamp library.** Open a snippet containing the
   stamp and click around the zone to erase. Trace a little **outside** the
   ink: the binarizer fills the polygon with the surrounding paper tone, and a
   polygon that clips the stamp leaves its edge behind.
2. Add `StampRemover` to your pipeline **right after the layout split**
   (`PageDetector`), before deskew.

```yaml
- name: stamps
  processor: StampRemover
  options:
    min_inliers: 6
    ratio: 0.75
```

## Why it is small

**It changes no pixels.** The matched polygon goes into `meta["erase"]` and
that is all. The host already knows what to do with an erase mask: carry it
through deskew, keystone and dewarp; fill it with the paper tone measured
around it *before* thresholding, so the stamp is out of Wolf's statistics both
local and global; whiten it after; and in replay punch it out of the keep-mask
so the final `wolf_masked` excludes it properly.

It finds. The host removes.

## Two choices worth knowing about

**`estimateAffinePartial2D`, not `findHomography`.** A stamp is a small rigid
mark, and the transform it really undergoes is rotation + uniform scale +
translation. A full homography has eight degrees of freedom to spend on a
handful of inliers, and it spends them — folding a stamp into a sliver that
then erases a strip of text. Refusing the freedom refuses the failure mode.

**`min_inliers` is the knob that matters.** It is the whole distance between
"missed the stamp" and "erased a paragraph". `max_area_frac` is the backstop:
a degenerate fit is large before it is anything else, so a match covering more
than a quarter of the page is refused and logged.

## Speed, measured

Finding features on the page is 95% of this step's cost, and it depends on
how many pixels the page has — not on how many stamps are in the library
(features are computed once per page, then every stamp is matched against
them: four stamps cost 154 ms where one costs 144).

So **run it at native resolution, before the DPI normalise.** A page scanned at
139 dpi and normalised to 300 has 4.6× the pixels and no new information, and
the invented pixels are worse than useless: interpolation artefacts match as
spurious keypoints, so the unstamped-page noise floor was 29 inliers at 300 dpi
and 5 at native. On the 240 pages of a real book with three stamped pages:

| where the step runs | ms / page | found | false positives |
|---|---|---|---|
| after the normalise (300 dpi, upsampled) | 752 | 3 / 3 | 0 |
| **before it (139 dpi, native)** | **57** | **3 / 3** | **0** |

Things that were tried and do NOT work, so nobody tries them again: a coarse
low-resolution pass to gate a full one (at 0.35× the stamped pages score 21/9/4
against a noise floor of 59 — downscaling destroys the fine structure the stamp
is made of); capping SIFT to its strongest N keypoints (at 800, two of the three
stamped pages score 0 — text is stronger than a stamp); normalised
cross-correlation (not rotation- or scale-invariant, which is why this uses
SIFT); ORB (2 inliers on real stamped pages); filtering keypoints by scale (the
stamp's keypoints have the same size distribution as text).

**`detector`**: `sift` (default) or `akaze`. AKAZE is 2.5× faster and more
selective *on a page of 300 dpi or more* (454 / 56 / 16 inliers against a floor
of 4), and misses a faint stamp at 139 dpi every time (61 / 8 / 0). Choose it
only if this step runs on a high-resolution page.

The library's descriptors are computed **inside the traced polygon** (grown by
8 px), not over the whole snippet, and **at the scale of the page being
searched** — `save_stamp` records the snippet's dpi for that. Text left in the
crop no longer becomes part of the stamp's signature; it was, silently, and it
matched text.

The plugin declares its one meta key to the host — `declare_meta("stamps_found",
MetaKind.SCALAR)` — so a warp knows it is a number to copy, not a coordinate to
move.

## Placement is not a suggestion

Replay folds erase regions into its keep-mask **at the anchor**. A producer
sitting after a geometric step has polygons in the wrong frame by then, and
replay skips them with a note rather than applying them to the wrong pixels —
so a StampRemover placed after the dewarp works in the forward pass and
quietly does nothing on replay. Put it right after the split.
