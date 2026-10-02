"""Скачивает модели в папку models/ (≈1,1 ГБ):  python tools/download_models.py

• GigaAM v3 e2e RNNT int8 (русский, Сбер, MIT)                 — Hugging Face istupakov/gigaam-v3-onnx
• NVIDIA Parakeet TDT 0.6B v2 int8 (английский, CC-BY-4.0)     — Hugging Face istupakov/parakeet-tdt-0.6b-v2-onnx
• OPUS-MT en→ru (офлайн-перевод, CC-BY-4.0)                     — пакет Argos Translate

Более сильная офлайн-модель перевода (OPUS-MT tc-big) собирается конвертацией и требует PyTorch:
  pip install transformers torch sacremoses
  ct2-transformers-converter --model Helsinki-NLP/opus-mt-tc-big-en-zle --output_dir models/tcbig --quantization int8
  (и скопировать source.spm / target.spm из того же репозитория в models/tcbig)
Без неё программа использует OPUS-MT из пакета Argos.
"""
import io
import shutil
import urllib.request
import zipfile
from pathlib import Path

from huggingface_hub import snapshot_download

MODELS = Path(__file__).resolve().parent.parent / "models"

ASR = {
    "gigaam-v3-e2e-rnnt": ("istupakov/gigaam-v3-onnx",
                           ["config.json", "v3_e2e_rnnt_*.int8.onnx", "v3_e2e_rnnt_vocab.txt"]),
    "nemo-parakeet-tdt-0.6b-v2": ("istupakov/parakeet-tdt-0.6b-v2-onnx",
                                  ["config.json", "*.int8.onnx", "vocab.txt"]),
}
ARGOS_EN_RU = "https://argos-net.com/v1/translate-en_ru-1_9.argosmodel"


def main():
    MODELS.mkdir(exist_ok=True)
    for folder, (repo, patterns) in ASR.items():
        dst = MODELS / folder
        print(f"→ {folder} ({repo})")
        snapshot_download(repo, allow_patterns=patterns, local_dir=dst)
    dst = MODELS / "en_ru"
    if not (dst / "model").exists():
        print("→ OPUS-MT en→ru (Argos)")
        data = urllib.request.urlopen(ARGOS_EN_RU, timeout=120).read()
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            root = z.namelist()[0].split("/")[0]
            z.extractall(MODELS / "_tmp")
        shutil.move(str(MODELS / "_tmp" / root), dst)
        shutil.rmtree(MODELS / "_tmp", ignore_errors=True)
    print("Готово:", MODELS)


if __name__ == "__main__":
    main()
