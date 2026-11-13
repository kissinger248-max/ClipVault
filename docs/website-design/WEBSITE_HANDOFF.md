# ClipVault website: handoff

These `*.dc.html` files are design mockups. Build the real site as plain static HTML, CSS and a little JavaScript, with no framework and no build step.
Mockup syntax to convert:
- `{{ x }}` holes and the `<script type="text/x-dc">` blocks: turn them into static markup, or into small vanilla JS where noted below.
- `<sc-for>` loops: write each item out as plain HTML.
- `<sc-if>` blocks: plain HTML, or JS-toggled where noted below.

Where this file and the mockups disagree, this file wins.

## Facts that changed since the mockups
- **Windows and macOS installers are built, tested and published on GitHub Releases.** Linux and the Docker/server build are not released yet. Show them as small "Coming soon" cards with no download button, or leave them out.
- **Setup uses "Sign in with Twitch".** Nobody needs developer-console app keys anymore. Remove every mention of "app keys" and "developer console":
  - Main: the line under the download cards.
  - FAQ: "Do I need a Twitch account?" and "What else do I need to install?"
  - Privacy §4: Twitch sees requests from the user's signed-in account.
  - New wording: "Sign in with your Twitch account during setup. ClipVault only reads; it never posts or writes in chat."
- **FFmpeg:** check the installer build config in this repo. If the installers bundle FFmpeg, remove "Needs FFmpeg" everywhere. If they don't, keep it.
- **Code signing:** check the release workflow. Write the FAQ answer "My computer warns me when I open the installer" from what's actually true (signed or not, for each OS).

## Pages
| Mockup | Build as |
| --- | --- |
| Main.dc.html | index.html (landing + download) |
| FAQ.dc.html | faq.html |
| About.dc.html | about.html |
| Privacy.dc.html | privacy.html |
| Terms.dc.html | terms.html |
| Licenses.dc.html | licenses.html |
| (new) | 404.html, same header/footer, "Page *not found.*" + home link |

## Downloads from GitHub Releases
- Get the repo and the latest release with `gh repo view` and `gh release view --json tagName,publishedAt,assets,url`.
- Button links use the stable URL `https://github.com/<owner>/<repo>/releases/latest/download/<asset-file-name>`, so they never need editing. This only works if asset names don't contain the version. If they do, say so and suggest renaming them in the release workflow.
- Fill version, release date and file sizes from the release. Make "Release notes" link to the release page and add an "Older versions" link to `/releases`.
- Checksums: use the SHA-256 the release lists. If there is none, download the assets and compute it. Show checksums in a small `<details>` under the download cards.
- The macOS card says which chips the build supports (Apple silicon, Intel or universal), taken from the build config.

## Look
- `site.css` is production-ready. Use it as `assets/site.css`. All colours are CSS variables on `.cv-site`, dark by default.
- **Fonts, from Google Fonts:**
  - Inter 400/500/600/700 (body), Inter Tight 500–800 (headlines, tracking −0.05em), Instrument Serif italic (the purple `<em>` accent) and Montserrat 800 (text on the mock videos).
  - Use one `css2` link with `display=swap`, plus `preconnect` to fonts.googleapis.com and fonts.gstatic.com.
  - Privacy §2 must add: "Fonts are loaded from Google Fonts, so your browser sends your IP address to Google when a page loads," with a link to Google's privacy policy. Change the "no third-party" lines in the summary to match.
- **Buttons:** pills (radius 999px). Primary is #7C3AED with white text. Secondary is off-white #F4F4F5 with dark text.
- **Text colour pairs:** body text #F4F4F5 and secondary text #A1A1AA on #0B0B0D; purple accent text #A78BFA. All pass WCAG AA.

## Images (Claude Code makes these)
- Put them in `assets/img/`, named `placeholder-*.svg` or `.webp` so they're easy to find and replace.
- Use them for: the clip cards and video frames in the hero and the endless row (9:16 and 16:9), the share image (1200×630, built from the hero) and favicons/apple-touch-icon (from the purple logo mark).
- Placeholders show dark abstract frames only: no real people, logos, streamer names or Twitch, TikTok or YouTube branding. Give each one an empty `alt=""` when it's decoration, or a short alt text when it carries meaning.

## Behaviour (assets/site.js, vanilla, about 60 lines)
1. **OS detection:** `navigator.userAgentData.platform`, falling back to `navigator.userAgent`. It sets the hero button ("Download for macOS" or "Download for Windows") to that OS's asset URL, and highlights that platform's download card.
   - On Linux the button reads "Download" and scrolls to #download, where the "Coming soon" card is.
   - On a phone the button reads "Get it for your computer" and a note is shown.
   - Without JS the button links to #download.
2. **Hero tilt:** on mousemove over `.cv-stage`, set `.cv-rig` to `rotateX(6 - y*14deg) rotateY(-14 + x*26deg)`, where x and y run from −0.5 to 0.5. It resets on mouseleave. Skip it entirely under `prefers-reduced-motion`. Throttle with requestAnimationFrame.
3. **Endless clip row:** the cards are listed twice so the CSS loop is seamless. Mark the second set `aria-hidden="true"`.
4. **FAQ:** native `<details>` elements; no JS needed.

## Placeholders that stay for the owner (search for "[")
[Free] / pricing, your name or company, address, contact/legal/privacy emails, jurisdiction, the licence choice in Terms §2, the transcription provider name (Privacy §4) and the hosting provider plus log retention (Privacy §2). Everything that can be read from the repo or GitHub Releases gets filled in, not left as a placeholder.

## Must-haves
- Responsive down to 360 px. Under about 900 px the hero stacks, the visual goes below the text and shrinks to fit (scale the 640 px rig), and grids drop to 1–2 columns.
- Every page: `<title>`, meta description, Open Graph/Twitter tags with the share image, favicon, `lang="en"`, and a skip-to-content link.
- Add `sitemap.xml` and `robots.txt`.
- No cookies or analytics. Google Fonts is the only third-party request.
- Lighthouse: Accessibility 100 and Performance 90+ on mobile.
- Keep the "not affiliated with Twitch, TikTok, YouTube or Google" line in every footer.
