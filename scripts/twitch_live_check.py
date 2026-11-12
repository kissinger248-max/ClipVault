"""Manual live check against the real Twitch API (not run by pytest).

Usage:
    python scripts/twitch_live_check.py LOGIN [LOGIN ...] [--days 7] [--dump DIR]

Needs the Twitch client ID and secret in the keychain
(`python -m clipvault secrets set twitch_client_id` / `twitch_client_secret`).
It only reads public data and writes nothing to the database.

What it shows, per streamer:
- the user lookup result;
- how many clips Twitch returned for the last N days, with the top five;
- hints for the "do Auto Clips show up?" question (SPEC section 18): how many
  clips are featured, and which creator names made the most clips. A clip made
  by Twitch itself, rather than a viewer, should stand out there.

With --dump DIR the raw JSON responses are saved, to be turned into test fixtures.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

from clipvault import twitch
from clipvault.timeutil import utc_now


def main() -> int:
    parser = argparse.ArgumentParser(description="Manual live check against the Twitch API.")
    parser.add_argument("logins", nargs="+")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--dump", metavar="DIR", help="save raw responses as JSON here")
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")

    try:
        client = twitch.build_client()
    except twitch.MissingCredentials as exc:
        print(exc)
        return 1
    dump_dir = Path(args.dump) if args.dump else None
    if dump_dir:
        dump_dir.mkdir(parents=True, exist_ok=True)

    try:
        logins = [login.lower() for login in args.logins]
        users = client.get_users(logins=logins)
        _dump(dump_dir, "users.json", {"data": users})
        found = {user["login"]: user for user in users}
        now = utc_now()
        for login in logins:
            user = found.get(login)
            if user is None:
                print(f"\n{login}: Twitch has no such user")
                continue
            kind = user.get("broadcaster_type") or "regular"
            print(f"\n{user['display_name']} (id {user['id']}, {kind})")
            clips = list(client.iter_clips(user["id"], now - timedelta(days=args.days), now))
            _dump(dump_dir, f"clips_{login}.json", {"data": clips})
            featured = sum(1 for clip in clips if clip.get("is_featured"))
            print(f"  {len(clips)} clip(s) in the last {args.days} day(s); {featured} featured")
            for clip in clips[:5]:
                views, seconds = clip["view_count"], clip["duration"]
                print(f"  {views:>8,} views  {seconds:>5.1f}s  {clip['title']}")
            creators = Counter(clip.get("creator_name") or "(none)" for clip in clips)
            top = ", ".join(f"{name} x{count}" for name, count in creators.most_common(5))
            print(f"  top clip creators: {top or '(no clips)'}")
            if clips:
                print("  fields on a clip: " + ", ".join(sorted(clips[0])))
    except (twitch.TwitchAuthError, twitch.TwitchAPIError) as exc:
        print(f"Twitch problem: {exc}")
        return 1
    finally:
        client.close()
    return 0


def _dump(folder: Path | None, name: str, payload: dict) -> None:
    if folder:
        text = json.dumps(payload, indent=2, ensure_ascii=False)
        (folder / name).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
