"""Export the dashboard as a single self-contained HTML file.

The live dashboard needs its server. A snapshot does not: the same page with the
data baked in, so it can be opened from disk, emailed, or hosted anywhere. It is
read-only by construction - no polling, no WebSocket, no action buttons.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..log import get_logger

log = get_logger("web.snapshot")

TEMPLATE = Path(__file__).parent / "dashboard.html"

# The live page bootstraps itself here; a snapshot replaces that with baked data.
LIVE_BOOTSTRAP = "connect(); poll(); setInterval(poll, 5000);"

SNAPSHOT_BOOTSTRAP = """
/* ---- static snapshot: no server, no polling, no actions ---- */
SNAP = BAKED_SNAPSHOT;
render();
renderFeed(SNAP.events || []);
(function () {
  const conn = document.getElementById("b-conn");
  conn.className = "badge";
  conn.textContent = "snapshot \\u00b7 " + BAKED_AT;
  for (const id of ["btn-scan", "btn-flatten"]) {
    const b = document.getElementById(id);
    b.disabled = true;
    b.title = "Actions need the live server (python run.py serve)";
  }
})();
"""


def build(snapshot: dict[str, Any], *, taken_at: datetime | None = None) -> str:
    """Return standalone HTML for this snapshot."""
    html = TEMPLATE.read_text(encoding="utf-8")
    moment = (taken_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")

    if LIVE_BOOTSTRAP not in html:
        raise RuntimeError(
            "dashboard.html no longer contains the live bootstrap line; "
            "update LIVE_BOOTSTRAP in snapshot.py to match"
        )

    baked = (
        f"const BAKED_SNAPSHOT = {json.dumps(snapshot, separators=(',', ':'))};\n"
        f"const BAKED_AT = {json.dumps(moment)};\n"
        f"{SNAPSHOT_BOOTSTRAP}"
    )
    html = html.replace(LIVE_BOOTSTRAP, baked)

    # A snapshot must say so, above everything else on the page.
    banner = (
        '<div class="banner warn" style="margin-bottom:16px">'
        f"<strong>Read-only snapshot</strong> taken {moment}. Numbers are frozen at that "
        "moment and this page places no orders. Run <code>python run.py serve</code> "
        "for the live desk."
        "</div>"
    )
    html = html.replace('<div id="banners"></div>', banner + '<div id="banners"></div>')
    # The title is the page's NAME - it stays stable across exports. When the
    # snapshot was taken belongs in the banner and the badge, not the tab.
    html = html.replace(
        "<title>hyperbot — Hyperliquid copy desk</title>",
        "<title>Hyperliquid Copy Desk</title>",
    )
    return html


def write(snapshot: dict[str, Any], path: str) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build(snapshot), encoding="utf-8")
    log.info("snapshot written to %s (%.0f KB)", out, out.stat().st_size / 1024)
    return out
