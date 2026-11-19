"""What each platform needs: credential fields, OAuth endpoints, scopes, the redirect URI and a short guide."""

from __future__ import annotations

import os

PORT = int(os.getenv("CLIPWAVE_PORT", "8765"))


def redirect_uri(platform: str, port: int | None = None) -> str:
    return f"http://localhost:{port or PORT}/oauth/{platform}/callback"


PLATFORMS: dict[str, dict] = {
    "tiktok": {
        "label": "TikTok", "fields": [("client_key", "Client Key"), ("client_secret", "Client Secret")],
        "auth_url": "https://www.tiktok.com/v2/auth/authorize/",
        "token_url": "https://open.tiktokapis.com/v2/oauth/token/",
        "scopes": ["user.info.basic", "video.upload"], "oauth": True,
        "guide": [
            "Go to developers.tiktok.com and sign in with the TikTok account that will own the app.",
            "Manage apps → Connect an app → create an app (any name, e.g. ClipWave).",
            "Add products: Login Kit and Content Posting API. Turn on the scopes user.info.basic and video.upload (Drafts mode needs nothing else; video.publish is only for Direct Post, after TikTok approves it).",
            "Under Login Kit (Desktop) paste the redirect URI shown here (a localhost address with a port; the port must match). If TikTok refuses http://localhost, see SETUP_UPLOADS.md (HTTPS tunnel).",
            "Add each of your TikTok accounts as a Target User while the app is in sandbox.",
            "Copy the Client Key and Client Secret into the boxes here.",
            "Use Drafts mode (default): each clip lands in your TikTok inbox and you tap Post. "
            "No audit is needed for your own accounts; TikTok keeps at most 5 waiting drafts "
            "per account a day. Direct Post needs TikTok's audit, which doesn't accept "
            "reposting tools (see docs/POSTING_AUDITS.md).",
        ]},
    "youtube": {
        "label": "YouTube / Google", "fields": [("client_id", "OAuth Client ID"), ("client_secret", "Client Secret")],
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"],
        "oauth": True,
        "guide": [
            "Go to console.cloud.google.com and create a project (e.g. ClipWave).",
            "APIs & Services → Library → enable “YouTube Data API v3”.",
            "OAuth consent screen: Personal mode if Google offers it, otherwise External "
            "with your channels’ Google accounts as Test users. Publish it (“In production”) "
            "when it works: sign-ins of apps left in Testing expire after 7 days.",
            "Credentials → Create credentials → OAuth client ID → type “Web application”.",
            "Add the redirect URI shown here under Authorized redirect URIs.",
            "Copy the Client ID and Client Secret into the boxes here.",
            "Google no longer locks unverified projects to private (checked 2026-11-19); "
            "uploads have their own quota of 100 a day. Upload one Short as private, check "
            "it, then switch privacy to public in Settings.",
        ]},
    "instagram": {
        "label": "Instagram / Meta", "fields": [("app_id", "Instagram App ID"), ("app_secret", "Instagram App Secret")],
        "auth_url": "https://www.instagram.com/oauth/authorize",
        "token_url": "https://api.instagram.com/oauth/access_token",
        "scopes": ["instagram_business_basic", "instagram_business_content_publish"], "oauth": True,
        "guide": [
            "Go to developers.facebook.com → Create app → type Business (or Other) and add the product “Instagram”.",
            "Choose “API setup with Instagram login”.",
            "Add each Instagram account as an Instagram tester (App roles → Roles) and accept the invite in the Instagram app.",
            "Paste the redirect URI shown here under Business login settings → OAuth redirect URIs.",
            "Copy the Instagram App ID and App Secret (from the Instagram product page, not the app’s main ID) into the boxes here.",
            "The Instagram account must be a Business or Creator account (Instagram → Settings → Account type).",
        ]},
    "twitch": {
        "label": "Twitch", "fields": [("client_id", "Client ID"), ("client_secret", "Client Secret")], "oauth": False,
        "scopes": [],
        "guide": [
            "Go to dev.twitch.tv/console → Register Your Application.",
            "OAuth redirect URL: http://localhost (not used for clip data). Category: Application Integration.",
            "Copy the Client ID and generate a Client Secret. They are used for streamer, game and title lookups.",
            "These may already be in your .env; values saved here take priority.",
        ]},
}
