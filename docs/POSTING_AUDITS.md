# Posting through the official APIs: what each platform needs (checked 2026-11-19)

The owner chose to post through TikTok's, YouTube's and Instagram's own APIs
(`docs/MUSE_POSTING_PLAN.md`, decision 1b). This is what that takes, what to apply for, and
what not to apply for. Setting up the apps and connecting accounts: `SETUP_UPLOADS.md`.

Hard rule 1 still comes first: only clips of streamers with posting switched on and a
recorded permission can be queued. The first public post of each account is confirmed by you
(hard rule 11).

## Summary

| | What to do | Public posts | Audit needed |
|---|---|---|---|
| **TikTok** | Keep **Drafts** mode (the default). Each clip lands in your TikTok inbox; you tap Post | Yes, when you post the draft in TikTok | No. Don't apply for Direct Post (see below) |
| **YouTube** | Set privacy to public after checking the first private upload | Yes | No. Google's current docs no longer lock unverified projects to private |
| **Instagram** | Your own Business/Creator accounts as testers | Yes, for your accounts | Only for accounts you don't own |

## TikTok

**Drafts (Upload API, scope `video.upload`).** This is what ClipVault does by default.
- The clip goes to the account's TikTok inbox. You open the notification, can add a sound or
  edit, then tap Post. Set TikTok's content disclosure there for campaign clips.
- TikTok holds **at most 5 pending drafts per account per 24 hours**. ClipVault now never
  schedules more than 5 TikTok drafts a day per account, whatever the daily maximum says.
  If TikTok still answers "too many pending", the draft waits until the next day. Posting or
  deleting drafts in TikTok frees room.
- At most 6 requests a minute per account; an upload address is valid for an hour. ClipVault
  handles both.
- While the TikTok app is in **Sandbox**, add each of your posting accounts as a Target User.
  That's enough for your own accounts. Leaving Sandbox (app review) is only needed for accounts
  that aren't yours.

**Direct Post: don't apply.** TikTok's Content Sharing Guidelines (developers.tiktok.com/doc/content-sharing-guidelines) rule out ClipVault's use:
- "Copying arbitrary content from other platforms is not acceptable". Posting Twitch clips is
  exactly that, even with the streamer's permission.
- A tool that only uploads to accounts you or your team manage is not acceptable.
- Every post needs a person choosing privacy (no default), comments/duet/stitch (none on by
  default), commercial disclosure and a consent line, which doesn't fit a scheduled queue.

  Unaudited apps can direct-post only privately (`SELF_ONLY`) to private accounts, up to 5 users
  a day. ClipVault detects this and says to use Drafts. Direct mode also needs the
  `video.publish` scope, which ClipVault doesn't request.

**Content rules that apply either way:**
- no added brand name, logo, watermark, link or promotional text on the video (ClipVault's
  renders have none; the "@login on Twitch" credit line is off by default);
- captions and hashtags must be editable by you before posting (in Drafts you paste them in
  TikTok).

## YouTube Shorts

Google's current `videos.insert` reference (developers.google.com/youtube/v3/docs/videos/insert):
- "Videos uploaded from unverified API projects are not restricted to private viewing mode."
  Older docs, and the first version of `SETUP_UPLOADS.md`, said they were locked to private
  until an audit.
- Quota: 1 unit from a separate "Video Uploads" bucket, **100 uploads a day per project**. The
  older "1,600 of 10,000 units, about 6 a day" no longer applies.

The Google sign-in (OAuth consent screen) still matters, because `youtube.upload` is a
sensitive scope:
- **Personal mode**, if Google offers it to your account (Cloud console onboarding): the app is
  limited to your own Google account, needs no verification and shows no warning. Best for
  one person.
- **Testing** (external, test users): works, but Google issues refresh tokens that expire after
  7 days for apps in Testing, so you'd reconnect YouTube weekly. Check the current OAuth docs;
  this was not re-confirmed today.
- **In production, unverified:** a "Google hasn't verified this app" screen you click through,
  at most 100 users. Fine for your own channels.
- **Verification** (privacy policy URL, demo video, scope justification) is only needed to offer
  the app to other people.

Steps to go public:
1. Leave ClipVault's YouTube privacy on **private** (the default) and let one Short upload.
2. Check it in YouTube Studio: vertical, under 3 minutes, headline and captions right.
3. Switch privacy to public in Posting → Settings. That's the first public post (hard rule 11).
   The channel needs a verified phone number for longer videos and custom thumbnails, not for
   Shorts.

YouTube's reused-content and Content ID rules still apply to reposted streams (SPEC §19):
expect claims on music, and no monetisation for plain reposts.

## Instagram Reels

Unchanged from `SETUP_UPLOADS.md`: Business or Creator accounts added as testers work
straight away; 25 posts per 24 hours per account; Meta app review only for accounts you don't
own.

## What changed in ClipVault today

- TikTok drafts are capped at 5 per account per day, and "too many pending" waits a day
  instead of landing in Post manually.
- YouTube's notes, upload limit and "kept private" message follow Google's current docs.
- Hard rule 2 (CLAUDE.md) now names the official APIs as the posting route.
