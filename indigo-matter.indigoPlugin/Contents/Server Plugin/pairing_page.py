"""The pairing page's HTML (PRD §6). Pure rendering — no I/O, no Indigo import.

The only caller is :meth:`Plugin._pairing_page`, which builds the live
``pairing`` object and passes it here to render.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

import export_bridge          # for export_bridge.describe_fabric

#: Where the raw ``MT:`` payload can be rendered as a scannable QR code. The
#: CHIP project's own tool, which is the reference implementation of the payload
#: format — so a code it cannot render is a code no commissioner would accept
#: either. Linked rather than embedded: see :meth:`Plugin._pairing_page` for why
#: no QR is generated locally.
QR_VIEWER_URL = "https://project-chip.github.io/connectedhomeip/qrcode.html"


def _escape(text: Any) -> str:
    """Minimal HTML escaping for the pairing page.

    Hand-rolled rather than ``html.escape`` only in that it also handles a
    ``None`` — every value on that page comes from the bridge node or from an
    exception string, and one of them being absent must not render the word
    "None" into a field a user is about to type into their phone.
    """
    if text is None:
        return ""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _pairing_html(pairing, message: str) -> str:
    """The pairing page (PRD §6). Self-contained: no scripts, no assets.

    ``pairing`` may be ``None`` when there is nothing to report — the page still
    renders, carrying ``message``, because a blank page over a bridge that is
    merely not running is indistinguishable from a broken handler.
    """
    manual = _escape(getattr(pairing, "manual_pairing_code", None))
    qr_payload = _escape(getattr(pairing, "qr_pairing_code", None))
    expires = _escape(getattr(pairing, "window_expires_at", None))
    fabrics = list(getattr(pairing, "fabrics", ()) or [])
    paired = ", ".join(_escape(export_bridge.describe_fabric(f)) for f in fabrics) or "none yet"
    banner = f'<p class="msg">{_escape(message)}</p>' if message else ""
    codes = ""
    if manual:
        # The payload is URL-encoded into the viewer link because an `MT:` string
        # is base-38 and can legitimately contain characters that would otherwise
        # end the query (`+`, `/`, `%`), producing a link that opens the tool with
        # a silently truncated payload — a QR that scans and means the wrong thing.
        viewer = f"{QR_VIEWER_URL}?data={quote(str(getattr(pairing, 'qr_pairing_code', '') or ''), safe='')}"
        codes = f"""
    <p class="warn"><strong>This page shows a live commissioning passcode.</strong>
       Anyone who can reach this URL can add the bridge — and every Indigo device you
       export — to <em>their</em> Apple Home, Alexa or Google account, for as long as the
       window is open. The Indigo Web Server only asks for a password if you have turned
       authentication on, so if you have not, treat this URL as the code itself: do not
       put it in a chat or an email, and close the window when you are done (it also
       expires on its own).</p>
    <h2>Manual pairing code</h2>
    <p class="code">{manual}</p>
    <h2>QR payload</h2>
    <p class="payload">{qr_payload}</p>
    <p><a href="{_escape(viewer)}" rel="noreferrer noopener" target="_blank">
       Render this payload as a scannable QR code</a> (opens the Matter project's own
       viewer — it needs internet access, and the payload is sent to it).</p>
    {f'<p class="expiry">This code stops working at {expires}.</p>' if expires else ''}
    <h2>What to expect</h2>
    <p>Add the bridge in your ecosystem's app as you would any Matter accessory. Every
       ecosystem will warn that it is an <strong>uncertified accessory</strong> — that is
       normal for a bridge like this one, and the same warning Homebridge and Home Assistant
       produce. Choose "Add Anyway".</p>"""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Indigo Matter bridge — pairing</title>
<style>
 body {{ font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        margin: 0 auto; max-width: 34rem; padding: 1.5rem; color: #222; }}
 h1 {{ font-size: 1.3rem; }} h2 {{ font-size: 1rem; margin-bottom: .2rem; color: #555; }}
 .code {{ font: 700 2.1rem/1.2 ui-monospace, Menlo, monospace; letter-spacing: .08em;
          margin: .2rem 0 1rem; word-break: break-all; }}
 .payload {{ font: .85rem ui-monospace, Menlo, monospace; word-break: break-all;
             background: #f4f4f6; padding: .6rem; border-radius: .4rem; }}
 .msg {{ background: #fff6d6; border: 1px solid #e8d48a; padding: .7rem; border-radius: .4rem; }}
 .warn {{ background: #fdeaea; border: 1px solid #d99; padding: .7rem; border-radius: .4rem;
          font-size: .92rem; }}
 .expiry {{ color: #a33; }}
 footer {{ margin-top: 2rem; font-size: .85rem; color: #777; }}
 @media (prefers-color-scheme: dark) {{
   body {{ background: #16171a; color: #e6e6e6; }} h2 {{ color: #aaa; }}
   .payload {{ background: #26272b; }} .msg {{ background: #3a3320; border-color: #6b5c2e; }}
   .warn {{ background: #3a2222; border-color: #7a4444; }}
 }}
</style></head><body>
<h1>Indigo Matter bridge</h1>
{banner}{codes}
<footer>Paired ecosystems: {paired}.<br>
This page is served by the Indigo Web Server from the Matter plugin.</footer>
</body></html>"""
