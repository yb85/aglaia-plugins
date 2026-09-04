# send-to-kindle

Mail a finished export to a Kindle address — or to anyone. Plain SMTP with the document attached.

Two of Amazon's rules are enforced *after* the SMTP transaction has already succeeded, so both are handled before it starts:

* **The sender must be on Amazon's approved list.** Mail from an unrecognised address is dropped in silence — SMTP says 250, nothing arrives, nothing reports a failure. So the success message promises only that the mail was accepted, and names the approved-sender list.
* **There is a size ceiling** around 50 MB. An oversized file is refused *before the connection is opened*, naming the limit and the actual size.

For Gmail, iCloud and Outlook the password must be an **app-specific** one. That is the most common first-send failure, so the field says so.

See the module docstring in the entry file for the details, and
[the plugin store design](https://github.com/yb85/aglaia/blob/main/docs/plugin-store.md)
for how plugins are reviewed and installed.
