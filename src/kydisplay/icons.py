# Kydisplay — share a Plasma desktop on a wireless display
# Copyright (C) 2026 Kydisplay contributors
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for
# more details.
#
# You should have received a copy of the GNU General Public License along
# with this program. If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The application's own icon, found whether or not it has been installed.

Asking the icon theme is the right first question: an installed Kydisplay
puts its icons where the desktop keeps icons, and a theme that has been
restyled should be allowed to answer for us.

It is not the only question, because a checkout has installed nothing.
Running from source, the theme has never heard of this application, the
lookup comes back empty, and every caller silently falls through to
whatever generic icon it named as a fallback — which is how both the
window and the system tray ended up showing a plain monitor glyph while
the real icon sat in the repository unused. Nothing was broken; nobody
had put the file anywhere the lookup could see.

So the theme is asked first and the shipped files are the answer when it
has nothing. Every size is handed over rather than one: the tray asks for
something small, the window manager for something larger, and a QIcon
holding all of them lets each take what suits instead of scaling one
badly.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtGui import QIcon

APP_ID = "nl.eandf.Kydisplay"

# Where the icons sit relative to this file, both in a checkout
# (<repo>/assets/icons/hicolor) and under an installed tree that keeps
# them beside the package rather than in the desktop's theme.
_RELATIVE_ASSET_ROOTS = (
    Path("..") / ".." / "assets" / "icons" / "hicolor",
    Path("..") / "assets" / "icons" / "hicolor",
)


def _bundled_icon() -> QIcon | None:
    """The icon assembled from the files shipped in the tree, or ``None``
    when they are not where this expects them."""
    here = Path(__file__).resolve().parent
    for relative in _RELATIVE_ASSET_ROOTS:
        root = (here / relative).resolve()
        if not root.is_dir():
            continue
        # Sorted so the smallest is added first, which costs nothing and
        # makes the result the same every run rather than filesystem order.
        files = sorted(
            root.glob(f"*/apps/{APP_ID}.png"),
            key=lambda path: int(path.parent.parent.name.split("x")[0] or 0),
        )
        if not files:
            continue
        icon = QIcon()
        for path in files:
            icon.addFile(str(path))
        return icon
    return None


def app_icon(fallback: str = "video-display") -> QIcon:
    """Kydisplay's icon: the theme's if it has one, the shipped files if
    not, and only then whatever generic name the caller offered."""
    icon = QIcon.fromTheme(APP_ID)
    if not icon.isNull():
        return icon
    bundled = _bundled_icon()
    if bundled is not None and not bundled.isNull():
        return bundled
    return QIcon.fromTheme(fallback)
