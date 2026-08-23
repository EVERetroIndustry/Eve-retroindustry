# -*- mode: python ; coding: utf-8 -*-
#
# EVE Retroindustry - PyInstaller spec
#
# Build:
#   1. python scripts/build_sde_base.py   (creates sde_base.db)
#   2. pyinstaller eve_retroindustry.spec
#
# Output: dist/EVE_Retroindustry/
#   ├── EVE_Retroindustry.exe   (Windows) / EVE_Retroindustry (Linux)
#   ├── sde_base.db             (bundled, used as first-run template)
#   └── ... (Python runtime + deps)
#
# Distribute the entire dist/EVE_Retroindustry/ folder as a ZIP.
# eve_cache.db is created next to the .exe on first run.

import os
import sys
from PyInstaller.utils.hooks import collect_all

block_cipher = None

# pywebview uses Qt (PyQt6-WebEngine) on both Linux and Windows. Self-contained
# bundled Chromium - no system webkit2gtk on Linux, no Edge WebView2 / pythonnet
# on Windows (the default pywebview Windows backend tries to load
# Python.Runtime.dll through pythonnet and silently corrupts under PyInstaller).
#
# We rely on PyInstaller's built-in PyQt6 hook (it sees the imports in
# webview.platforms.qt and pulls only the Qt modules we actually use:
# QtCore, QtGui, QtWidgets, QtNetwork, QtWebEngineCore, QtWebEngineWidgets).
# Collecting all of PyQt6 unconditionally bloats the build by ~400 MB with
# Qt6Quick/QML/3D/Designer modules that we never touch.
_qt_hiddenimports = [
    "PyQt6.QtCore",
    "PyQt6.QtGui",
    "PyQt6.QtWidgets",
    "PyQt6.QtNetwork",
    "PyQt6.QtWebEngineCore",
    "PyQt6.QtWebEngineWidgets",
]

_wv_datas, _wv_binaries, _wv_hiddenimports = collect_all("webview")

a = Analysis(
    ["launcher.py"],
    pathex=["."],
    binaries=_wv_binaries,
    datas=[
        ("app/web/templates", "app/web/templates"),
        ("app/web/static", "app/web/static"),
        ("assets/icon.ico", "assets"),
        # Bundle SDE base - first run copies it to eve_cache.db; later runs
        # use it to refresh SDE tables when the bundled DB has newer/more
        # types than the user's eve_cache.db (e.g. after an in-app update).
        ("sde_base.db", "."),
    ] + _wv_datas,
    hiddenimports=[
        # JWT / auth
        "jwt",
        "jwt.algorithms",
        # uvicorn internals not auto-detected
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "uvicorn.lifespan.off",
        # anyio backend
        "anyio",
        "anyio._backends._asyncio",
        # HTTP
        "httpx",
        "httpcore",
        "httpcore._backends.asyncio",
        "h11",
        # FastAPI / starlette
        "starlette.middleware.cors",
        "multipart",
        "python_multipart",
        # email (used by httpx internally)
        "email.mime.text",
        "email.mime.multipart",
        # PIL kept for any image processing (formerly used by tray icon)
        "PIL",
        "PIL.Image",
    ] + _qt_hiddenimports + _wv_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "pandas",
        "PyQt5",
        "wx",
        "gi",
        # pystray (replaced by pywebview)
        "pystray",
        "Xlib",
        # Block pywebview's Windows fallback chain (winforms / mshtml / edgechromium
        # all go through pythonnet → .NET, which silently corrupts under
        # PyInstaller). With these excluded, if Qt fails to load, pywebview
        # raises a clear "QT cannot be loaded" instead of cascading into a
        # confusing Python.Runtime.dll traceback.
        "pythonnet",
        "clr",
        "clr_loader",
        "webview.platforms.winforms",
        "webview.platforms.mshtml",
        "webview.platforms.edgechromium",
        "webview.platforms.cef",
        "webview.platforms.cocoa",
        "webview.platforms.gtk",
        "webview.platforms.android",
        # Qt modules pulled in transitively but unused by our QWebEngineView
        "PyQt6.QtQml",
        "PyQt6.QtQuick",
        "PyQt6.QtQuickWidgets",
        "PyQt6.Qt3DCore",
        "PyQt6.Qt3DRender",
        "PyQt6.Qt3DInput",
        "PyQt6.Qt3DLogic",
        "PyQt6.Qt3DExtras",
        "PyQt6.QtMultimedia",
        "PyQt6.QtMultimediaWidgets",
        "PyQt6.QtPdf",
        "PyQt6.QtPdfWidgets",
        "PyQt6.QtCharts",
        "PyQt6.QtDataVisualization",
        "PyQt6.QtSensors",
        "PyQt6.QtBluetooth",
        "PyQt6.QtPositioning",
        "PyQt6.QtSerialPort",
        "PyQt6.QtSql",
        "PyQt6.QtTest",
        "PyQt6.QtDesigner",
        "PyQt6.QtHelp",
        "PyQt6.QtSvg",
        "PyQt6.QtXml",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# Post-filter Qt6 binaries/datas: the PyQt6 PyInstaller hook bulk-collects
# the entire Qt6 tree. We only need QtCore, QtGui, QtWidgets, QtNetwork,
# QtWebEngine* and a small set of platform/imageformat/tls plugins. Strip
# everything else.
#
# Naming differs per platform:
#   Linux:   libQt6Core.so.6 in PyQt6/Qt6/lib/
#   Windows: Qt6Core.dll      in PyQt6/Qt6/bin/
# The regex uses `(lib)?` and the path check covers both `/lib/` and `/bin/`.
import re

_qt_lib_keep = re.compile(
    r"(lib)?Qt6(Core|DBus|Gui|Network|OpenGL|OpenGLWidgets|Widgets|PrintSupport|"
    r"WebEngineCore|WebEngineWidgets|WebChannel|Positioning|"
    r"QuickWidgets|Quick|Qml|QmlMeta|QmlModels|QmlWorkerScript|"
    # The Linux window-system backends. These are NOT optional: libqxcb.so
    # needs XcbQpa and libqwayland.so needs WaylandClient, and without them
    # the loader falls back to the HOST's copies. That "worked" only while the
    # host happened to run the same Qt build - the moment the bundled Qt moved
    # from 6.11.1 to 6.11.2 the app died with "no Qt platform plugin could be
    # initialized" (undefined symbol ... QtPrivate_6_11_1). Shipping them is
    # ~2 MB and makes the bundle independent of the host, which is the whole
    # point of a bundle. WlShellIntegration is the wl-shell fallback plugin's
    # library, Svg backs the SVG image/icon plugins, EglFSDeviceIntegration
    # backs the eglfs platform plugin.
    r"XcbQpa|WaylandClient|WlShellIntegration|Svg|EglFSDeviceIntegration)\."
)
# NB: QtWebEngineWidgets pulls in QtPrintSupport (web page printing) and
# QtQuickWidgets. Both must stay or the bundle only runs on machines that
# happen to have system Qt installed. QuickWidgets must precede Quick in the
# alternation is not required (regex backtracks), but keep it explicit.
_qt_other_lib_keep = re.compile(
    r"^(lib)?(icu|avcodec|avformat|avutil|swresample|webp|"
    r"minizip|xslt|xml2|lcms2|ssl|crypto|"
    # Windows-only graphics layer needed by QtWebEngine (ANGLE OpenGL ES
    # → Direct3D translator + software fallback). Without these the
    # WebEngine zygote can't initialize GL context and the import fails
    # at pywebview backend selection time.
    r"EGL|GLESv2|opengl32sw|D3DCompiler|d3dcompiler|vk_swiftshader|vulkan)"
)
# QtWebEngineProcess is the standalone helper executable Chromium spawns
# for renderers / GPU / utility processes. On Linux it lives in libexec/
# (outside the lib/-filtered branch), on Windows it sits in bin/ alongside
# the DLLs - so the Windows filter must allow it explicitly.
_qt_exec_keep = re.compile(r"QtWebEngineProcess(\.exe)?$")
# Plugins kept across platforms - superset, harmless extras stay.
# Linux needs xcb/wayland; Windows needs windowsvistastyle (under styles/).
_qt_plugin_keep = re.compile(
    r"PyQt6[/\\]Qt6[/\\]plugins[/\\](platforms|imageformats|tls|iconengines|"
    r"styles|xcbglintegrations|egldeviceintegrations|"
    r"wayland-shell-integration|wayland-decoration-client|"
    r"wayland-graphics-integration-client|platforminputcontexts|"
    r"platformthemes|networkinformation)[/\\]"
)
_qt_resources_keep = re.compile(
    r"PyQt6[/\\]Qt6[/\\]resources[/\\](qtwebengine_resources|qtwebengine_devtools|"
    r"icudtl|v8_context_snapshot)"
)

def _qt_keep(dest: str) -> bool:
    # Normalize backslashes (Windows) for substring checks.
    d = dest.replace("\\", "/")
    if "PyQt6/Qt6/" not in d:
        return True
    # Only apply the Qt6 lib filter on Linux ("/lib/" path). On Windows the
    # equivalent DLLs live in "/bin/" alongside critical extras
    # (QtWebEngineProcess.exe, libEGL.dll, libGLESv2.dll, opengl32sw.dll,
    # d3dcompiler_47.dll, vk_swiftshader.dll, …). Trying to allowlist all
    # of them is brittle, and the bin/ tree is only ~80 MB so we leave it
    # whole - Windows bundle stays under 250 MB which is fine.
    if "/lib/" in d:
        base = d.rsplit("/", 1)[-1]
        return bool(
            _qt_lib_keep.match(base)
            or _qt_other_lib_keep.match(base)
            or _qt_exec_keep.match(base)
        )
    if "/plugins/" in d:
        # libqpdf.so reads PDFs as images and drags in libQt6Pdf (4.3 MB) for
        # something the app never does. Dropped with its library rather than
        # shipped broken - the build check below would fail on it otherwise.
        if d.endswith("/libqpdf.so") or d.endswith("/qpdf.dll"):
            return False
        return bool(_qt_plugin_keep.search(dest))
    if "/translations/" in d:
        return d.endswith("/qtwebengine_locales/en-US.pak") or "en-US" in d
    if "/resources/" in d:
        return bool(_qt_resources_keep.search(dest))
    if "/qml/" in d:
        return False
    if "/qsci/" in d:
        return False
    return True

a.binaries = [b for b in a.binaries if _qt_keep(b[0])]
a.datas    = [d for d in a.datas    if _qt_keep(d[0])]


def _check_qt_plugins_are_self_contained(binaries) -> None:
    """Fail the build if a bundled Qt plugin needs a Qt library we stripped.

    This is the check that was missing. The filter above is a hand-written
    allowlist, so a plugin can end up in the bundle while its library does not -
    and nothing complains, because the loader quietly takes the HOST's copy.
    The app then runs on the build machine and on any machine with a matching
    system Qt, and aborts everywhere else. Linux only: it uses ldd, and the
    Windows bundle keeps bin/ whole so it cannot drift this way.
    """
    if not sys.platform.startswith("linux"):
        return
    import subprocess

    have = {os.path.basename(src) for dest, src, _ in binaries}
    libdirs = {os.path.dirname(src) for dest, src, _ in binaries
               if "/Qt6/lib/" in src.replace("\\", "/")}
    env = dict(os.environ, LD_LIBRARY_PATH=os.pathsep.join(libdirs))
    problems = []
    for dest, src, _ in binaries:
        u = src.replace("\\", "/")
        # Everything PyQt6 ships, not just the Qt6 tree: the Python extension
        # modules link Qt libraries too. `PyQt6/QtWebEngineWidgets.abi3.so` needs
        # libQt6OpenGLWidgets, which the allowlist missed because the pattern
        # demanded "OpenGL." exactly - so the host's copy was loaded instead.
        if "/PyQt6/" not in u or not (".so" in os.path.basename(u)):
            continue
        out = subprocess.run(["ldd", src], capture_output=True, text=True,
                             env=env).stdout
        for lib in sorted(set(re.findall(r"libQt6[A-Za-z]+\.so\.6", out))):
            if lib not in have:
                problems.append(f"  {os.path.relpath(dest)} needs {lib}")
    if problems:
        raise SystemExit(
            "Qt bundle is not self-contained - these plugins would load the "
            "host's Qt (and abort whenever it differs):\n" + "\n".join(problems)
            + "\nAdd the library to _qt_lib_keep, or drop the plugin in _qt_keep."
        )


_check_qt_plugins_are_self_contained(a.binaries)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="EVE_Retroindustry",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon="assets/icon.ico",
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="EVE_Retroindustry",
)
