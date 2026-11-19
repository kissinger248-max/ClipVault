"""Manual live check of the website's own Twitch app (hard rule 9: live checks live here).

    python scripts/web_twitch_live_check.py LOGIN [LOGIN ...] [--clip URL] [--vod URL]

Needs the website's keys in the environment (never the desktop app's):
CLIPVAULT_WEB_TWITCH_CLIENT_ID and CLIPVAULT_WEB_TWITCH_CLIENT_SECRET, and
CLIPVAULT_WEB_PUBLIC_URL if the website isn't at http://localhost:8800.

It gets an app token, searches each streamer, and plans the clips the website would make
from each channel (and from --clip / --vod links), using the official API only. Nothing is
downloaded and nothing is stored. Last, it prints the sign-in return address the Twitch
app must list, exactly.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clipvault_web import accounts, twitch_web  # noqa: E402
from clipvault_web.settings import from_env  # noqa: E402


def show(plan: twitch_web.Plan) -> None:
    live = " (LIVE)" if plan.is_live else ""
    median = f", usual clip {plan.median_views:,.0f} views" if plan.median_views else ""
    median = median or next(
        (f", usual clip {i.median:,.0f} views" for i in plan.items if i.median), ""
    )
    print(f"  {plan.kind}: {plan.name}{live}, {len(plan.items)} clip(s), "
          f"{plan.more_found} more moment(s){median}")  # fmt: skip
    for item in plan.items:
        title = item.title[:60]
        print(f"    {item.views or 0:>9,} views  {item.duration:4.0f} s  {title!r}  {item.url}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("logins", nargs="*", help="streamers to look up")
    parser.add_argument("--clip", help="a clip link to plan")
    parser.add_argument("--vod", help="a past-stream (VOD) link to plan")
    parser.add_argument("--count", type=int, default=5)
    args = parser.parse_args()
    settings = from_env(Path("web-data-live-check"))
    if not (settings.twitch_client_id and settings.twitch_client_secret):
        print("Set CLIPVAULT_WEB_TWITCH_CLIENT_ID and CLIPVAULT_WEB_TWITCH_CLIENT_SECRET first "
              "(the website's own Twitch app).", file=sys.stderr)  # fmt: skip
        return 2

    client = twitch_web.build_client(settings)
    problems = 0
    try:
        for login in args.logins:
            found = twitch_web.search(client, login)
            names = ", ".join(f"{r['name']}{' (LIVE)' if r['live'] else ''}" for r in found[:5])
            print(f"search {login!r}: {names or 'nothing'}")
            try:
                show(twitch_web.plan_run(client, twitch_web.Link("channel", login.lower()),
                                         args.count))  # fmt: skip
            except twitch_web.LinkProblem as exc:
                print(f"  {exc}")
                problems += 1
        for text in filter(None, (args.clip, args.vod)):
            print(f"link {text}:")
            try:
                show(twitch_web.plan_run(client, twitch_web.parse_link(text), args.count))
            except twitch_web.LinkProblem as exc:
                print(f"  {exc}")
                problems += 1
    finally:
        client.close()

    print()
    print("The Twitch app's OAuth Redirect URL must be exactly:")
    print(f"  {accounts.redirect_uri(settings)}")
    print("Sign-in to try by hand (website running): "
          f"{settings.public_url}/mine  and  {settings.public_url}/optout")  # fmt: skip
    print("OK" if not problems else f"{problems} lookup(s) found nothing to make")
    return 0


if __name__ == "__main__":
    sys.exit(main())
