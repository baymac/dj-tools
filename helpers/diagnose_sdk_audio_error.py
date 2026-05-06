"""Run the SDK on specific bp_ids and dump every diagnostic field returned —
so we can build a precise classifier for `Unable to get audio information`
error variants (delisted vs region-locked vs token-stale vs other).

The point: figure out which fields differ between error types empirically,
without guessing. Run this against:

  - Known-working tracks (gold standard, success path)
  - Tracks the failure sidecar already proves are unavailable (real catalog
    issue under fresh token = baseline for "delisted")
  - Anything that fails after refreshing DJ Studio (token issue path)

Compare the outputs side-by-side to see what `bp_state`, `error_props`, and
`phase` look like in each case. Then update `_is_bp_token_stale` in
detect/studio_analyse.py with real markers.

DJ Studio MUST be quit (port 61894 + .beatport/ cache locks).

Usage:
  uv run python helpers/diagnose_sdk_audio_error.py --ids 23330162,2012257,19729168
  uv run python helpers/diagnose_sdk_audio_error.py --ids 23330162 --output /tmp/probe.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from detect.studio_sdk import (
    SdkHelper,
    _get_dj_studio_access_token,
    is_dj_studio_running,
)

console = Console()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ids", required=True, metavar="ID,ID,...",
                    help="Comma-separated beatport IDs to probe")
    ap.add_argument("--output", default="/tmp/sdk_audio_diagnostic.json",
                    help="Where to save the full per-id response dump")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    if is_dj_studio_running():
        console.print("[red]Quit DJ Studio first[/red] (port 61894 + .beatport/ cache locks)")
        return 1

    try:
        bids = [int(x.strip()) for x in args.ids.split(",") if x.strip()]
    except ValueError:
        console.print(f"[red]--ids must be comma-separated integers[/red]")
        return 1

    try:
        access_jwt = _get_dj_studio_access_token()
    except Exception as e:
        console.print(f"[red]DJ Studio JWT decrypt/refresh failed: {e}[/red]")
        return 1

    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ids": bids,
        "results": [],
    }

    with SdkHelper(access_jwt, verbose=args.verbose) as helper:
        for bid in bids:
            console.print(f"\n[cyan]bp:{bid}[/cyan]")
            res = helper.analyze(bid)

            # Skim everything we can about the result
            entry = {
                "beatport_id": bid,
                "ok": res.get("ok"),
                "message": res.get("message"),
                "bp_state": res.get("bp_state"),
                "error_props": res.get("error_props"),
                "phase": res.get("phase"),
            }
            # If success, we have a result with timing info etc.
            if res.get("ok"):
                r = res.get("result") or {}
                entry["success_summary"] = {
                    "duration_sec": r.get("duration_sec"),
                    "sample_rate": r.get("sample_rate"),
                    "channels": r.get("channels"),
                    "timing_ms": r.get("timing_ms"),
                    "wasm": r.get("wasm"),
                    "server_ok": (r.get("server") or {}).get("ok"),
                    "stems_ok": list((r.get("stem_metrics") or {}).keys()),
                }
                console.print(f"  [green]OK[/green]  duration={r.get('duration_sec'):.0f}s  total={r.get('timing_ms', {}).get('total', 0)}ms")
            else:
                console.print(f"  [red]FAIL[/red]")
                console.print(f"    message:     {entry['message']}")
                console.print(f"    bp_state:    {entry['bp_state']!r}")
                console.print(f"    phase:       {entry['phase']!r}")
                console.print(f"    error_props: {entry['error_props']}")
            out["results"].append(entry)

    # Save full JSON for offline diff
    Path(args.output).write_text(json.dumps(out, indent=2, default=str))
    console.print(f"\n[dim]Full dump → {args.output}[/dim]")
    console.print()

    # Compact summary table for quick visual diff
    console.print("[bold]Summary (compact):[/bold]")
    print(f"{'bp_id':>10}  {'status':<10}  {'phase':<35}  {'bp_state':<25}  message")
    print("-" * 130)
    for r in out["results"]:
        status = "OK" if r["ok"] else "FAIL"
        msg = (r["message"] or "")[:40]
        phase = str(r["phase"] or "")[:35]
        bs = str(r["bp_state"] or "")[:25]
        print(f"{r['beatport_id']:>10}  {status:<10}  {phase:<35}  {bs:<25}  {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
