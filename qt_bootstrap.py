"""Make PySide6 importable on Windows/conda installations.

On this machine (and on most Anaconda/Miniconda setups) importing ``QtCore``
fails with::

    ImportError: DLL load failed while importing QtCore:
    The specified procedure could not be found.

The reason is DLL search order, not a broken install: the interpreter's own
directory and ``%PATH%`` (which conda fills with its ``Library\\bin``) are
searched before the ``PySide6`` package directory, so Qt ends up binding
against conda's older copies of its dependencies.

Loading Qt's core libraries explicitly with ``LOAD_WITH_ALTERED_SEARCH_PATH``
(``winmode=8``) resolves each dependency from the PySide6 directory first; once
those modules are in the process, the extension modules bind to them.

Import this module *before* anything imports PySide6::

    import qt_bootstrap
    qt_bootstrap.prepare()
    from PySide6.QtWidgets import QApplication
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys

logger = logging.getLogger(__name__)

_LOAD_WITH_ALTERED_SEARCH_PATH = 0x00000008

#: Loaded in dependency order; missing files are simply skipped.
_CORE_LIBRARIES = (
    "Qt6Core.dll",
    "Qt6Gui.dll",
    "Qt6Widgets.dll",
    "Qt6Network.dll",
    "Qt6Svg.dll",
    "Qt6PrintSupport.dll",
)

_prepared = False


def prepare() -> bool:
    """Ensure ``from PySide6 import QtWidgets`` works.  Returns True on success."""
    global _prepared
    if _prepared:
        return True

    if _can_import_qtcore():
        _prepared = True
        return True

    if sys.platform != "win32":
        return False

    try:
        import PySide6

        package_dir = os.path.dirname(os.path.abspath(PySide6.__file__))
    except Exception:
        logger.debug("PySide6 is not installed", exc_info=True)
        return False

    # Give Qt's own directory priority for any later, lazily loaded plugin.
    os.environ["PATH"] = package_dir + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory") and os.path.isdir(package_dir):
        try:
            os.add_dll_directory(package_dir)
        except OSError:
            pass

    for name in _CORE_LIBRARIES:
        path = os.path.join(package_dir, name)
        if not os.path.exists(path):
            continue
        try:
            ctypes.WinDLL(path, winmode=_LOAD_WITH_ALTERED_SEARCH_PATH)
        except OSError as exc:
            logger.debug("Could not preload %s: %s", name, exc)

    if _can_import_qtcore():
        logger.info("PySide6 loaded after preloading Qt libraries from %s", package_dir)
        _prepared = True
        return True

    logger.error("PySide6 could not be loaded from %s", package_dir)
    return False


def _can_import_qtcore() -> bool:
    try:
        from PySide6 import QtCore  # noqa: F401

        return True
    except Exception:
        return False


def install_message_handler() -> None:
    """Route Qt's own warnings into the Python logger instead of stderr."""
    try:
        from PySide6 import QtCore
    except Exception:
        return

    levels = {
        QtCore.QtMsgType.QtDebugMsg: logging.DEBUG,
        QtCore.QtMsgType.QtInfoMsg: logging.INFO,
        QtCore.QtMsgType.QtWarningMsg: logging.WARNING,
        QtCore.QtMsgType.QtCriticalMsg: logging.ERROR,
        QtCore.QtMsgType.QtFatalMsg: logging.CRITICAL,
    }

    def handler(mode, context, message) -> None:  # noqa: ANN001 - Qt signature
        logging.getLogger("qt").log(levels.get(mode, logging.INFO), "%s", message)

    QtCore.qInstallMessageHandler(handler)
