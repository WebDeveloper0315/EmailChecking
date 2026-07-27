"""Application theme: complete palettes plus a modern stylesheet.

Why this module exists
----------------------
Setting only a palette is not enough on Windows.  The native *windows11* style
paints some widgets (tool buttons, menu text, headers) with colours taken from
the **operating system's** light/dark setting, not from the application palette.
With Windows in dark mode and the app forced to a light palette, toolbar labels
were drawn white on a white background - invisible.

So the theme is applied as a unit:

1. the **Fusion** style, which honours the palette for everything it draws;
2. a **complete** palette (every role, including the Disabled group);
3. a stylesheet that gives the window its modern look - flat toolbars, rounded
   inputs, slim scrollbars, soft selection - expressed in the same colours.

``system`` follows the OS: Qt 6.5+ reports it through ``styleHints()``, with the
registry as a fallback on Windows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import qt_bootstrap

qt_bootstrap.prepare()

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor, QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

__all__ = ["Palette", "LIGHT", "DARK", "apply", "resolve", "current", "is_dark"]


@dataclass(frozen=True)
class Palette:
    """Every colour the application uses, in one place."""

    name: str
    window: str          # window / toolbar background
    surface: str         # panels, list background
    surface_alt: str     # hovered rows, alternating rows
    base: str            # editable fields
    border: str
    text: str
    text_muted: str
    accent: str
    accent_text: str
    accent_soft: str     # selected row background
    danger: str
    warning_bg: str
    warning_text: str
    info_bg: str
    info_text: str
    shadow: str

    @property
    def is_dark(self) -> bool:
        return QColor(self.window).lightness() < 128


LIGHT = Palette(
    name="light",
    window="#f5f6f8",
    surface="#ffffff",
    surface_alt="#eef1f5",
    base="#ffffff",
    border="#d8dce2",
    text="#1f2328",
    text_muted="#5c6470",
    accent="#2563eb",
    accent_text="#ffffff",
    accent_soft="#dbe7fe",
    danger="#c5221f",
    warning_bg="#fff4d6",
    warning_text="#7a5900",
    info_bg="#e8f0fe",
    info_text="#174ea6",
    shadow="rgba(15, 23, 42, 0.08)",
)

DARK = Palette(
    name="dark",
    window="#1b1d21",
    surface="#212429",
    surface_alt="#2a2e35",
    base="#1a1c20",
    border="#343a42",
    text="#e6e8ea",
    text_muted="#9aa2ad",
    accent="#4f8cf7",
    accent_text="#ffffff",
    accent_soft="#26385c",
    danger="#f2726f",
    warning_bg="#3a2f12",
    warning_text="#f0c674",
    info_bg="#1d2b41",
    info_text="#9dc1fb",
    shadow="rgba(0, 0, 0, 0.4)",
)

_current: Palette = LIGHT


def system_is_dark() -> bool:
    """What the desktop is set to, as far as we can tell."""
    try:
        hints = QApplication.styleHints()
        scheme = hints.colorScheme()          # Qt 6.5+
        if scheme == Qt.ColorScheme.Dark:
            return True
        if scheme == Qt.ColorScheme.Light:
            return False
    except Exception:
        pass
    try:  # Windows fallback
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return value == 0
    except Exception:
        return False


def resolve(theme: str) -> Palette:
    """Map ``light`` / ``dark`` / ``system`` to a concrete palette."""
    if theme == "dark":
        return DARK
    if theme == "light":
        return LIGHT
    return DARK if system_is_dark() else LIGHT


def current() -> Palette:
    return _current


def is_dark() -> bool:
    return _current.is_dark


def _qpalette(colours: Palette) -> QPalette:
    """A complete QPalette - every role, including the Disabled group."""
    palette = QPalette()
    window = QColor(colours.window)
    surface = QColor(colours.surface)
    base = QColor(colours.base)
    text = QColor(colours.text)
    muted = QColor(colours.text_muted)
    accent = QColor(colours.accent)

    palette.setColor(QPalette.Window, window)
    palette.setColor(QPalette.WindowText, text)
    palette.setColor(QPalette.Base, base)
    palette.setColor(QPalette.AlternateBase, QColor(colours.surface_alt))
    palette.setColor(QPalette.ToolTipBase, surface)
    palette.setColor(QPalette.ToolTipText, text)
    palette.setColor(QPalette.PlaceholderText, muted)
    palette.setColor(QPalette.Text, text)
    palette.setColor(QPalette.Button, surface)
    palette.setColor(QPalette.ButtonText, text)
    palette.setColor(QPalette.BrightText, QColor(colours.danger))
    palette.setColor(QPalette.Link, accent)
    palette.setColor(QPalette.LinkVisited, accent.darker(115))
    palette.setColor(QPalette.Highlight, accent)
    palette.setColor(QPalette.HighlightedText, QColor(colours.accent_text))
    palette.setColor(QPalette.Light, QColor(colours.surface_alt))
    palette.setColor(QPalette.Midlight, QColor(colours.border))
    palette.setColor(QPalette.Mid, QColor(colours.border))
    palette.setColor(QPalette.Dark, muted)
    palette.setColor(QPalette.Shadow, QColor(0, 0, 0, 60))

    # The Disabled group is what makes greyed-out text readable in both themes.
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        palette.setColor(QPalette.Disabled, role, muted)
    palette.setColor(QPalette.Disabled, QPalette.Highlight, QColor(colours.surface_alt))
    palette.setColor(QPalette.Disabled, QPalette.HighlightedText, muted)
    return palette


def stylesheet(c: Palette) -> str:
    """The modern look: flat surfaces, rounded inputs, slim scrollbars."""
    return f"""
* {{ outline: 0; }}

QWidget {{
    color: {c.text};
    font-size: 10pt;
}}
QMainWindow, QDialog {{ background: {c.window}; }}

/* ---------------------------------------------------------------- toolbars */
QToolBar {{
    background: {c.window};
    border: 0;
    border-bottom: 1px solid {c.border};
    padding: 5px 8px;
    spacing: 4px;
}}
QToolBar QLabel {{ color: {c.text_muted}; padding: 0 2px; }}
QToolButton {{
    color: {c.text};
    background: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 5px 10px;
}}
QToolButton:hover {{ background: {c.surface_alt}; border-color: {c.border}; }}
QToolButton:pressed {{ background: {c.accent_soft}; }}
QToolButton:checked {{
    background: {c.accent_soft};
    border-color: {c.accent};
    color: {c.text};
}}
QToolButton:disabled {{ color: {c.text_muted}; }}
QToolButton::menu-indicator {{ subcontrol-position: right center; right: 2px; }}

/* ------------------------------------------------------------------ inputs */
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {c.base};
    color: {c.text};
    border: 1px solid {c.border};
    border-radius: 6px;
    padding: 5px 8px;
    selection-background-color: {c.accent};
    selection-color: {c.accent_text};
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus,
QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {c.accent};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
    background: {c.surface_alt};
    color: {c.text_muted};
}}
QComboBox::drop-down {{ border: 0; width: 18px; }}
QComboBox QAbstractItemView {{
    background: {c.surface};
    border: 1px solid {c.border};
    border-radius: 6px;
    selection-background-color: {c.accent_soft};
    selection-color: {c.text};
    padding: 4px;
}}

QPushButton {{
    background: {c.surface};
    color: {c.text};
    border: 1px solid {c.border};
    border-radius: 6px;
    padding: 6px 14px;
}}
QPushButton:hover {{ background: {c.surface_alt}; }}
QPushButton:pressed {{ background: {c.accent_soft}; }}
QPushButton:disabled {{ color: {c.text_muted}; background: {c.surface_alt}; }}
QPushButton:default {{
    background: {c.accent};
    color: {c.accent_text};
    border-color: {c.accent};
}}
QPushButton:default:hover {{ background: {c.accent}; }}
QPushButton:flat {{ background: transparent; border-color: transparent;
                    color: {c.accent}; padding: 4px 6px; }}
QPushButton:flat:hover {{ background: {c.surface_alt}; }}

QCheckBox, QRadioButton {{ spacing: 7px; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 15px; height: 15px;
    border: 1px solid {c.border};
    border-radius: 4px;
    background: {c.base};
}}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {c.accent};
    border-color: {c.accent};
}}

/* ------------------------------------------------------------------- views */
QListView, QTreeWidget, QTreeView, QListWidget {{
    background: {c.surface};
    border: 0;
    border-radius: 0;
    padding: 4px;
}}
QListView, QListWidget {{ show-decoration-selected: 1; }}
/* Trees keep the selection inside the item: extending it over the expander
   column paints a stray block next to the folder name.  The branch itself is
   left unstyled on purpose, so Qt keeps drawing the expand/collapse arrows. */
QTreeView, QTreeWidget {{ show-decoration-selected: 0; }}
QListView::item, QListWidget::item {{
    border-radius: 7px;
    padding: 2px;
    margin: 1px 3px;
}}
QListView::item:hover, QListWidget::item:hover,
QTreeWidget::item:hover {{ background: {c.surface_alt}; }}
QListView::item:selected, QListWidget::item:selected,
QTreeWidget::item:selected {{
    background: {c.accent_soft};
    color: {c.text};
}}
QTreeWidget::item {{ border-radius: 6px; padding: 4px 2px; margin: 1px 2px; }}
QTreeWidget {{ background: {c.window}; }}
QHeaderView::section {{
    background: {c.window};
    color: {c.text_muted};
    border: 0;
    border-bottom: 1px solid {c.border};
    padding: 5px;
}}

/* ---------------------------------------------------------------- chrome */
QSplitter::handle {{ background: {c.border}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}

QStatusBar {{
    background: {c.window};
    border-top: 1px solid {c.border};
    color: {c.text_muted};
}}
QStatusBar::item {{ border: 0; }}
QStatusBar QLabel {{ color: {c.text_muted}; padding: 0 6px; }}

QProgressBar {{
    background: {c.surface_alt};
    border: 0;
    border-radius: 5px;
    height: 8px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{ background: {c.accent}; border-radius: 5px; }}

QScrollBar:vertical {{
    background: transparent; width: 11px; margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {c.border}; border-radius: 5px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {c.text_muted}; }}
QScrollBar:horizontal {{
    background: transparent; height: 11px; margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {c.border}; border-radius: 5px; min-width: 30px;
}}
QScrollBar::handle:horizontal:hover {{ background: {c.text_muted}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QMenu {{
    background: {c.surface};
    border: 1px solid {c.border};
    border-radius: 8px;
    padding: 5px;
}}
QMenu::item {{ padding: 6px 22px 6px 14px; border-radius: 5px; }}
QMenu::item:selected {{ background: {c.accent_soft}; color: {c.text}; }}
QMenu::separator {{ height: 1px; background: {c.border}; margin: 5px 8px; }}

QTabWidget::pane {{
    border: 1px solid {c.border};
    border-radius: 8px;
    top: -1px;
    background: {c.surface};
}}
QTabBar::tab {{
    background: transparent;
    color: {c.text_muted};
    padding: 7px 16px;
    border: 0;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{ color: {c.text}; border-bottom-color: {c.accent}; }}
QTabBar::tab:hover {{ color: {c.text}; }}

QGroupBox {{
    border: 1px solid {c.border};
    border-radius: 8px;
    margin-top: 12px;
    padding: 10px 8px 8px 8px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
    color: {c.text_muted};
}}

QToolTip {{
    background: {c.surface};
    color: {c.text};
    border: 1px solid {c.border};
    border-radius: 6px;
    padding: 5px 7px;
}}
"""


def apply(app: Optional[QApplication], theme: str) -> Palette:
    """Apply a theme to the whole application.  Returns the palette used."""
    global _current
    colours = resolve(theme)
    _current = colours
    if app is None:
        return colours

    # Fusion follows the palette everywhere; the native Windows style does not,
    # which is what made light-on-light text possible.
    app.setStyle("Fusion")
    app.setPalette(_qpalette(colours))
    app.setStyleSheet(stylesheet(colours))
    return colours
