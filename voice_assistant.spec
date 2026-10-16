# Сборка «Голосовой помощник» в папку с .exe:  venv\Scripts\pyinstaller voice_assistant.spec
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, copy_metadata

import os  # noqa: E402

# модели: русский, английский и офлайн-перевод (tc-big, если собран, иначе OPUS-MT из Argos)
datas = [(f"models/{m}", f"models/{m}") for m in ("gigaam-v3-e2e-rnnt", "nemo-parakeet-tdt-0.6b-v2")]
datas.append(("models/tcbig", "models/tcbig") if os.path.isdir("models/tcbig") else ("models/en_ru", "models/en_ru"))
datas.append(("models/silero-vad", "models/silero-vad"))
datas += collect_data_files("onnx_asr")
datas += collect_data_files("soundcard")
datas += collect_data_files("customtkinter")       # темы и шрифты нового интерфейса
for pkg in ("onnx-asr", "yt-dlp", "onnxruntime", "numpy", "huggingface-hub", "pycaw", "comtypes", "av"):
    try:
        datas += copy_metadata(pkg)           # библиотеки проверяют свою версию при импорте
    except Exception:
        pass
binaries = collect_dynamic_libs("ctranslate2")

a = Analysis(
    ["app.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=["process_audio", "overlay_ui", "audio_input", "ui_main", "recognition", "textproc", "dsp", "paths", "soxr", "pystray._win32", "pycaw.pycaw", "comtypes.stream", "soundcard.mediafoundation",
                   "darkdetect"],
    excludes=["torch", "torchaudio", "transformers", "gigaam", "faster_whisper", "matplotlib", "pandas", "IPython",
              "hydra", "omegaconf", "pydub", "tensorflow", "noisereduce", "keyboard", "PyQt5", "PySide6",
              "notebook", "jupyter", "sympy", "scipy"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Голосовой помощник",
    console=False,
    icon="docs/icon.ico",
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Голосовой помощник", upx=False)

import shutil  # noqa: E402
shutil.copy("Как пользоваться.txt", "dist/Голосовой помощник/Как пользоваться.txt")
shutil.copy("Инструкция.html", "dist/Голосовой помощник/Инструкция.html")
