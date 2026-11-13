# ClipVault panel redesign: handoff

The `*.dc.html` files in this folder are design mockups, not production templates.
Use them as the visual reference and rebuild each screen as Jinja2 + HTMX templates.
Their `{{ t.xxx }}` holes are mockup tokens. They clash with Jinja syntax, so don't copy them verbatim.

## Screen → file
| Screen | Mockup |
| --- | --- |
| Home | Main.dc.html (dark: HomeDark) |
| Clips | Clips.dc.html (dark: ClipsDark) |
| A clip's page | ClipPage.dc.html (dark: ClipPageDark) |
| Streamers | Streamers.dc.html |
| Settings | Settings.dc.html |
| First-run password | SetupPassword.dc.html (Setup step 0) |
| Setup steps 1–6 + done | Setup.dc.html (`step` 1–7) |
| Phone layout | PhoneHome / PhoneClips (+ Dark) |

## Design tokens (CSS custom properties)
Light / dark. Switch on `@media (prefers-color-scheme: dark)`, with no in-app appearance setting.

| Token | Light | Dark |
| --- | --- | --- |
| --bg (page) | #F5F5F7 | #1C1C1E |
| --side (sidebar) | #ECECF0 | #252527 |
| --card | #FFFFFF | #2C2C2E |
| --menu | #FFFFFF | #3A3A3C |
| --label | #1D1D1F | #F5F5F7 |
| --label2 (secondary text) | #6E6E73 | #A1A1A6 |
| --sep (borders) | #D9D9DE | #3A3A3C |
| --fill (fields, segmented bg) | #E6E6EB | #3A3A3C |
| --switch-off | #D1D1D6 | #48484A |
| --switch-on | #00782C | #30A14E |
| --accent (filled buttons, white text) | #1E6EF4 | #1E6EF4 |
| --link / icons | #1E6EF4 | #5CB8FF |
| --selected (sidebar row) | #DCE8FD | #1D3557 |
| --green | #00782C | #4AD968 |
| --red | #C50F25 | #FF6165 |
| --red-bg | #FDE7EA | #45191D |
| --orange | #B04A00 | #FFA056 |
| --badge | #C50F25 | #E5323F |
| --shadow | 0 1px 2px rgba(0,0,0,.05), 0 2px 8px rgba(0,0,0,.04) | inset 0 1px 0 rgba(255,255,255,.05) |

Phone layout: page bg #F2F2F7 / #000000, card #FFFFFF / #1C1C1E.
All text pairs meet WCAG AA (4.5:1).

## Type
- Font stack: `-apple-system, BlinkMacSystemFont, "SF Pro Text", Inter, "Segoe UI", sans-serif`. Do not bundle SF fonts; their licence covers Apple platforms only.
- Desktop: body 14/20, secondary 13, section heading 17/22 semibold, page title 26/32 bold, big numbers 28/34 bold.
- Phone (under 600 px): body 17/22, page title 34/41 bold, controls at least 44 px tall.
- Video text (headline and captions): Montserrat 800, already bundled in assets/fonts.

## Layout and components
- Sidebar (200 px) + content. Below 600 px wide, switch to a 4-tab bottom bar (Home, Clips, Streamers, Settings) that floats with a frosted look.
- Cards: radius 12 (desktop) or 16 (phone), 1 px --sep border, --shadow.
- Buttons: 32–36 px tall, radius 8–9. Primary = --accent fill. Secondary = --card with --sep border. Destructive = red text. Labels are verbs in Title Case.
- Switches: 44×26 pill, 22 px knob. Segmented controls on a --fill track.
- Clip cards are 9:16 phone-version thumbnails with the headline box drawn in.
- Problems appear inline in a "Needs you" card with Fix and Dismiss, never as a pop-up. The Home sidebar item shows a red count badge.
- Status card colours: green (running), yellow (paused), red (needs you). Always pair the colour with an icon and words.
- Icons are inline stroke SVGs (see mockups). SF Symbols can't be used outside Apple platforms.

## Motion
Use `motion.css` as is (it goes in web/static/), and hook up its classes: `cv-thumb` (clip cards), `cv-spin` ("being edited"), `cv-live` (LIVE badge), `cv-word` (caption preview). Everything turns off under prefers-reduced-motion.
