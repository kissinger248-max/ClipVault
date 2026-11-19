# Setting up auto-upload (TikTok, YouTube Shorts, Instagram Reels)

Open ClipVault and click **Posting** in the sidebar. Everything below happens there. Nothing posts until you turn **Dry run** off.

## 1. Create the developer apps (free)
The Posting → **Platform Setup** page shows the steps and the exact redirect URI for each platform
(`http://localhost:<panel port>/oauth/<platform>/callback`, port 8765 unless you changed it). Paste the keys there; they are stored in the macOS Keychain
under ClipVault's own Keychain entry (names start with `posting.`). They are never written to the repo, the database or `.env`.

| Platform | Where | Keys |
| --- | --- | --- |
| TikTok | developers.tiktok.com → Login Kit + Content Posting API | Client Key, Client Secret |
| YouTube | console.cloud.google.com → YouTube Data API v3 → OAuth client (Web) | Client ID, Client Secret |
| Instagram | developers.facebook.com → Instagram → API setup with Instagram login | Instagram App ID, App Secret |
| Twitch | dev.twitch.tv/console | Client ID, Client Secret (already in `.env` is fine) |

**If a portal refuses `http://localhost` as a redirect URI** (TikTok and Meta can be strict about HTTPS), expose the studio
through a free HTTPS tunnel (e.g. Tailscale Funnel or a Cloudflare quick tunnel), register that HTTPS address instead, and
open the studio through it when you press Connect. I could not verify each portal's current rule from here, so check this first.

## 2. Connect accounts
Posting → **Connected Accounts** → Connect. You sign in on the platform's own page; ClipVault never sees a password.
Add up to ~5 accounts per platform. Instagram accounts must be Business or Creator. While an app is unapproved, add each of your
accounts as a test user (TikTok: Target User; Google: Test user; Meta: Instagram tester).

## 3. The audits (why posts may be private at first)
- **TikTok:** an unaudited app can only post privately. Keep **Drafts mode** (default): the clip lands in your TikTok inbox and you tap Post.
  To get Direct Post, submit the app for review in the TikTok developer portal (demo video, explanation of the use case) and wait for approval.
- **YouTube:** uploads from an unverified Google project are locked to private. Apply via the YouTube API Services audit form, then set privacy to public.
  Default quota is 10,000 units/day, about 6 uploads (each costs 1,600).
- **Instagram:** works for your own testers right away; to use it on accounts you don't own, Meta app review is needed. 25 posts per 24 h per account.

## 4. Safety defaults
Dry run ON (full flow, nothing posted) · auto-approve OFF · TikTok in Drafts · YouTube private · 2 hours (120 minutes, changeable in minutes) between posts per account (+ jitter) ·
4 posts per account per day. Change them in Posting → Settings. Nothing is queued by itself: press **Queue for posting** on a finished phone version (or turn auto-approve on, which only takes clips made afterwards and that passed QA).

**Permission comes first.** A clip can only be queued when its streamer has *Allow posting* on with a recorded permission on the Streamers page. That rule is checked again at upload time, so withdrawing permission stops a queued post.

## 5. Captions
Each clip gets a caption and hashtags from its transcript, the real Twitch category, the stream title and why the Analyst picked it.
Install Ollama and pull a model (`ollama pull llama3.2`) so the caption is model-written; without it the clip's own hook/title is used.
Copy the built-in rules into a file named `hashtags.yaml` in ClipVault's data folder (macOS: `~/Library/Application Support/ClipVault/`) to change them (game map, moment tags, blocklist). Theo blocks any upload whose caption mentions something not in the clip.

## 6. If something fails
It lands in Posting → **Post manually** with the reason, the caption, Copy caption, Open in Finder and Mark as posted.

## TikTok sign-in details
ClipVault signs in to TikTok as a desktop app (Login Kit for Desktop) with PKCE: every Connect press makes a new one-time
code verifier, sends only its SHA-256 (hex) as `code_challenge` with `code_challenge_method=S256`, and gives the verifier back
when swapping the code for tokens. The redirect address is `http://localhost:<port>/oauth/tiktok/callback` with the port the
app is actually using (shown on Platform Setup; it must match the Login Kit entry in the TikTok portal). For now only
`user.info.basic` and `video.upload` are requested, which is all Drafts mode needs; Direct Post needs `video.publish` after
TikTok approves it. If sign-in fails, the Connected Accounts page shows why.

## Where TikTok drafts show up
In Drafts mode TikTok puts the video in your **Inbox** (System notifications); tap it, edit if you like, then tap **Post**
or **Save to drafts** inside TikTok. TikTok's draft upload cannot carry a caption, so ClipVault shows its caption under
Queue → Done with a **Copy caption** button: paste it when you post. A caption that arrives pre-filled by TikTok itself
is TikTok's own default for sandbox apps, not ClipVault's.
