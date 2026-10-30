from pathlib import Path

from PyInstaller.utils.hooks import collect_all, copy_metadata

root = Path(SPECPATH).parent
datas, binaries, hiddenimports = [], [], ["_cffi_backend"]
for package in ("garminconnect", "curl_cffi", "ua_generator"):
    package_data, package_binaries, package_imports = collect_all(package)
    datas += package_data
    binaries += package_binaries
    hiddenimports += package_imports
for package in ("garminconnect", "curl_cffi", "mywhoosh-garmin-sync"):
    datas += copy_metadata(package)

a = Analysis(
    [str(root / "packaging" / "windows_entry.py")],
    pathex=[str(root / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pytest", "setuptools", "pip"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="MyWhooshGarminSync", console=False, upx=False,
    disable_windowed_traceback=True,
)
