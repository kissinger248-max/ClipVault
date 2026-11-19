"""TikTok Content Posting API: creator_info/query → video/init (or inbox/video/init) → chunked PUT → status/fetch.

Modes (setting tiktok_mode): "drafts" sends the clip to the user's TikTok inbox (they tap Post); "direct" posts it,
but an unaudited app can only post privately. That case is detected and explained."""

from __future__ import annotations

import json
import time

from clipvault.posting import http as requests

from clipvault.posting.uploaders.base import ClipFile, Result, UploadError, Uploader, request

API = "https://open.tiktokapis.com/v2/post/publish"
CHUNK = 10 * 1024 * 1024
UNAUDITED = "unaudited_client_can_only_post_to_private_accounts"
WHY_PRIVATE = ("TikTok hasn't audited this app yet, so direct posts can only be private. "
               "Switch TikTok to Drafts mode in settings (the clip lands in your inbox and you tap Post), "
               "or apply for the Content Posting API audit (see SETUP_UPLOADS.md).")


def _check(r: requests.Response, what: str) -> dict:
    try:
        data = r.json()
    except ValueError:
        raise UploadError(f"TikTok {what}: unreadable answer ({r.status_code})", "temporary")
    err = data.get("error") or {}
    code = err.get("code", "ok")
    if code != "ok":
        msg = err.get("message", "")
        if code == UNAUDITED:
            raise UploadError(WHY_PRIVATE, "permanent")
        if code == "scope_not_authorized":
            raise UploadError(f"TikTok {what}: this connection doesn't have the permission needed for that step. "
                              "Drafts need only video.upload; reconnect TikTok on Connected Accounts and approve it.",
                              "permanent")
        if code in ("rate_limit_exceeded", "internal_error", "spam_risk_too_many_posts"):
            raise UploadError(f"TikTok {what}: {code} {msg}", "temporary")
        raise UploadError(f"TikTok {what}: {code} {msg}", "permanent")
    return data.get("data") or {}


def chunk_plan(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunks): TikTok wants >=5 MB chunks, the last one may be larger."""
    if size <= CHUNK:
        return size, 1
    return CHUNK, size // CHUNK


class TikTok(Uploader):
    platform = "tiktok"

    def upload(self, clip, caption, account, token, settings, *, dry_run=False, http=requests, sleep=time.sleep) -> Result:
        hdr = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}
        mode = settings.get("tiktok_mode", "drafts")
        info = {}
        if mode == "direct" and "scopes" in account and "video.publish" not in json.loads(account["scopes"] or "[]"):
            raise UploadError("Direct Post needs TikTok's video.publish permission, which this connection doesn't have "
                              "(ClipVault only asks for user.info.basic and video.upload for now). Use Drafts mode.", "permanent")
        if mode == "direct" and not account.get("simulated"):      # creator info belongs to Direct Post (needs video.publish)
            info = _check(request(http, "POST", f"{API}/creator_info/query/", headers=hdr, sleep=sleep), "creator info")
            limit = info.get("max_video_post_duration_sec")
            if limit and clip.duration > limit:
                raise UploadError(f"Video is {clip.duration:.0f}s; this TikTok account allows {limit}s.", "permanent")
        options = info.get("privacy_level_options") or []
        privacy = "PUBLIC_TO_EVERYONE" if "PUBLIC_TO_EVERYONE" in options else (options[0] if options else "SELF_ONLY")
        note = ""
        if mode == "direct" and privacy == "SELF_ONLY":
            note = "Posted privately: " + WHY_PRIVATE
        csize, total = chunk_plan(clip.size)
        source = {"source": "FILE_UPLOAD", "video_size": clip.size, "chunk_size": csize, "total_chunk_count": total}
        if dry_run:
            return Result(True, detail=f"Dry run: would send {clip.size/1e6:.1f} MB to TikTok as {mode} ({privacy}). {note}".strip(),
                          dry_run=True)
        if mode == "direct":
            body = {"post_info": {"title": caption["per_platform"]["tiktok"][:2200], "privacy_level": privacy,
                                  "disable_duet": False, "disable_comment": False, "disable_stitch": False},
                    "source_info": source}
            init = _check(request(http, "POST", f"{API}/video/init/", headers=hdr, json=body, sleep=sleep), "start")
        else:
            init = _check(request(http, "POST", f"{API}/inbox/video/init/", headers=hdr, json={"source_info": source},
                                  sleep=sleep), "start (drafts)")
        publish_id, url = init["publish_id"], init["upload_url"]
        with clip.path.open("rb") as f:
            for i in range(total):
                start = i * csize
                data = f.read(csize if i < total - 1 else clip.size - start)
                r = request(http, "PUT", url, data=data, sleep=sleep, headers={
                    "Content-Type": "video/mp4", "Content-Length": str(len(data)),
                    "Content-Range": f"bytes {start}-{start + len(data) - 1}/{clip.size}"})
                if r.status_code not in (200, 201, 206):
                    raise UploadError(f"TikTok upload chunk {i + 1}/{total} failed ({r.status_code})", "temporary")
        for _ in range(60):
            st = _check(request(http, "POST", f"{API}/status/fetch/", headers=hdr, json={"publish_id": publish_id},
                                sleep=sleep), "status")
            s = st.get("status", "")
            if s == "PUBLISH_COMPLETE":
                ids = st.get("publicaly_available_post_id") or st.get("publicly_available_post_id") or []
                return Result(True, url=f"https://www.tiktok.com/@{account.get('handle','')}/video/{ids[0]}" if ids else "",
                              detail=note or "Posted.", extra={"publish_id": publish_id})
            if s == "SEND_TO_USER_INBOX":
                return Result(True, detail="In your TikTok inbox: open TikTok and tap Post.", extra={"publish_id": publish_id})
            if s == "FAILED":
                reason = st.get("fail_reason", "unknown")
                raise UploadError(f"TikTok rejected the video: {reason}", "permanent")
            sleep(5)
        raise UploadError("TikTok is still processing after 5 minutes.", "temporary")
