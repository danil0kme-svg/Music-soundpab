# Сборка: pip download yt-dlp --no-deps -d build_wheels && pyinstaller SoundpadMusic.spec
import glob

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = [], [], []

# ffmpeg внутри exe
d, b, h = collect_all("imageio_ffmpeg")
datas += d
binaries += b
hiddenimports += h

# yt-dlp кладём wheel-файлом, чтобы его можно было обновлять без пересборки exe
wheels = glob.glob("build_wheels/yt_dlp-*.whl")
if not wheels:
    raise SystemExit("Нет build_wheels/yt_dlp-*.whl — сначала pip download yt-dlp --no-deps -d build_wheels")
datas += [(w, "wheels") for w in wheels]
# анализируем yt-dlp, чтобы в exe попали все нужные ему модули стандартной библиотеки
# и опциональные зависимости (certifi, websockets, brotli...)
hiddenimports += collect_submodules("yt_dlp")

a = Analysis(["launcher.py"], pathex=["."], datas=datas, binaries=binaries,
             hiddenimports=hiddenimports)
# ...а сам yt_dlp из exe выкидываем: он грузится из wheel
a.pure = [m for m in a.pure if m[0] != "yt_dlp" and not m[0].startswith("yt_dlp.")]

pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
          name="SoundpadMusic", console=False, upx=False)
