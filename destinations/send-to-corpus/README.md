# send-to-corpus

Upload a finished export to a **Corpus library** instance: `POST /book/upload`, multipart, `X-API-Key` header.

Aglaïa already knows the document's metadata, so it sends it — retyping a title into a web form is the tedium this destination removes. Only fields that have a value are sent: an empty field means *erase this* to the corpus's metadata route.

The API's four outcomes stay four:

| code | meaning |
|---|---|
| 201 | added, with the id |
| 200 | already in base; nothing written |
| 400 | extension not admitted, or empty |
| 413 | over 2 GiB |

See the module docstring in the entry file for the details, and
[the plugin store design](https://github.com/yb85/aglaia/blob/main/docs/plugin-store.md)
for how plugins are reviewed and installed.
