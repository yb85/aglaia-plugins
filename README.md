# aglaia-plugins

The plugin registry for [Aglaïa](https://aglaia.bibli.cc) — a scanner and
page-extraction pipeline for books.

Aglaïa reads `index.json` from this repository, verifies each file against the
sha256 the index gives for it, and installs the plugin into the user's own
plugin directory. **Every plugin here has been read by a human before it was
merged.**

## Read this before you install anything

An Aglaïa plugin is Python that runs **inside the application process**. It has
the same access to your files as Aglaïa itself. There is no sandbox, and the
design document [says so in its first
section](https://github.com/yb85/aglaia/blob/main/docs/plugin-store.md) rather
than implying otherwise — a user who believes there is a wall installs things
he would otherwise think about.

What this registry actually provides:

| | |
|---|---|
| Every PR is read by a person | catches anything a reader would catch |
| **No plugin ever installs a dependency** | removes the entire supply-chain vector |
| Per-file sha256 in the index | a tampered download is refused |
| Declared capabilities, shown before install | you consent to *this* behaviour |
| Automated import scan | catches accidents and obvious exfiltration |

Reviewing something does not make it ours. The install dialog names the person
who wrote each plugin and links to its source, and the button says **"I trust
the code and/or its author"** — because a reader who checked the diff and a
reader who knows the author are both consenting truthfully.

## What is here

| Plugin | Kind | What it does |
|---|---|---|
| [send-to-folder](destinations/send-to-folder) | destination | Copy the export into a folder you already sync. The smallest complete example. |
| [send-to-calibre](destinations/send-to-calibre) | destination | Add it to a calibre content server's library. |
| [send-to-kindle](destinations/send-to-kindle) | destination | Mail it to a Kindle address. |
| [send-to-corpus](destinations/send-to-corpus) | destination | Upload it to a YLIB corpus. |

## Submitting one

Read [CONTRIBUTING.md](CONTRIBUTING.md). In short: one directory per plugin
under its kind, a manifest, exactly one top-level `.py`, a LICENSE, a README,
and imports drawn only from `aglaia.plugin_api`, a small standard-library
allow-list, and the libraries Aglaïa already ships.

`index.json` is **generated**. Never edit it by hand; CI regenerates it and
fails the PR if the committed copy is stale.
