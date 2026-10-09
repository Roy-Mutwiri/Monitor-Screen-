# Overrides the broken pyinstaller-hooks-contrib hook for webrtcvad-wheels: the package is a thin
# pure-Python wrapper around the compiled `_webrtcvad` extension.
from PyInstaller.utils.hooks import collect_dynamic_libs

hiddenimports = ["_webrtcvad"]
binaries = collect_dynamic_libs("webrtcvad")
