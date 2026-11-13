"""Builds the ASS subtitle file burned into a vertical video (SPEC §10.5).

Everything is positioned by this module with explicit coordinates: libass does
no wrapping or layout of its own. That is what lets QA check, from the same
numbers, that every piece of text sits inside the safe box (hard rule 5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from clipvault.process.transcribe import Word, mask_text
from clipvault.render.template import Font, Look, get_font

_PHRASE_END = ".,?!;:"
_KAPPA = 0.5523  # control-point distance for drawing a quarter circle with a Bezier curve


@dataclass(frozen=True)
class Box:
    """A rectangle on the frame that some burned-in text occupies."""

    name: str
    x0: float
    y0: float
    x1: float
    y1: float

    def inside(self, safe: Look) -> bool:
        return (
            self.x0 >= safe.x0 and self.y0 >= safe.y0 and self.x1 <= safe.x1 and self.y1 <= safe.y1
        )


@dataclass(frozen=True)
class CaptionBlock:
    """A few words shown together, with the time each one is highlighted."""

    words: tuple[Word, ...]  # display text, times relative to the rendered video
    start: float
    end: float
    lines: tuple[tuple[int, ...], ...] = ()  # word indexes per line


@dataclass
class Overlay:
    """The finished subtitle file plus what QA needs to know about it."""

    ass: str
    boxes: list[Box] = field(default_factory=list)
    headline_size: float = 0.0
    headline_lines: int = 0
    headline_fits: bool = True
    blocks: list[CaptionBlock] = field(default_factory=list)


# --- small ASS helpers ---------------------------------------------------------


def ass_color(hex_color: str, opacity: float = 1.0) -> tuple[str, str]:
    """Convert ``#RRGGBB`` to ASS colour and alpha tags' values (``&HBBGGRR&``, ``&HAA&``)."""
    red, green, blue = hex_color[1:3], hex_color[3:5], hex_color[5:7]
    alpha = round((1 - opacity) * 255)
    return f"&H{blue}{green}{red}&".upper(), f"&H{alpha:02X}&"


def ass_time(seconds: float) -> str:
    centis = max(int(round(seconds * 100)), 0)
    hours, rest = divmod(centis, 360_000)
    minutes, rest = divmod(rest, 6000)
    secs, centis = divmod(rest, 100)
    return f"{hours}:{minutes:02}:{secs:02}.{centis:02}"


def ass_escape(text: str) -> str:
    """Make text safe inside a Dialogue line (no override blocks, no line breaks)."""
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


def rounded_rect(width: float, height: float, radius: float) -> str:
    """ASS drawing commands for a rounded rectangle with its top-left corner at 0,0."""
    r = min(radius, width / 2, height / 2)
    c = r * _KAPPA
    w, h = width, height
    points = (
        f"m {r:.1f} 0 l {w - r:.1f} 0 "
        f"b {w - r + c:.1f} 0 {w:.1f} {r - c:.1f} {w:.1f} {r:.1f} "
        f"l {w:.1f} {h - r:.1f} "
        f"b {w:.1f} {h - r + c:.1f} {w - r + c:.1f} {h:.1f} {w - r:.1f} {h:.1f} "
        f"l {r:.1f} {h:.1f} "
        f"b {r - c:.1f} {h:.1f} 0 {h - r + c:.1f} 0 {h - r:.1f} "
        f"l 0 {r:.1f} "
        f"b 0 {r - c:.1f} {r - c:.1f} 0 {r:.1f} 0"
    )
    return points


def _event(start: float, end: float, layer: int, tags: str, text: str) -> str:
    return f"Dialogue: {layer},{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{{{tags}}}{text}"


def _font_tags(font: Font, px: float) -> str:
    return f"\\fn{font.full_name}\\fs{font.ass_size(px):.2f}\\b0\\i0"


# --- headline ------------------------------------------------------------------


@dataclass(frozen=True)
class HeadlineLayout:
    lines: tuple[tuple[str, ...], ...]
    size: float
    fits: bool
    box: Box


def layout_headline(text: str, look: Look) -> HeadlineLayout:
    """Choose the size and line breaks for the headline (largest size that fits)."""
    spec = look.headline
    font = get_font(spec.font)
    words = tuple((text.upper() if spec.uppercase else text).split())
    inner = spec.max_width - 2 * spec.padding_x
    size = float(spec.size)
    chosen: tuple[tuple[str, ...], ...] | None = None
    while size >= spec.min_size:
        chosen = _break_lines(words, font, size, inner, spec.max_lines)
        if chosen is not None:
            break
        size -= 4
    fits = chosen is not None
    if chosen is None:
        size = float(spec.min_size)
        chosen = _break_lines(words, font, size, float("inf"), spec.max_lines) or (words,)
    widest = max(font.width(" ".join(line), size) for line in chosen)
    height = len(chosen) * size * spec.line_spacing + 2 * spec.padding_y
    width = widest + 2 * spec.padding_x
    centre = look.frame.width / 2
    box = Box("headline", centre - width / 2, spec.top, centre + width / 2, spec.top + height)
    return HeadlineLayout(chosen, size, fits, box)


def _break_lines(
    words: tuple[str, ...], font: Font, size: float, max_width: float, max_lines: int
) -> tuple[tuple[str, ...], ...] | None:
    """Fewest lines that fit; for two lines, the most even split. None if nothing fits."""
    if not words:
        return ((),)
    if font.width(" ".join(words), size) <= max_width:
        return (words,)
    if max_lines < 2 or len(words) < 2:
        return None
    best: tuple[float, tuple[tuple[str, ...], ...]] | None = None
    for cut in range(1, len(words)):
        first, second = words[:cut], words[cut:]
        widest = max(font.width(" ".join(first), size), font.width(" ".join(second), size))
        if widest <= max_width and (best is None or widest < best[0]):
            best = (widest, (first, second))
    return best[1] if best else None


def pick_key_word(words: list[str]) -> int:
    """Index of the word to highlight when none was chosen: the longest one."""
    plain = [re.sub(r"\W", "", w) for w in words]
    return max(range(len(words)), key=lambda i: (len(plain[i]), -i)) if words else -1


def _headline_events(
    layout: HeadlineLayout, look: Look, key_word: str | None, end: float
) -> list[str]:
    spec = look.headline
    font = get_font(spec.font)
    size = layout.size
    flat = [w for line in layout.lines for w in line]
    key_index = pick_key_word(flat)
    if key_word:
        wanted = re.sub(r"\W", "", key_word).lower()
        matches = [i for i, w in enumerate(flat) if re.sub(r"\W", "", w).lower() == wanted]
        key_index = matches[0] if matches else key_index

    events: list[str] = []
    outline_style = look.headline_style == "outline"
    text_colour, _ = ass_color("#FFFFFF" if outline_style else spec.text_color)
    accent, _ = ass_color(look.accent)
    if not outline_style:
        box_colour, _ = ass_color(spec.box_color)
        box = layout.box
        path = rounded_rect(box.x1 - box.x0, box.y1 - box.y0, spec.radius)
        tags = f"\\an7\\pos({box.x0:.1f},{box.y0:.1f})\\bord0\\shad0\\1c{box_colour}\\p1"
        events.append(_event(0, end, 0, tags, path))

    cap = font.cap_height(size)
    space = font.width(" ", size)
    slot = size * spec.line_spacing
    index = 0
    for row, line in enumerate(layout.lines):
        line_width = font.width(" ".join(line), size)
        x = look.frame.width / 2 - line_width / 2
        baseline = spec.top + spec.padding_y + row * slot + slot / 2 + cap / 2
        top = baseline - font.ascent(size)
        for word in line:
            width = font.width(word, size)
            colour = text_colour
            if index == key_index:
                if outline_style:
                    colour = accent
                else:
                    pad_x, pad_y = size * 0.12, size * 0.14
                    patch = rounded_rect(width + 2 * pad_x, cap + 2 * pad_y, size * 0.16)
                    tags = (
                        f"\\an7\\pos({x - pad_x:.1f},{baseline - cap - pad_y:.1f})"
                        f"\\bord0\\shad0\\1c{accent}\\p1"
                    )
                    events.append(_event(0, end, 1, tags, patch))
            border = (
                f"\\bord{spec.outline}\\3c&H000000&\\shad0" if outline_style else "\\bord0\\shad0"
            )
            tags = f"\\an7\\pos({x:.1f},{top:.1f}){_font_tags(font, size)}\\1c{colour}{border}"
            events.append(_event(0, end, 2, tags, ass_escape(word)))
            x += width + space
            index += 1
    return events


# --- captions ------------------------------------------------------------------


def display_word(text: str, uppercase: bool) -> str:
    """How a spoken word is shown in captions: no trailing commas or full stops."""
    shown = text.strip().rstrip(".,;:")
    return shown.upper() if uppercase else shown


def build_caption_blocks(
    words: tuple[Word, ...],
    look: Look,
    duration: float | None = None,
    mask: re.Pattern | None = None,
) -> list[CaptionBlock]:
    """Group words into caption blocks (SPEC §10.5).

    A new block starts after a pause, after the end of a phrase, when the block
    is full, or when another word would not fit on the lines available. Blocks
    then get their on-screen times: at least ``min_seconds`` where the next
    block leaves room.
    """
    spec = look.caption
    font = get_font(spec.font)
    limit = spec.max_width

    def fits(group: list[Word]) -> bool:
        return (
            _caption_lines([display_word(w.text, spec.uppercase) for w in group], font, look)[1]
            <= limit
        )

    shown = [
        Word(mask_text(w.text, mask), w.start, w.end)
        for w in words
        if display_word(w.text, spec.uppercase)
        and w.end > 0
        and (duration is None or w.start < duration)
    ]
    groups: list[list[Word]] = []
    for word in shown:
        current = groups[-1] if groups else None
        if current:
            previous = current[-1]
            paused = word.start - previous.end > spec.pause_split
            phrase_done = (
                previous.text.strip()[-1:] in _PHRASE_END and len(current) >= spec.min_words
            )
            full = len(current) >= spec.max_words
            if paused or phrase_done or full or not fits([*current, word]):
                current = None
        if current is None:
            groups.append([word])
        else:
            current.append(word)

    # A lone word joins its neighbour when they are spoken together and there is room.
    merged: list[list[Word]] = []
    for group in groups:
        previous = merged[-1] if merged else None
        if (
            previous
            and (len(group) < spec.min_words or len(previous) < spec.min_words)
            and len(previous) + len(group) <= spec.max_words
            and group[0].start - previous[-1].end <= spec.pause_split
            and fits(previous + group)
        ):
            previous.extend(group)
        else:
            merged.append(list(group))

    return _time_blocks(merged, look, font, duration)


def _time_blocks(
    groups: list[list[Word]], look: Look, font: Font, duration: float | None
) -> list[CaptionBlock]:
    """Give each block its on-screen time, never less than ``min_seconds``.

    A block normally appears with its first word and stays a moment after its
    last. In fast speech the next block would cut it short, so later blocks are
    pushed back just enough; at the very end of the clip the last blocks are
    pulled forward instead.
    """
    spec = look.caption
    minimum = float(spec.min_seconds)
    starts = [max(group[0].start, 0.0) for group in groups]
    for i in range(1, len(starts)):
        starts[i] = max(starts[i], starts[i - 1] + minimum)
    if duration is not None:
        for i in range(len(starts) - 1, -1, -1):
            latest = (duration if i == len(starts) - 1 else starts[i + 1]) - minimum
            starts[i] = max(min(starts[i], latest), 0.0)

    blocks: list[CaptionBlock] = []
    for i, group in enumerate(groups):
        following = starts[i + 1] if i + 1 < len(groups) else duration
        end = max(group[-1].end + spec.hold, starts[i] + minimum)
        if following is not None:
            end = min(end, following)
        display = tuple(Word(display_word(w.text, spec.uppercase), w.start, w.end) for w in group)
        lines, _width = _caption_lines([w.text for w in display], font, look)
        blocks.append(CaptionBlock(display, starts[i], max(end, starts[i]), lines))
    return blocks


def _caption_lines(
    texts: list[str], font: Font, look: Look
) -> tuple[tuple[tuple[int, ...], ...], float]:
    """Split a block over one or two lines; return the lines and the widest line's width."""
    spec = look.caption
    size = float(spec.size)

    def width(indexes: range | tuple[int, ...]) -> float:
        if not indexes:
            return 0.0
        gaps = (len(indexes) - 1) * (font.width(" ", size) + spec.word_gap)
        plain = sum(font.width(texts[i], size) for i in indexes) + gaps
        # Either end word may be the enlarged one; allow for both, plus the outline.
        ends = font.width(texts[indexes[0]], size) + font.width(texts[indexes[-1]], size)
        return plain + ends * (spec.active_scale - 1) / 2 + 2 * spec.outline + 4

    everything = tuple(range(len(texts)))
    if width(everything) <= spec.max_width or len(texts) < 2 or spec.max_lines < 2:
        return (everything,), width(everything)
    best = min(
        (
            (max(width(everything[:cut]), width(everything[cut:])), cut)
            for cut in range(1, len(texts))
        ),
        key=lambda item: item[0],
    )
    return (everything[: best[1]], everything[best[1] :]), best[0]


def _caption_events(block: CaptionBlock, look: Look, layout: str) -> tuple[list[str], Box]:
    spec = look.caption
    font = get_font(spec.font)
    size = float(spec.size)
    white, _ = ass_color(spec.color)
    accent, _ = ass_color(look.accent)
    y_centre = float(spec.y_center[layout])
    line_step = size * spec.line_spacing
    space = font.width(" ", size) + spec.word_gap
    base = (
        f"{_font_tags(font, size)}\\bord{spec.outline}\\shad{spec.shadow}"
        "\\3c&H000000&\\4c&H000000&\\4a&H80&\\be1"
    )
    scale = round(spec.active_scale * 100)

    events: list[str] = []
    x0 = y0 = float("inf")
    x1 = y1 = float("-inf")
    rows = len(block.lines)
    for row, indexes in enumerate(block.lines):
        # The last line sits on y_centre; an extra line goes above it, never below.
        centre_y = y_centre - (rows - 1 - row) * line_step
        widths = [font.width(block.words[i].text, size) for i in indexes]
        total = sum(widths) + space * (len(indexes) - 1)
        x = look.frame.width / 2 - total / 2
        for i, width in zip(indexes, widths, strict=True):
            word = block.words[i]
            centre_x = x + width / 2
            text = ass_escape(word.text)
            pos = f"\\an5\\pos({centre_x:.1f},{centre_y:.1f})"
            lit_from = min(max(word.start, block.start), block.end)
            later = [block.words[j].start for j in range(i + 1, len(block.words))]
            lit_until = min(max(later[0] if later else block.end, lit_from), block.end)
            if lit_from > block.start:
                events.append(_event(block.start, lit_from, 3, f"{pos}{base}\\1c{white}", text))
            if lit_until > lit_from:
                tags = f"{pos}{base}\\1c{accent}\\fscx{scale}\\fscy{scale}"
                events.append(_event(lit_from, lit_until, 4, tags, text))
            if block.end > lit_until:
                events.append(_event(lit_until, block.end, 3, f"{pos}{base}\\1c{white}", text))
            grown = width * spec.active_scale / 2 + spec.outline
            x0, x1 = min(x0, centre_x - grown), max(x1, centre_x + grown)
            x += width + space
        half = font.line_height(size) * spec.active_scale / 2 + spec.outline
        y0, y1 = min(y0, centre_y - half), max(y1, centre_y + half + spec.shadow)
    return events, Box("caption", x0, y0, x1, y1)


# --- the whole file --------------------------------------------------------------


def build_overlay(
    *,
    look: Look,
    layout: str,
    duration: float,
    headline: str | None,
    login: str,
    words: tuple[Word, ...] = (),
    key_word: str | None = None,
    disclosure: str | None = None,
    mask: re.Pattern | None = None,
) -> Overlay:
    """Build the ASS file for one render and report where every piece of text sits."""
    overlay = Overlay(ass="")
    events: list[str] = []
    centre = look.frame.width / 2
    below = float(look.headline.top)

    if headline and headline.strip():
        shape = layout_headline(headline.strip(), look)
        overlay.headline_size, overlay.headline_fits = shape.size, shape.fits
        overlay.headline_lines = len(shape.lines)
        overlay.boxes.append(shape.box)
        events += _headline_events(shape, look, key_word, duration)
        below = shape.box.y1

    def line_of_text(
        name: str, spec: Look, text: str, top: float, end: float, opacity: float
    ) -> float:
        font = get_font(spec.font)
        size = float(spec.size)
        colour, alpha = ass_color(spec.color, opacity)
        tags = (
            f"\\an8\\pos({centre:.1f},{top:.1f}){_font_tags(font, size)}\\1c{colour}\\1a{alpha}"
            "\\bord2\\3c&H000000&\\3a&H60&\\shad0"
        )
        events.append(_event(0, end, 2, tags, ass_escape(text)))
        half = font.width(text, size) / 2 + 2
        bottom = top + font.line_height(size)
        overlay.boxes.append(Box(name, centre - half, top, centre + half, bottom))
        return bottom

    credit = look.credit.text.format(login=login)
    if credit:
        below = line_of_text(
            "credit", look.credit, credit, below + look.credit.gap, duration, look.credit.opacity
        )
    if disclosure:
        seconds = min(float(look.disclosure.seconds), duration)
        line_of_text(
            "disclosure", look.disclosure, disclosure, below + look.disclosure.gap, seconds, 1.0
        )

    overlay.blocks = build_caption_blocks(words, look, duration, mask)
    for block in overlay.blocks:
        if block.end <= block.start:
            continue
        block_events, box = _caption_events(block, look, layout)
        events += block_events
        overlay.boxes.append(box)

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {look.frame.width}\n"
        f"PlayResY: {look.frame.height}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Montserrat,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,"
        "0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    overlay.ass = header + "\n".join(events) + "\n"
    return overlay
