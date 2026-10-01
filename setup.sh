#!/bin/bash
# Python environment and the speech models the assistant loads.
# Archives are deleted after they unpack. Already-present models are skipped.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt

mkdir -p models
cd models

fetch_tar() {
  local url="$1" marker="$2"
  local archive
  archive="$(basename "$url")"
  if [[ -e "$marker" ]]; then
    echo "already present: $marker"
    return
  fi
  echo "downloading $archive"
  curl -fL --retry 3 -o "$archive" "$url"
  tar xf "$archive"
  rm -f "$archive"
}

fetch_file() {
  local url="$1" name="$2"
  if [[ -f "$name" ]]; then
    echo "already present: $name"
    return
  fi
  echo "downloading $name"
  curl -fL --retry 3 -o "$name" "$url"
}

ASR="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"
TTS="https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models"
KWS="https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models"
SPK="https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models"

# Spanish listener. Kroko streams. The others close the phrase at silence.
# Whisper small is scored and can be chosen by voice. It is not the automatic live engine.
fetch_tar "$ASR/sherpa-onnx-streaming-zipformer-es-kroko-2025-08-06.tar.bz2" \
  sherpa-onnx-streaming-zipformer-es-kroko-2025-08-06
fetch_tar "$ASR/sherpa-onnx-whisper-tiny.tar.bz2" sherpa-onnx-whisper-tiny
fetch_tar "$ASR/sherpa-onnx-whisper-base.tar.bz2" sherpa-onnx-whisper-base
fetch_tar "$ASR/sherpa-onnx-whisper-small.tar.bz2" sherpa-onnx-whisper-small
fetch_tar "$ASR/sherpa-onnx-nemo-canary-180m-flash-en-es-de-fr-int8.tar.bz2" \
  sherpa-onnx-nemo-canary-180m-flash-en-es-de-fr-int8

# Nine Spanish Piper voices. Sharvard supplies voices 2 and 3 from one model.
for voice in \
  vits-piper-es_ES-davefx-medium-int8 \
  vits-piper-es_ES-sharvard-medium-int8 \
  vits-piper-es_ES-carlfm-x_low-int8 \
  vits-piper-es_ES-glados-medium-int8 \
  vits-piper-es_ES-miro-high-int8 \
  vits-piper-es_MX-ald-medium-int8 \
  vits-piper-es_MX-claude-high-int8 \
  vits-piper-es_AR-daniela-high-int8
do
  fetch_tar "$TTS/${voice}.tar.bz2" "$voice"
done

# Voice prints.
fetch_file \
  "$SPK/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx" \
  3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx

# English path, used only when config.json language is en.
fetch_tar "$KWS/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01.tar.bz2" \
  sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01
fetch_tar "$ASR/sherpa-onnx-moonshine-tiny-en-int8.tar.bz2" \
  sherpa-onnx-moonshine-tiny-en-int8
fetch_tar "$TTS/vits-piper-en_US-amy-low.tar.bz2" vits-piper-en_US-amy-low

echo "models ready"
