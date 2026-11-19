"""YouTube Shorts: resumable videos.insert. Vertical and <= 3 minutes (validated first). Quota is tracked by the queue."""

from __future__ import annotations

import time

from clipvault.posting import http as requests

from clipvault.posting.uploaders.base import ClipFile, Result, UploadError, Uploader, request

UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
API = "https://www.googleapis.com/youtube/v3"
MAX_SECONDS = 180
# videos.insert, docs checked 2026-11-19: 1 unit from its own "Video Uploads" bucket, 100 calls a
# day per project, and "Videos uploaded from unverified API projects are not restricted to
# private viewing mode" (older docs said 1,600 units and a private lock until an audit).
UPLOADS_PER_DAY = 100
LOCKED = (
    "YouTube kept this upload private although public was asked for. Google's current rules "
    "don't lock unverified projects to private, so check the channel in YouTube Studio "
    "(verified phone number, no strikes or restrictions) and the privacy setting in "
    "Posting → Settings."
)


def validate(clip: ClipFile) -> None:
    if clip.duration > MAX_SECONDS:
        raise UploadError(f"Too long for Shorts ({clip.duration:.0f}s, max {MAX_SECONDS}s).", "permanent")
    if clip.height <= clip.width:
        raise UploadError("Shorts must be vertical.", "permanent")


class YouTube(Uploader):
    platform = "youtube"

    def upload(self, clip, caption, account, token, settings, *, dry_run=False, http=requests, sleep=time.sleep) -> Result:
        validate(clip)
        hdr = {"Authorization": f"Bearer {token}"}
        privacy = settings.get("youtube_privacy", "private")
        if not account.get("simulated"):
            r = request(http, "GET", f"{API}/channels", params={"part": "id", "mine": "true"}, headers=hdr, sleep=sleep)
            if r.status_code in (401, 403):
                raise UploadError("YouTube refused the sign-in. Reconnect the account.", "permanent")
        if dry_run:
            return Result(True, dry_run=True,
                          detail=f"Dry run: would upload {clip.size/1e6:.1f} MB to YouTube as {privacy}.")
        body = {"snippet": {"title": caption["per_platform"]["youtube_title"][:100],
                            "description": caption["per_platform"]["youtube_description"], "categoryId": "20"},
                "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
        r = request(http, "POST", UPLOAD, params={"uploadType": "resumable", "part": "snippet,status"}, sleep=sleep,
                    headers=hdr | {"Content-Type": "application/json; charset=UTF-8", "X-Upload-Content-Length": str(clip.size),
                                   "X-Upload-Content-Type": "video/mp4"}, json=body)
        _raise_for(r)
        loc = r.headers.get("Location")
        if not loc:
            raise UploadError("YouTube gave no upload address.", "temporary")
        with clip.path.open("rb") as f:
            r = request(http, "PUT", loc, data=f, sleep=sleep, headers={"Content-Type": "video/mp4",
                                                                        "Content-Length": str(clip.size)})
        _raise_for(r)
        data = r.json()
        vid = data["id"]
        got = (data.get("status") or {}).get("privacyStatus", privacy)
        detail = "Uploaded."
        if privacy != "private" and got == "private":
            detail = LOCKED
        return Result(True, url=f"https://youtube.com/shorts/{vid}", detail=detail, extra={"video_id": vid, "privacy": got})


def _raise_for(r: requests.Response) -> None:
    if r.status_code < 400:
        return
    try:
        err = r.json().get("error", {})
        reasons = {e.get("reason") for e in err.get("errors", [])}
        msg = err.get("message", "")
    except ValueError:
        reasons, msg = set(), r.text[:200]
    if "quotaExceeded" in reasons or "dailyLimitExceeded" in reasons or "uploadLimitExceeded" in reasons:
        raise UploadError("YouTube's daily upload quota is used up; will retry tomorrow.", "quota")
    if r.status_code in (401, 403):
        raise UploadError(f"YouTube refused: {msg or r.status_code}. Reconnect the account or check permissions.", "permanent")
    raise UploadError(f"YouTube error {r.status_code}: {msg}", "permanent")
