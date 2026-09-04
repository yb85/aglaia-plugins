# send-to-folder

Copy a finished export into a folder you already sync.

The least clever destination, and often the right one: if a folder is already
watched by Dropbox, Syncthing, iCloud Drive, a Calibre auto-add directory or a
script of your own, then "send it there" is the whole integration.

No secrets, no network, and it imports nothing beyond the plugin API — which
makes it the **smallest complete example** of a destination, and the one to
copy when writing your own.

## Settings

| Setting | Meaning |
|---|---|
| Folder | Where to copy the export. Must already exist. |
| A folder per author | Put each file under `<folder>/<author>/`. |
| Overwrite an existing file | Off by default; a taken name gets ` (2)`. |

## Two things it is careful about

**Filenames.** The document's title becomes the filename, with characters a
filesystem will not take replaced rather than dropped — a title containing a
slash must not become two path segments.

**Not overwriting silently.** Two books can share a title. Overwriting a scan
someone spent an hour on is not a thing to do quietly.
