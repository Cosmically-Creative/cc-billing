"""Check WCAG 2.1 color contrast of the theme tokens in templates/billing/app_base.html."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "templates" / "billing" / "app_base.html").read_text(encoding="utf-8")
STYLE = re.search(r"<style[^>]*>(.*?)</style>", HTML, re.S).group(1)
STYLE = re.sub(r"/\*.*?\*/", "", STYLE, flags=re.S)


def read_block(selector):
    """Return the custom properties of the block whose selector is exactly `selector`."""
    body = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", STYLE).group(1)
    return {name: value.strip() for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body)}


def resolve(props, fallback=None):
    """Resolve var(--other) references against the same block, then the fallback block."""
    fallback = fallback or {}
    resolved = dict(props)
    for _ in range(5):  # a few passes so chains like var(a) -> var(b) settle
        changed = False
        for name, value in resolved.items():
            m = re.fullmatch(r"var\((--[\w-]+)(?:,\s*([^)]+))?\)", value.strip())
            if m:
                ref, default = m.groups()
                new = resolved.get(ref) or fallback.get(ref) or (default or "").strip()
                if new and new != value:
                    resolved[name] = new
                    changed = True
        if not changed:
            break
    return resolved


def hex_rgb(value):
    """Parse #RGB or #RRGGBB into (r, g, b) floats in 0..1, or None if not a hex color."""
    m = re.fullmatch(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})", value.strip())
    if not m:
        return None
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def luminance(rgb):
    def lin(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg_rgb, bg_rgb):
    l1, l2 = sorted((luminance(fg_rgb), luminance(bg_rgb)), reverse=True)
    return (l1 + 0.05) / (l2 + 0.05)


LIGHT_RAW = read_block(":root")
DARK_RAW = read_block(':root[data-theme="dark"]')
SYSTEM_RAW = read_block(':root[data-theme="system"]')
VALUES = {
    "light": resolve(LIGHT_RAW, LIGHT_RAW),
    "dark": resolve(DARK_RAW, LIGHT_RAW),
}

EXCLUDED_NON_COLORS = {"--cc-font", "--cc-sidebar-width"}
COLOR_TOKENS = [
    name for name, value in VALUES["light"].items()
    if name.startswith("--cc-") and name not in EXCLUDED_NON_COLORS and hex_rgb(value)
]


def color_of(theme, name):
    value = VALUES[theme].get(name)
    assert value is not None, f"{theme}: missing token {name}"
    rgb = hex_rgb(value)
    assert rgb is not None, f"{theme}: {name} = {value!r} is not a hex color"
    return rgb


def pair_id(theme, fg, bg):
    return f"{theme}-{fg.replace('--cc-', '')}-on-{bg.replace('--cc-', '')}"


def test_dark_blocks_match():
    assert DARK_RAW == SYSTEM_RAW, (
        f'dark block has {sorted(set(DARK_RAW) - set(SYSTEM_RAW))} extra, '
        f'{sorted(set(SYSTEM_RAW) - set(DARK_RAW))} missing, and differs in '
        f'{sorted(k for k in set(DARK_RAW) & set(SYSTEM_RAW) if DARK_RAW[k] != SYSTEM_RAW[k])}'
    )


@pytest.mark.parametrize("name", COLOR_TOKENS, ids=lambda n: f"dark-overrides{n.replace('--cc-', '-')}")
def test_dark_overrides_every_color_token(name):
    assert name in DARK_RAW, f"dark block is missing color token {name}"


TEXT_PAIRS = [
    ("--cc-text", ["--cc-surface-page", "--cc-surface-card", "--cc-surface-raised",
                   "--cc-surface-sidebar", "--cc-input-bg"]),
    ("--cc-text-secondary", ["--cc-surface-page", "--cc-surface-card", "--cc-surface-raised"]),
    ("--cc-link", ["--cc-surface-page", "--cc-surface-card"]),
    ("--cc-link-hover", ["--cc-surface-card"]),
    ("--cc-action-text", ["--cc-action", "--cc-action-hover", "--cc-danger"]),
    ("--cc-selected-text", ["--cc-selected-bg"]),
    ("--cc-error-text", ["--cc-surface-card"]),
]
STATUSES = ["draft", "approved", "sent", "partly-paid", "paid", "overdue", "void", "ledger"]
STATUS_PAIRS = [(f"--cc-status-{s}-fg", f"--cc-status-{s}-bg") for s in STATUSES]
ALL_PAIRS = [
    pytest.param(theme, fg, bg, id=pair_id(theme, fg, bg))
    for theme in ("light", "dark")
    for fg, bgs in TEXT_PAIRS
    for bg in bgs
] + [
    pytest.param(theme, fg, bg, id=pair_id(theme, fg, bg))
    for theme in ("light", "dark")
    for fg, bg in STATUS_PAIRS
]


@pytest.mark.parametrize(("theme", "fg", "bg"), ALL_PAIRS)
def test_text_contrast(theme, fg, bg):
    ratio = contrast(color_of(theme, fg), color_of(theme, bg))
    assert ratio >= 4.5, f"{fg} on {bg}: {ratio:.2f} < 4.5"


@pytest.mark.parametrize("theme", ["light", "dark"], ids=lambda t: f"{t}-text-muted-on-card")
def test_ui_contrast(theme):
    ratio = contrast(color_of(theme, "--cc-text-muted"), color_of(theme, "--cc-surface-card"))
    assert ratio >= 3, f"--cc-text-muted on --cc-surface-card: {ratio:.2f} < 3"


def test_dark_page_is_brand_navy():
    value = VALUES["dark"].get("--cc-surface-page", "")
    assert value.lower() == "#0b1532", f"dark --cc-surface-page is {value!r}, expected #0B1532"
