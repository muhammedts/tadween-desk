"""Quiet Desk theme: design tokens, QPalette, the style sheet and recoloured local
SVG icons.

Everything comes from files in `ui/assets` shipped with the app (tokens.json, app.qss,
icons/*.svg, all original to this project); nothing is downloaded and only Windows
system fonts are named. Colours are read from the tokens at paint time, so a theme
switch repaints custom-drawn rows without a restart.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import QAbstractButton, QApplication, QLabel, QWidget

ASSETS = Path(__file__).resolve().parent / "assets"
TOKENS_PATH = ASSETS / "tokens.json"
QSS_PATH = ASSETS / "app.qss"
ICON_DIR = ASSETS / "icons"

THEME_CHOICES = ("system", "light", "dark")
THEME_LABELS = {"system": "Match Windows", "light": "Light", "dark": "Dark"}
SETTING_KEY = "theme"
_PLACEHOLDER = re.compile(r"\{([cz]):([A-Za-z0-9.]+)\}")
_ICON_PROP = "mm_icon"  # "name|role|px" on widgets whose icon follows the theme


@lru_cache(maxsize=1)
def tokens() -> dict[str, Any]:
    return json.loads(TOKENS_PATH.read_text(encoding="utf-8"))


def current() -> str:
    """The theme in use ("light" or "dark")."""
    app = QGuiApplication.instance()
    value = app.property("mm_theme") if app is not None else None
    return value if value in ("light", "dark") else "light"


def color(key: str, theme: str | None = None) -> QColor:
    value = tokens()["color"][theme or current()][key]
    m = re.fullmatch(r"rgba\((\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)", value)
    if m:
        r, g, b, a = m.groups()
        return QColor(int(r), int(g), int(b), round(float(a) * 255))
    return QColor(value)


def size(key: str) -> int:
    return int(tokens()["size"][key])


def parse_choice(value: Any) -> str:
    return value if value in THEME_CHOICES else "system"


def resolve_theme(choice: str) -> str:
    """'system' follows Windows (QStyleHints.colorScheme)."""
    if choice in ("light", "dark"):
        return choice
    scheme = QGuiApplication.styleHints().colorScheme()
    return "dark" if scheme == Qt.ColorScheme.Dark else "light"


def build_palette(theme: str) -> QPalette:
    def c(k: str) -> QColor:
        return color(k, theme)

    p = QPalette()
    roles = {
        QPalette.ColorRole.Window: c("bg.canvas"),
        QPalette.ColorRole.WindowText: c("text.primary"),
        QPalette.ColorRole.Base: c("bg.surface"),
        QPalette.ColorRole.AlternateBase: c("bg.surfaceAlt"),
        QPalette.ColorRole.Text: c("text.primary"),
        QPalette.ColorRole.PlaceholderText: c("text.tertiary"),
        QPalette.ColorRole.Button: c("bg.surface"),
        QPalette.ColorRole.ButtonText: c("text.primary"),
        QPalette.ColorRole.Highlight: c("bg.selectedStrong"),
        QPalette.ColorRole.HighlightedText: c("text.primary"),
        QPalette.ColorRole.Link: c("accent.text"),
        QPalette.ColorRole.LinkVisited: c("accent.text"),
        QPalette.ColorRole.ToolTipBase: c("text.primary"),
        QPalette.ColorRole.ToolTipText: c("bg.surface"),
        QPalette.ColorRole.Accent: c("accent.bg"),
        QPalette.ColorRole.Mid: c("border.default"),
        QPalette.ColorRole.Midlight: c("border.subtle"),
        QPalette.ColorRole.Dark: c("border.strong"),
    }
    for role, col in roles.items():
        p.setColor(role, col)
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, c("text.disabled"))
    return p


def render_qss(theme: str, template: str | None = None) -> str:
    """The style sheet for a theme. Raises ValueError if any {c:…}/{z:…} placeholder is
    unknown or left unresolved, so a token typo can never ship as broken CSS."""
    template = template if template is not None else QSS_PATH.read_text(encoding="utf-8")
    cols = tokens()["color"][theme]
    sizes = tokens()["size"]

    def sub(m: re.Match[str]) -> str:
        kind, key = m.group(1), m.group(2)
        table = cols if kind == "c" else sizes
        if key not in table:
            raise ValueError(f"unknown token placeholder in QSS: {{{kind}:{key}}}")
        return str(table[key])

    template = re.sub(r"/\*.*?\*/", "", template, flags=re.S)  # comments may mention placeholders
    out = _PLACEHOLDER.sub(sub, template)
    if "{c:" in out or "{z:" in out:
        raise ValueError("unresolved token placeholder in QSS")
    return out


def _font(families_key: str, points: float) -> QFont:
    f = QFont()
    f.setFamilies([name for name in tokens()["font"][families_key] if name not in ("sans-serif", "monospace")])
    f.setPointSizeF(points)
    return f


def ui_font() -> QFont:
    return _font("family.ui", 10.5)  # 14 px at 100 %; scales with Windows display scaling


def transcript_font(rtl: bool = False) -> QFont:
    return _font("family.transcript", 12.0 if rtl else 11.25)  # 16 px Arabic / 15 px Latin


def mono_font(points: float = 9.0) -> QFont:
    f = _font("family.mono", points)
    f.setStyleHint(QFont.StyleHint.Monospace)
    return f


def apply_theme(app: QApplication, choice: str = "system") -> str:
    """Fusion + palette + style sheet + UI font for a theme choice. Safe to call again
    at runtime: Qt re-polishes every widget when the application style sheet changes."""
    theme = resolve_theme(choice)
    qss = render_qss(theme)  # before touching the app: a bad template changes nothing
    if app.property("mm_theme") == theme and app.styleSheet() == qss:
        return theme  # already in force: re-polishing every widget again would only cost time
    if not app.property("mm_fusion"):
        app.setStyle("Fusion")  # Fusion honours QSS + palette consistently; the native style does not
        app.setProperty("mm_fusion", True)
    app.setFont(ui_font())
    app.setPalette(build_palette(theme))
    app.setProperty("mm_theme", theme)
    app.setStyleSheet(qss)
    icon.cache_clear()
    return theme


@lru_cache(maxsize=512)
def _svg(name: str) -> str | None:
    path = ICON_DIR / f"{name}.svg"
    return path.read_text(encoding="utf-8") if path.is_file() else None


@lru_cache(maxsize=512)
def icon(name: str, role: str = "text.primary", px: int = 20, theme: str | None = None) -> QIcon:
    """Recolour icons/<name>.svg (stroke=currentColor) for a colour token, with a
    Disabled mode, at the screen's device pixel ratio (sharp at 100–200 %)."""
    from PySide6.QtSvg import QSvgRenderer

    svg = _svg(name)
    result = QIcon()
    if svg is None:
        return result
    theme = theme or current()
    app = QGuiApplication.instance()
    dpr = max(1.0, app.devicePixelRatio()) if app is not None else 1.0
    for scale in sorted({1.0, dpr, 2.0}):
        for mode, key in ((QIcon.Mode.Normal, role), (QIcon.Mode.Disabled, "text.disabled"),
                          (QIcon.Mode.Selected, role), (QIcon.Mode.Active, role)):
            data = svg.replace("currentColor", tokens()["color"][theme][key])
            renderer = QSvgRenderer(QByteArray(data.encode("utf-8")))
            pm = QPixmap(QSize(round(px * scale), round(px * scale)))
            pm.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pm)
            renderer.render(painter, QRectF(0, 0, pm.width(), pm.height()))
            painter.end()
            pm.setDevicePixelRatio(scale)
            result.addPixmap(pm, mode)
    return result


def set_icon(widget: QWidget, name: str, role: str = "text.primary", px: int = 20) -> None:
    """Give a button (or a label, as a pixmap) a themed icon that follows theme changes."""
    widget.setProperty(_ICON_PROP, f"{name}|{role}|{px}")
    _apply_icon(widget, name, role, px)


def _apply_icon(widget: QWidget, name: str, role: str, px: int) -> None:
    themed = icon(name, role, px)
    if isinstance(widget, QAbstractButton):
        widget.setIcon(themed)
        widget.setIconSize(QSize(px, px))
    elif isinstance(widget, QLabel):
        widget.setPixmap(themed.pixmap(QSize(px, px)))


def refresh_icons(root: QWidget) -> None:
    """Re-render every themed icon below `root` (after a theme change)."""
    for widget in [root, *root.findChildren(QWidget)]:
        spec = widget.property(_ICON_PROP)
        if isinstance(spec, str) and spec.count("|") == 2:
            name, role, px = spec.split("|")
            _apply_icon(widget, name, role, int(px))


def set_props(widget: QWidget, **props: Any) -> None:
    """Set QSS selector properties and re-polish once."""
    changed = False
    for key, value in props.items():
        if widget.property(key) != value:
            widget.setProperty(key, value)
            changed = True
    if changed:
        repolish(widget)


def repolish(widget: QWidget) -> None:
    """Call after changing a dynamic property used in QSS selectors (variant, chip, state…)."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()
