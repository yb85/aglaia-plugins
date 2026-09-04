# Submitting a plugin

## The shape

```
<kind>/<slug>/
├── aglaia-plugin.toml   the manifest
├── <entry>.py           the ONE top-level module
├── _support/            optional private modules
├── README.md            what it does, and why it does it that way
├── LICENSE              matching the SPDX id in the manifest
└── tests/test_<slug>.py
```

`<kind>` is `processors`, `ocr` or `destinations`. **The directory decides the
slug** — the manifest must agree with it, and CI fails if it does not. The slug
also names the plugin's keychain namespace and its settings file, which is not
a thing to take a manifest's word for.

## The manifest

```toml
[plugin]
slug     = "send-to-folder"      # [a-z0-9-]{3,40}, == the directory name
name     = "A folder"
version  = "1.0.0"
summary  = "One line."
author   = "Your Name <you@example.org>"
homepage = "https://…"
license  = "MIT"                 # SPDX id, matching LICENSE
entry    = "send_to_folder.py"

[requires]
aglaia  = ">=0.1.0rc5,<0.2"
python  = ">=3.12"
api     = 1                      # aglaia.plugin_api major
imports = []                     # third-party modules you will import

[capabilities]
config  = true                   # wants its own settings store
secrets = false                  # wants the namespaced keychain
network = false                  # will open network connections
files   = true                   # touches paths outside its own folder
```

Every capability you declare is shown to the user before they install. Declaring
one does not grant power you could not take anyway — it is a **statement of
intent**, and doing something you did not declare is what gets a plugin revoked.

## What you may import

* `aglaia.plugin_api` — and **nothing else** under `aglaia`. Everything else is
  internal and moves without notice.
* A small standard-library allow-list. Notably **not** `os`, `sys`,
  `subprocess`, `socket`, `shutil`, `ctypes`, `importlib`, `pickle`,
  `sqlite3` or `keyring` — what you legitimately need from those you get from
  `PluginContext` instead: `ctx.config`, `ctx.secrets`, `ctx.data_dir`,
  `ctx.log`.
* Third-party modules **Aglaïa already ships**, declared in
  `[requires].imports`: `numpy`, `cv2`, `scipy`, `PIL`, `yaml`, and `httpx`
  (the last only with `network = true`).

**No plugin ever installs a dependency.** Not at review, not at install, not at
runtime. If you need a library Aglaïa does not ship, propose it as a host
dependency in a separate PR against Aglaïa itself. This single rule is what
removes the supply-chain surface, and it is why the import list can be closed.

## What CI checks

- The manifest parses and matches its directory.
- The version is a new semver.
- `LICENSE` exists and matches the SPDX id.
- Exactly one top-level `.py`, named by `entry`.
- No compiled artefacts, no vendored dependencies, no `pip`/`uv` calls.
- The import scan is clean against your declared allow-list.
- `index.json` regenerates to exactly what is committed.

## What a human then asks

- Does what it does match what the README and the manifest say?
- Is every declared capability used? Does anything **undeclared** happen?
- Any network call — to where, carrying what?
- Any path outside `ctx.data_dir`?
- Any dynamic import, `eval`, `exec`, `getattr` on builtins, base64 blob, or
  long opaque literal?
- Would this be embarrassing if it turned out to be malicious?

That last one is the real question, and it is why this registry is small on
purpose.

## Writing a destination

Start from [send-to-folder](destinations/send-to-folder): no secrets, no
network, nothing imported beyond the plugin API — the smallest thing that is
still a complete plugin.
