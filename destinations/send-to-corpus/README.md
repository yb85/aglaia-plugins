# send-to-corpus

Upload a finished export to a **Corpus library** instance: `POST /book/upload`, multipart, `X-API-Key` header.

Aglaïa already knows the document's metadata, so it sends it — retyping a title into a web form is the tedium this destination removes. Only fields that have a value are sent: an empty field means *erase this* to the corpus's metadata route.

Formats offered: PDF, Markdown, and Aglaïa's **OCR textpack** (`<stem>_OCR.textpack`: `source.pdf` + `text.md` + raw OCR output, [yb85/aglaia#148](https://github.com/yb85/aglaia/issues/148)). The textpack needs a corpus server that admits `.textpack` uploads (yb85/corpus#119); an older one answers 400 and the send says so. With `corpus.zlib_id` in its `info.json` the OCR attaches to that book; without it corpus creates a new book.

**Behind a proxy.** A corpus can sit behind an authenticating proxy (Cloudflare Access, for one). Add its headers under **Additional headers** in the plugin's settings — for Access, `CF-Access-Client-Id` and `CF-Access-Client-Secret`, whose values are generated as a *service token*. They go out with every request, alongside the API key, and their values live in the keychain.

When Access still refuses with the headers set, the message says so and quotes Access's own verdict (`service_token_status` from its redirect): the pair is wrong, or that token is not on this application under a policy whose **action is Service Auth** — adding the token to an *Allow* policy is not enough.

Redirects are never followed: a proxy answering 302 towards its own login page used to be read as a corpus answer, so a send that never arrived was reported as "already present". Any redirect, or any HTML where JSON was promised, is now a failure that names the proxy and the address.

The API's four outcomes stay four:

| code | meaning |
|---|---|
| 201 | added, with the id |
| 200 | the corpus already has it (its rule: same sha-256, or same title and author); nothing written. The message names the instance and the book it matched, so a wrong match is visible |
| 400 | extension not admitted, empty, or a malformed textpack |
| 413 | over 2 GiB — over **100 MB** behind Cloudflare: send a big textpack to the LAN address |
| 422 | the textpack's `corpus.zlib_id` names no book |

See the module docstring in the entry file for the details, and
[the plugin store design](https://github.com/yb85/aglaia/blob/main/docs/plugin-store.md)
for how plugins are reviewed and installed.
