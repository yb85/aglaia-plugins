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
    min_inliers: 12
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

## Placement is not a suggestion

Replay folds erase regions into its keep-mask **at the anchor**. A producer
sitting after a geometric step has polygons in the wrong frame by then, and
replay skips them with a note rather than applying them to the wrong pixels —
so a StampRemover placed after the dewarp works in the forward pass and
quietly does nothing on replay. Put it right after the split.
