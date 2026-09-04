# SPDX-License-Identifier: MIT
"""Copy a finished export into a folder you already sync.

The least clever destination, and often the right one. If a folder on this
machine is already watched by Dropbox, Syncthing, iCloud Drive, a Calibre
auto-add directory, or a script of your own, then "send it there" is the whole
integration — no server, no credentials, no API to go stale.

It has no secrets, declares no network access, and imports nothing beyond the
plugin API. That makes it the smallest complete example of a destination, and
the one to copy when writing your own.

Two things it does take care of:

* **Filenames.** The document's title becomes the filename, with the
  characters a filesystem will not take replaced rather than dropped — a title
  with a slash in it must not become two path segments.
* **Not overwriting silently.** An existing file of the same name gets a
  ` (2)` suffix unless you turn that off. Overwriting a scan someone spent an
  hour on, because two books share a title, is not a thing to do quietly.
"""

from __future__ import annotations

import re
from pathlib import Path

from aglaia.plugin_api import (
    BookMeta, CheckResult, Destination, Field, SendResult, register_destination,
)

#: Characters no common filesystem accepts, plus the ones that would change
#: what the path means.
_UNSAFE = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def safe_name(stem: str, fallback: str) -> str:
    """A filename that means what the title meant, on any filesystem."""
    cleaned = _UNSAFE.sub("-", str(stem or "")).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if len(cleaned) > 120:
        cleaned = cleaned[:120].rstrip()
    return cleaned or fallback


@register_destination
class FolderDestination(Destination):
    name = "send-to-folder"
    display = "A folder"
    description = "Copy the export into a folder — synced, watched, or plain."
    accepts = ("pdf", "md", "txt", "epub", "html", "docx")

    CONFIG_FIELDS = (
        Field("folder", "Folder", "str", "", required=True,
              placeholder="~/Dropbox/Books",
              help="Where to copy the export. Anything that already syncs or "
                   "is watched by another tool works: Dropbox, Syncthing, "
                   "iCloud Drive, a Calibre auto-add folder."),
        Field("subfolder_by_author", "A folder per author", "bool", False,
              help="Put each file under <folder>/<author>/. Documents with no "
                   "author go straight in the top folder rather than into one "
                   "called 'Unknown'."),
        Field("overwrite", "Overwrite an existing file", "bool", False,
              help="Off: a name that is taken gets ' (2)'. Overwriting a scan "
                   "because two books share a title is not a thing to do "
                   "quietly."),
    )
    SECRET_FIELDS = ()

    def _root(self) -> Path:
        return Path(str(self.conf("folder") or "")).expanduser()

    def check(self) -> CheckResult:
        root = self._root()
        if not str(self.conf("folder") or "").strip():
            return CheckResult(False, "No folder set.")
        if not root.exists():
            return CheckResult(
                False, f"{root} does not exist. Create it, or point this at a "
                       f"folder that is already there — a destination that "
                       f"makes directories on your behalf is a destination "
                       f"that quietly puts books in the wrong place.")
        if not root.is_dir():
            return CheckResult(False, f"{root} is a file, not a folder.")
        probe = root / ".aglaia-write-test"
        try:
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError as e:
            return CheckResult(False, f"Cannot write to {root} — {e.strerror}.")
        return CheckResult(True, f"Ready — files will go to {root}.")

    def send(self, path: Path, meta: BookMeta) -> SendResult:
        path = Path(path)
        if not path.is_file():
            return SendResult(False, f"No such file: {path}")
        pre = self.check()
        if not pre.ok:
            return SendResult(False, pre.message)

        target_dir = self._root()
        if self.conf("subfolder_by_author") and (meta.author or "").strip():
            target_dir = target_dir / safe_name(meta.author, "")
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return SendResult(False, f"Cannot create {target_dir} — {e}")

        stem = safe_name(meta.title or path.stem, path.stem)
        target = target_dir / f"{stem}{path.suffix}"
        if target.exists() and not self.conf("overwrite"):
            n = 2
            while (target_dir / f"{stem} ({n}){path.suffix}").exists():
                n += 1
            target = target_dir / f"{stem} ({n}){path.suffix}"

        try:
            target.write_bytes(path.read_bytes())
        except OSError as e:
            return SendResult(False, f"Copy failed — {e}")
        mb = target.stat().st_size / (1024 * 1024)
        return SendResult(True, f"Copied to {target} ({mb:.1f} MB).",
                          url=target.as_uri(), detail={"path": str(target)})
