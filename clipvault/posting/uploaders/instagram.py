"""Instagram Reels (Instagram API with Instagram Login): REELS container → resumable upload to rupload.facebook.com
(no public hosting needed) → poll status → media_publish. Checks content_publishing_limit first."""

from __future__ import annotations

import time

from clipvault.posting import http as requests

from clipvault.posting.uploaders.base import ClipFile, Result, UploadError, Uploader, request

GRAPH = "https://graph.instagram.com/v21.0"
RUPLOAD = "https://rupload.facebook.com/ig-api-upload/v21.0"
MAX_SECONDS = 900


def _data(r: requests.Response, what: str) -> dict:
    try:
        d = r.json()
    except ValueError:
        raise UploadError(f"Instagram {what}: unreadable answer ({r.status_code})", "temporary")
    if r.status_code >= 400 or d.get("error"):
        e = d.get("error") or {}
        code = e.get("code")
        kind = "temporary" if code in (1, 2, 4, 17, 32, 613) else "permanent"
        raise UploadError(f"Instagram {what}: {e.get('message', r.status_code)}", kind)
    return d


class Instagram(Uploader):
    platform = "instagram"

    def upload(self, clip, caption, account, token, settings, *, dry_run=False, http=requests, sleep=time.sleep) -> Result:
        if account.get("account_type") not in (None, "BUSINESS", "MEDIA_CREATOR", "CREATOR"):
            raise UploadError("This Instagram account is Personal. Switch it to Business or Creator in the Instagram app.",
                              "permanent")
        if clip.duration > MAX_SECONDS:
            raise UploadError(f"Reels can be at most {MAX_SECONDS//60} minutes.", "permanent")
        ig = account.get("external_id", "me")
        if not account.get("simulated"):
            lim = _data(request(http, "GET", f"{GRAPH}/{ig}/content_publishing_limit",
                                params={"fields": "quota_usage,config", "access_token": token}, sleep=sleep), "limit check")
            row = (lim.get("data") or [{}])[0]
            total = (row.get("config") or {}).get("quota_total", 25)
            if row.get("quota_usage", 0) >= total:
                raise UploadError("Instagram's 24-hour publishing limit is reached; will retry later.", "quota")
        if dry_run:
            return Result(True, dry_run=True, detail=f"Dry run: would upload {clip.size/1e6:.1f} MB to Instagram Reels.")
        c = _data(request(http, "POST", f"{GRAPH}/{ig}/media", sleep=sleep, data={
            "media_type": "REELS", "upload_type": "resumable", "caption": caption["per_platform"]["instagram"],
            "access_token": token}), "create")
        cid = c["id"]
        with clip.path.open("rb") as f:
            r = request(http, "POST", c.get("uri") or f"{RUPLOAD}/{cid}", sleep=sleep, data=f, headers={
                "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(clip.size)})
        _data(r, "upload")
        for _ in range(60):
            st = _data(request(http, "GET", f"{GRAPH}/{cid}", params={"fields": "status_code,status", "access_token": token},
                               sleep=sleep), "status")
            code = st.get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                raise UploadError(f"Instagram couldn't process the video: {st.get('status', code)}", "permanent")
            sleep(5)
        else:
            raise UploadError("Instagram is still processing after 5 minutes.", "temporary")
        pub = _data(request(http, "POST", f"{GRAPH}/{ig}/media_publish", sleep=sleep,
                            data={"creation_id": cid, "access_token": token}), "publish")
        link = ""
        try:
            link = _data(request(http, "GET", f"{GRAPH}/{pub['id']}", params={"fields": "permalink", "access_token": token},
                                 sleep=sleep), "link").get("permalink", "")
        except UploadError:
            pass
        return Result(True, url=link, detail="Posted.", extra={"media_id": pub["id"]})
