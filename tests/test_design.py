"""The design system (static/style.css) holds: cascade layers in order, one token set, the views (cad.css,
cadmap.css, settings.css, station.css, health.css) only lay out on the tokens, every component the pages and scripts use is defined
once in the components layer, and every class they use exists. tools/ui_consistency.py checks the rendered
result in a browser; this is the static half (no browser)."""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
VIEWS = ("cad.css", "cadmap.css", "settings.css", "station.css", "health.css")
LAYERS = ["vendor", "reset", "tokens", "base", "components", "views", "utilities"]

# The components (style.css @layer components): the vocabulary the views and scripts build with.
COMPONENTS = {
    "btn", "nav", "primary", "danger", "quiet", "danger-quiet", "sm", "mini", "lg", "icon", "wide", "stat", "play", "menu-btn",
    "live-btn", "alerts-btn", "field", "check", "sw", "knob", "seg", "tabs", "tab", "vertical", "chip", "chips", "pill",
    "badge", "st-badge", "why", "feed-ev", "town", "pc-badge", "tg-badge", "talk-badge", "unit-tag", "talk-chip", "card",
    "card-head", "sec-head", "view-head", "view-title", "list", "item", "meta", "txt", "sugg", "evtable", "tscroll",
    "empty", "loading", "error", "notice", "warnbox", "banner", "help", "help-pop", "popover", "alerts-panel", "overlay",
    "dialog", "dialog-head", "dialog-title", "dialog-body", "dialog-foot", "toasts", "toast", "ico", "inc-marker",
    "menu", "menu-item", "menu-mark", "menu-text", "menu-hint", "menu-sep", "menu-back",
    "paging-live", "paging-row", "page-dept",
}

# Raw lengths a view may still write for a size, each for a reason: the map's own marks are drawn a step
# under the smallest type token (a rig chip's tag, the far-zoom chips).
RAW_ALLOWED = {
    ("cadmap.css", "calc(var(--fs-2xs) - 3px)"),
    ("cadmap.css", "calc(var(--fs-2xs) - 2px)"),
}
SIZE_PROPS = re.compile(r"^(font|font-size|line-height|(min-|max-)?(height|block-size)|padding(-[a-z-]+)?)$")

# Classes the scripts and the page use as hooks or states only (looked up, toggled, or data), not styled.
HOOKS = {
    "console-only", "board-only", "ro-ok", "in-settings", "modal", "stale", "ro-dispatch", "ro-settings", "access", "unset",
    "boot", "active", "blocked", "on", "off", "open", "folded", "fold", "collapsed", "sel", "warn", "ok", "err", "info",
    "talking", "talked", "ended", "rig", "disp", "src", "dp", "ev", "rt", "ou", "rc", "slot", "s-unit", "s-kind", "s-count",
    "lbl", "caret", "dot", "ring", "r2", "n", "tag", "car", "k", "q", "t", "unit", "hint", "num", "loc", "flow", "chans",
    "tones", "towns", "units", "users", "given", "available", "pending", "added", "deleted", "retired", "edited",
    "default", "manual", "queued", "nonemerg", "closed", "dismissed", "cleared", "oos", "main", "norec", "confirm",
    "mixed", "gsw", "held", "elsewhere", "absent", "radio", "state", "rp-unit", "rp-source", "rp-error", "rp-scroll",
    "rc-groups", "radios-panel", "s-tsrow", "s-tsrows", "s-tsnow", "cm-focus", "cm-pop-head", "cm-overlay", "look-unset",
    "dispatcher", "upgrade", "dispatch", "note", "status", "system", "attach", "comment", "busy", "times", "pc", "feed",
    "feed-row", "uf", "ask", "pick", "inline", "row", "spacer", "dim", "small", "mono",
    "s-sample", "s-used", "s-meta", "sm-save", "sm-test", "sm-lab", "ap-unit", "unit-code", "span2", "qi", "urow",
    "ov-card", "ov-ne", "ov-page", "cm-btn", "cm-rig", "cm-pin", "sg-l", "db", "rp-edit", "joint", "spare", "dept",
    "addon", "unknown", "live", "src-confirmed", "src-guessed", "src-heard", "s-state", "s-flag", "s-confirm", "s-kind",
    "signin", "action", "refused", "no", "one", "ret", "unk", "eta", "geo", "d-addr", "big", "notx", "txd", "ek",
    "fold", "ugroup", "ulist", "uh", "un", "utitle", "ut", "usub", "inc-link", "ubtns", "ouh", "qh", "qn", "qt", "qe",
    "qa", "qs", "ptype", "rid", "rigtag", "chip", "pages", "log", "radio-log", "other", "og", "og-head", "og-calls",
    "og-name", "og-n", "og-last", "og-snip", "muted", "clamp", "lists", "s-num", "s-color", "db-body", "db-panel",
    "feed-tog", "inc", "ov-head", "ptext", "rv-head", "rv-list", "s-tabs", "tg-gchip", "tg-heard", "unit-groups",
    "alt-tog", "pa-dismiss", "tg-eng", "page",
}


def css(name):
    return (STATIC / name).read_text()


def strip_comments(text):
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def layer_block(text, name):
    """The text of `@layer name { ... }` (balanced braces)."""
    m = re.search(rf"@layer {name}\s*\{{", text)
    assert m, f"no @layer {name}"
    depth, i = 1, m.end()
    while depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[m.end():i - 1]


def declarations(text):
    """(selector, property, value) for every declaration, nested at-rules included."""
    out, stack, buf = [], [], ""
    for ch in strip_comments(text):
        if ch == "{":
            stack.append(buf.strip())
            buf = ""
        elif ch == "}":
            for d in buf.split(";"):
                if ":" in d and stack:
                    p, v = d.split(":", 1)
                    out.append((stack[-1], p.strip(), v.strip()))
            buf = ""
            if stack:
                stack.pop()
        elif ch == ";" and stack:
            d = buf
            if ":" in d:
                p, v = d.split(":", 1)
                out.append((stack[-1], p.strip(), v.strip()))
            buf = ""
        else:
            buf += ch
    return out


def classes_in_css(text):
    return set(re.findall(r"\.([a-zA-Z][\w-]*)", re.sub(r"url\([^)]*\)|\d*\.\d+[a-z%]*", "", strip_comments(text))))


def test_layers_declared_once_in_order_and_the_views_in_theirs():
    style = strip_comments(css("style.css"))
    first = re.search(r"@layer ([\w, ]+);", style)
    assert first and [x.strip() for x in first.group(1).split(",")] == LAYERS
    assert style.index(first.group(0)) < style.index("@import")       # the order is set before anything lands in a layer
    assert '@import url("/static/vendor/maplibre-gl.css") layer(vendor);' in style
    assert "maplibre-gl.css" not in (STATIC / "cad.html").read_text()    # one way in: the vendor layer
    for name in ("reset", "tokens", "base", "components", "utilities"):
        layer_block(style, name)
    for v in VIEWS:
        text = strip_comments(css(v)).strip()
        assert text.startswith("@layer views {") and text.endswith("}"), f"{v}: everything inside @layer views"
        assert layer_block(text, "views").strip(), v
        assert ":root" not in text, f"{v}: tokens live in style.css"


def test_views_use_tokens_for_sizes():
    """font-size, line-height, heights and padding in a view come from the tokens (var(...)), never a raw length."""
    bad = []
    for v in VIEWS:
        for sel, prop, val in declarations(css(v)):
            if not SIZE_PROPS.match(prop) or (v, val) in RAW_ALLOWED:
                continue
            raw = re.sub(r"var\([^()]*\)", "", val)
            if re.search(r"(?<![\w-])\d*\.?\d+(px|em|rem|pt)\b", raw):
                bad.append(f"{v}: {sel} {{ {prop}: {val} }}")
    assert not bad, "raw sizes in views:\n" + "\n".join(bad)
    for v in VIEWS:                                              # no viewport-scaled type anywhere
        for sel, prop, val in declarations(css(v)):
            if prop in ("font-size", "font"):
                assert not re.search(r"v[wh]\b|vmin|vmax|clamp\(", val), f"{v}: {sel}"


def test_one_token_set():
    tokens = layer_block(strip_comments(css("style.css")), "tokens")
    for t in ("--fs-2xs", "--fs-xs", "--fs-sm", "--fs-md", "--fs-lg", "--fs-xl", "--fs-2xl",
              "--lh-2xs", "--lh-md", "--lh-2xl", "--ctl-sm", "--ctl", "--ctl-lg", "--tag-h", "--ico-sm", "--ico", "--ico-lg",
              "--radius-s", "--radius", "--bw", "--focus-w", "--elev-0", "--elev-1", "--elev-dialog", "--z-sticky",
              "--z-popover", "--z-overlay", "--z-toast", "--dur-1", "--dur-2", "--ease", "--zoom", "--gutter", "--rail-w",
              "--side-w", "--hdr-h", *(f"--s-{i}" for i in range(9))):
        assert re.search(rf"{t}:\s", tokens), t
    # type in rem (times the board's zoom), space on the 4 px grid, controls 24 / 28 / 36
    for i, rem in zip(("2xs", "xs", "sm", "md", "lg", "xl", "2xl"), ("0.6875", "0.75", "0.8125", "0.875", "1", "1.25", "1.5")):
        assert f"--fs-{i}: calc({rem}rem * var(--zoom));" in tokens
    for i in range(1, 9):
        assert f"--s-{i}: calc({4 * i}px * var(--zoom));" in tokens
    assert "--ctl-sm: calc(24px" in tokens and "--ctl: calc(28px" in tokens and "--ctl-lg: calc(36px" in tokens
    coarse = re.search(r"@media \(pointer: coarse\)\s*\{(.*?)\}\s*\}", tokens, re.S).group(1)
    for t in ("--ctl-sm", "--ctl", "--ctl-lg"):
        assert int(re.search(rf"{t}: (\d+)px", coarse).group(1)) >= 44, t
    assert "prefers-reduced-motion" in tokens
    # the board: one step, one variable, on its main area only
    cad = css("cad.css")
    assert "html.board .cad-main { --zoom: 1.25; }" in cad
    assert not re.search(r"html\.board[^{]*\{[^}]*font-size", strip_comments(cad))
    for v in VIEWS:                                  # every color, size and space from style.css
        assert "--fs:" not in css(v) and not re.search(r"^\s*--(fs|s|ctl|radius)[\w-]*:", css(v), re.M), v


def test_focus_ring_and_targets():
    style = strip_comments(css("style.css"))
    base = layer_block(style, "base")
    assert ":focus-visible" in base and "outline: var(--focus-w) solid var(--focus)" in base
    comps = layer_block(style, "components")
    # every button, field and tab is at least the small control height (24 px; 44 px on touch)
    for sel in (r":where\(button:not", r":where\(\.sm, \.mini\)", r":where\(\.tab\)(?= \{)", r":where\(input:not[^{]*?select\)(?= \{)",
                r":where\(button\.help\)", r":where\(label\.check, \.check\)", r":where\(\.sw\)"):
        m = re.search(sel + r"[^{]*\{([^}]*)\}", comps, re.S)
        assert m and re.search(r"(min-)?block-size: var\(--ctl(-sm)?\)", m.group(1)), sel

    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    tok = dict(re.findall(r"^\s+--([\w-]+):\s*(#[0-9A-Fa-f]{6});", style, re.M))
    for bg in ("bg", "panel", "raised", "hover", "pressed"):           # the ring: 3:1 against every surface (1.4.11)
        hi, lo = sorted((lum(tok["focus"]), lum(tok[bg])), reverse=True)
        assert (hi + 0.05) / (lo + 0.05) >= 3, bg
    for fg in ("text", "text-2", "dim"):                              # text on the pressed surface: AA
        hi, lo = sorted((lum(tok[fg]), lum(tok["pressed"])), reverse=True)
        assert (hi + 0.05) / (lo + 0.05) >= 4.5, fg


def used_classes():
    """Class names the page and the scripts put on elements."""
    found = set()
    html = "".join((STATIC / page).read_text() for page in ("cad.html", "station.html"))
    for m in re.findall(r'class="([^"]*)"', html):
        found |= set(m.split())
    for js in STATIC.glob("*.js"):
        if js.name in ("map-style.js",):
            continue
        t = js.read_text()
        for m in re.findall(r'\b(?:el|h|make)\(\s*"[a-z0-9]+",\s*"([^"]*)"', t):
            found |= set(m.split())
        for m in re.findall(r'\b(?:el|h|make)\(\s*"[a-z0-9]+",\s*`([^`]*)`', t):
            found |= set(re.sub(r"\$\{[^}]*\}", " ", m).split())
        for m in re.findall(r'class(?:Name|List\.(?:add|toggle|remove))\s*[=(]\s*"([^"]*)"', t):
            found |= set(m.split())
    return {c for c in found if re.fullmatch(r"[a-z][\w-]*[a-z0-9]", c)}


def test_components_are_defined_once_in_the_components_layer():
    style = strip_comments(css("style.css"))
    comps = classes_in_css(layer_block(style, "components"))
    missing = sorted(c for c in COMPONENTS if c not in comps)
    assert not missing, f"components not in @layer components: {missing}"
    used = used_classes()
    assert used & COMPONENTS >= {"primary", "chip", "card", "card-head", "view-head", "item", "notice", "dialog", "tab", "field"}
    # the old one-offs are gone: one name per component
    for gone in ("tg-panel", "tg-head", "tg-title", "tg-close", "s-back", "rc-panel", "radios-dialog", "back"):
        assert gone not in used, gone
        assert not re.search(rf"\.{gone}\b", "".join(css(f) for f in ("style.css", *VIEWS))), gone


def test_every_class_the_page_uses_exists():
    defined = set()
    for f in ("style.css", *VIEWS):
        defined |= classes_in_css(css(f))
    stray = sorted(c for c in used_classes() - defined - HOOKS if not c.startswith("maplibregl"))
    assert not stray, f"classes used but defined nowhere (style them, or list them in HOOKS): {stray}"


# -- filled states: readable text on the fill (GitHub #116) ---------------------------------------------------

FILLS = ("ok", "ok-text", "warn", "err", "info", "accent")     # the state and accent colors a pill, badge or button is filled with
# Under 4.5:1 but not the fill's hue, known: the map's "down" badge, white on --err (3.1:1; the map layers' work).
LOW_CONTRAST_KNOWN = {("cadmap.css", ".ml-down-badge")}


def color_tokens():
    """Token -> #RRGGBB (the one theme, dark), var() aliases (--on-ok: var(--on-fill)) resolved."""
    raw = dict(re.findall(r"^\s+--([\w-]+):\s*([^;]+);", layer_block(strip_comments(css("style.css")), "tokens"), re.M))

    def res(v, depth=0):
        m = re.fullmatch(r"var\(--([\w-]+)\)", v.strip())
        return res(raw[m.group(1)], depth + 1) if m and m.group(1) in raw and depth < 10 else v.strip()
    return {k: res(v) for k, v in raw.items() if re.fullmatch(r"#[0-9A-Fa-f]{6}", res(v))}


def filled_rules():
    """(file, selector, text token, fill token) for every rule that fills with a state / accent token and sets
    the text color on it."""
    out = []
    for f in ("style.css", *VIEWS):
        rules: dict[str, dict] = {}
        for sel, prop, val in declarations(css(f)):
            rules.setdefault(sel, {})[prop] = val
        for sel, d in rules.items():
            bg = re.fullmatch(r"var\(--([\w-]+)\)", d.get("background-color", d.get("background", "")).strip())
            fg = re.fullmatch(r"var\(--([\w-]+)\)", d.get("color", "").strip())
            if bg and fg and bg.group(1) in FILLS:
                out.append((f, sel, fg.group(1), bg.group(1)))
    return out


def test_filled_states_never_put_text_in_the_fills_hue():
    import colorsys

    def rgb(h):
        return [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]

    def lum(h):
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in rgb(h)]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

    tok = color_tokens()
    rules = filled_rules()
    sels = {s for _f, s, _c, _b in rules}
    # the ones #116 was about are found (the parse sees them): an open receiver, one open too long, the SDR's job
    assert {":where(.pill.radio.open)", ":where(.pill.radio.open.stuck)", ":where(.badge.ok, .talk-badge)"} <= sels
    assert any(s.startswith(":where(.seg) button:where(.active") for s in sels)
    bad = []
    for f, sel, fg, bg in rules:
        a, b = tok[fg], tok[bg]
        hi, lo = sorted((lum(a), lum(b)), reverse=True)
        ratio = (hi + 0.05) / (lo + 0.05)
        ha, la, sa = colorsys.rgb_to_hls(*rgb(a))
        hb, _lb, _sb = colorsys.rgb_to_hls(*rgb(b))
        same_hue = sa > 0.3 and min(abs(ha - hb), 1 - abs(ha - hb)) < 30 / 360
        if fg == bg or same_hue or (ratio < 4.5 and (f, sel) not in LOW_CONTRAST_KNOWN):
            bad.append(f"{f}: {sel} {{ color: --{fg} {a}; background: --{bg} {b} }} contrast {ratio:.1f}")
    assert not bad, "filled states with text in the fill's hue or under 4.5:1:\n" + "\n".join(bad)
    # the text tokens for the fills: dark on the light state colors
    for on, fill in (("on-ok", "ok"), ("on-warn", "warn")):
        assert lum(tok[on]) < 0.05 and lum(tok[fill]) > 0.3, on


def test_state_utilities_never_recolor_a_filled_pill_or_badge():
    # The utilities layer wins over every component whatever the specificity: `.ok { color: var(--ok) }` there
    # turned the text of an open receiver (class "pill radio ok open") green on its green fill (#116).
    util = layer_block(strip_comments(css("style.css")), "utilities")
    for sel, prop, _val in declarations(util):
        for part in re.split(r",(?![^(]*\))", sel):          # top-level commas only
            if prop == "color" and re.match(r"\s*\.(ok|warn|err|info)\b", part):
                assert re.search(r":not\([^)]*\.pill\b[^)]*\.badge\b", part), part
    js = (STATIC / "radio-controls.js").read_text()
    assert "pill radio ${state}" in js          # the receiver pill carries its state class ("ok"): see above
