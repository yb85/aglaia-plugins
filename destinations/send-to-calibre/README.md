# send-to-calibre

Add a finished export to a **calibre content server**'s library, over the server's own HTTP API (`POST /cdb/add-book/…`).

The file goes in the **raw request body** — not multipart — and every parameter is a **path segment**, so each is percent-quoted: an unquoted `/` in a title reshapes the route.

Needs a server running with `--enable-auth` and a user that can write to the library. calibre defaults to **digest** auth; `--auth-mode=basic` is the manual's advice behind a TLS reverse proxy. Both are supported, because guessing wrong gives a 401 that says nothing about which was expected.

A `duplicates` reply is reported as *already there*, not as an error.

See the module docstring in the entry file for the details, and
[the plugin store design](https://github.com/yb85/aglaia/blob/main/docs/plugin-store.md)
for how plugins are reviewed and installed.
