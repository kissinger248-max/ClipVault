# Muse, posting to TikTok and YouTube, and CapCut: plan (draft for review)

Status: **plan only, no code.** Written 2026-11-19 for the owner to review.
**Decided 2026-11-19:** posting route **(b)**: keep the official-API uploaders and apply for
TikTok's and Google's audits (`docs/POSTING_AUDITS.md`); hard rule 2 was changed to match.
M1 (Upload-Post adapter) is dropped. The Muse connector (M2) and CapCut (M3) still wait for
decisions 2 and 3. The request was:
"incorporate Muse to log into accounts and run uploading to TikTok/YouTube, and also editing
with CapCut if needed." Nothing is built until the decisions in section 7 are made.

## 1. Summary

- **Recommended:** Muse becomes a way to *talk to* ClipVault ("what's ready?", "post the top
  three", "pause posting"). ClipVault stays the engine that decides what may be posted. A posting
  service that has already passed TikTok's and Google's app audits does the uploading. Muse
  never holds a password and never drives a browser.
- **Not recommended:** Muse opening tiktok.com and YouTube Studio in its browser, typing your
  password and clicking Upload. It breaks hard rule 2, it is against both platforms' terms, and
  it puts the accounts you are building at risk of being banned (section 3).
- **CapCut:** it has no official editing API, and its terms forbid automated scripts.
  ClipVault already makes the edits CapCut is usually used for. The plan builds the missing
  effects into ClipVault, and adds a "send to CapCut" package for touching a clip up by hand
  (section 5).

## 2. What Muse is (checked 2026-11-19)

- **Meta Muse** is Meta's AI assistant, available in the US and Canada. Since September 2026 it
  has **connectors**: built-in ones (Meta's apps, a browser) and **custom connectors** for outside
  services. Several guides describe custom connectors as remote MCP servers over HTTPS with an
  OAuth sign-in. Meta's own developer documentation wasn't found, and sources disagree, so this
  needs confirming before anything is built.
- **Posting services already offer Muse connectors**: Upload-Post (listing in review), Postiz,
  PostEverywhere and ClipSpeed. In that model, Muse doesn't log in to TikTok or YouTube. You
  link each account to the service once (OAuth). Muse then asks the service to post, and the
  service uses TikTok's and YouTube's official APIs.
- **Meta doesn't review custom connectors**, so whatever a connector exposes, Muse can do.
- Muse also has a **built-in browser** it can drive. That is the only way it could "log into
  accounts" itself.

## 3. Two ways Muse could upload, compared

| | A. Through connectors and official APIs | B. Muse drives a browser and logs in |
|---|---|---|
| How | Muse → ClipVault → posting service → TikTok Content Posting API / YouTube Data API | Muse opens tiktok.com/upload and YouTube Studio, types your password, clicks Upload |
| Passwords | Never seen by Muse or ClipVault (OAuth link, once per account) | Given to an AI's browser session |
| ClipVault's hard rules | Fits rule 2 (posting-service adapter) and rule 1 (the permission gate stays in ClipVault) | **Breaks rule 2** ("never use browser automation"), and changing it needs your decision (rule 11) |
| Platform terms | The intended way | TikTok forbids automated scripts that "interact with the Services" and bots without written approval; YouTube forbids access "using any automated means" except its API or with permission |
| Risk to the accounts | Normal | Ban or restriction risk; CAPTCHAs, 2FA and "suspicious login" checks interrupt it |
| Reliability | Clear success/failure, the post's URL comes back | Breaks when a page changes; no reliable "it posted" signal |
| Public posts | Yes, via an audited posting service (section 4) | Yes, but see the risks above |

**Recommendation: A.** Section 6 lists what B would take if you still want it.

## 4. What ClipVault already has, and the real blocker

- **Posting exists** (`clipvault/posting/`, built in 0.6.0/0.7.0):
  - TikTok, YouTube Shorts and Instagram Reels through their official APIs, with a queue that
    spaces posts out and a dry run that starts ON;
  - TikTok defaults to drafts (you tap Post);
  - Theo's pre-upload checks, and a "Post manually" list.
  - Hard rule 1 is enforced in the code: only streamers with posting switched on and a recorded
    permission can be posted.
- **The blocker isn't logging in, it's the platforms' audits.** Through your *own* API apps:
  - TikTok keeps every post private until the app passes TikTok's Content Posting audit
    (TikTok's Direct Post docs, updated Aug 2026).
  - YouTube keeps uploads from unverified API projects private until an audit.

  A browser robot would get around the audits, which is exactly what the platforms' terms
  forbid. An **audited posting service** gets around them legitimately, because its app already
  passed. That is what hard rule 2 and SPEC §12.1 (Upload-Post first, Blotato as backup) were
  for.
- **Open conflict, noted in PROGRESS.md:** the 0.6.0 posting code uses the platforms' APIs
  directly, not the posting-service adapter in hard rule 2. You haven't signed off on that
  either. This plan settles both questions with decision 1.

## 5. CapCut

- **No official editing API.** CapCut's only official APIs are narrow AI generation features.
  Its terms forbid using "automated scripts or other technologies to … interact with the
  Services". Community projects (CapCutAPI, capcut-mate) write unofficial CapCut draft files.
  They are unofficial libraries in the sense of hard rule 2, and they break whenever CapCut
  changes its format.
- **ClipVault already does the usual CapCut work automatically:**
  - 9:16 layouts (face cam split, letterbox, native);
  - the hook headline with an emoji;
  - word-by-word highlighted captions inside the safe box;
  - loudness to -14 LUFS;
  - quality checks.
- **Plan:**
  1. You list the CapCut effects you actually use: zoom punch-ins on a reaction, sound effects,
     transitions, auto-reframe that follows the face, B-roll, templates. The common ones become
     render options in ClipVault's FFmpeg renderer, so they apply to every clip automatically.
     Punch-in zooms and a "boom" sound on the peak moment are easy wins.
  2. For the occasional hand touch-up, a **"Send to CapCut" package**: the clip without burned-in
     text, its captions as an `.srt` file, the headline as text, and a short how-to. You import
     it into CapCut yourself. Nothing is automated.
  3. Muse doesn't edit in CapCut.

## 6. If you still want option B (Muse logs in with a browser)

It would take:
- changing hard rule 2 (your decision);
- accepting that it breaks TikTok's and YouTube's terms;
- starting on throwaway test accounts, not the accounts you're building;
- your passwords stored in a password manager Muse can use, never in ClipVault;
- your confirmation on every post;
- a stop as soon as a CAPTCHA, a 2FA prompt or a warning appears.

Hard rule 1 still applies: only permitted streamers. I would still advise against it. A ban on
the posting accounts costs more than an audited posting service.

## 7. Decisions needed

1. **Posting route** (also settles the open hard-rule-2 conflict):
   - (a) **Upload-Post**, a paid audited service, with Blotato as backup. Recommended: public
     posts now, no audits to wait for, and it matches hard rule 2. TikTok isn't on Upload-Post's
     free plan, so this needs your OK on a paid plan (hard rule 11).
   - (b) Keep the 0.6.0 official-API uploaders and apply for TikTok's and Google's audits.
     Free, but everything stays private until they pass (weeks).
   - (c) Option B, the browser. Not recommended (section 6).
2. **Muse connector:** yes or no. If yes, how it reaches ClipVault on your computer (section 8,
   M2), and whether Muse may only approve and pause, or also pick clips.
3. **CapCut:** which effects you want built in, and whether you want the "Send to CapCut" package.
4. **First public post** of each account: you confirm it, as hard rule 11 already says.

## 8. Build order (after the decisions)

**M1. Posting-service adapter.**
- `publish/uploadpost.py` behind the existing queue: accounts linked through Upload-Post's
  hosted connect link (no passwords), TikTok privacy options read before each post, campaign
  disclosure flags, the post's URL stored.
- The 0.6.0 official-API uploaders stay as a second route.
- The permission gate, spacing, dry run and auto-pause stay unchanged.

Done when:
- a dry run passes through the adapter;
- the first real post of a test account is confirmed by you and visible to a logged-out viewer
  (SPEC §12.1);
- a clip of a streamer without permission can't be queued (test).

**M2. ClipVault connector for Muse.**
- A small, opt-in tool server inside ClipVault with a fixed list of tools:
  - `ready_clips`, `clip` (headline, still, length), `post_queue`;
  - `approve_post` / `skip_post`, `pause_posting` / `resume_posting`, `posting_status`.
- **No tool can change a streamer's posting permission, add accounts or read files**: those
  stay in the panel, with you.
- Muse runs in Meta's cloud, so it needs an HTTPS address. Two options:
  - (a) an outbound tunnel from your computer (for example Cloudflare Tunnel, free), with
    OAuth/sign-in in front and the tunnel off unless switched on;
  - (b) host the connector on the web version's server.
- Muse is never connected to the posting service directly: posting always goes through
  ClipVault's queue, so hard rule 1 can't be skipped.

Done when:
- from Muse you can list ready clips, approve or skip a post and pause posting;
- every tool call is logged;
- tests prove the permission gate holds through the connector;
- the tunnel is off by default.

**M3. CapCut-style effects and the "Send to CapCut" package**, from decision 3. Each effect is a
look setting with a QA check. Text stays in the safe box (hard rule 5); Shorts stay 15–59 s
(hard rule 4).

## 9. Risks

- **Muse's connector platform is new** (September 2026), and its documentation is unclear.
  Confirm how custom connectors work before M2.
- **Posting services change prices and plans.** Keep the adapter interface, so Blotato or the
  official uploaders can take over.
- **Platform rules:** reposting streamers' content still needs their permission (hard rule 1),
  and raw reposts usually don't earn creator rewards (SPEC §19).
- **Security:** a tunnel opens your computer to the internet. That's why it's off by default,
  has a sign-in, and has a small fixed tool list with no file access.

## Sources (read 2026-11-19)

- Muse connectors and posting connectors:
  - Postiz: https://postiz.com/blog/meta-muse-ai-connectors-schedule-social-media-posts
  - Upload-Post: https://www.upload-post.com/meta-muse-social-media/
  - PostEverywhere: https://posteverywhere.ai/agents/muse
  - Custom integrations: https://parallel.ai/articles/meta-muse-custom-integrations
  - Adding MCP connectors: https://anythingmcp.com/guides/clients/muse
- TikTok Content Posting API (unaudited clients post privately):
  https://developers.tiktok.com/docs/en/content-posting-api-reference-direct-post
- YouTube videos.insert (unverified projects upload privately):
  https://developers.google.com/youtube/v3/docs/videos/insert
- TikTok terms on automation (third-party record of the current text):
  https://conductatlas.com/platform/tiktok/tiktok-terms-of-service/provision/CA-P-018595/prohibition-on-automated-scraping-of-platform-data/
- YouTube terms: https://www.youtube.com/terms
- CapCut, no official editing API and its terms on automation:
  https://www.usecarly.com/blog/claude-capcut-integration/ and
  https://www.wireflow.ai/blog/capcut-api
