# Laya (https://github.com/NandhaKishorM/laya, Apache-2.0): a "decision model". It answers typed multiple-choice / yes-no / score
# questions about a piece of text in ONE forward pass (about 33 ms on a GPU) and never writes text. Here it is staged for the Commander
# AI Lab simulator, which asks an LLM "which action?" about 25 times per game; a small classifier can answer that class of question
# far faster, if it is accurate enough (it is measured in shadow mode first, see ../README.md).
#
# CPU only. The weights are baked in at a pinned Hugging Face commit and the pod has NO network access (NetworkPolicy), so the
# running service fetches nothing. Two checkpoints are included so they can be compared: the English base and the fine-tuned
# "typed-decisions" one (about 840 MB each).
FROM python:3.12-slim-bookworm

ARG LAYA_VERSION=0.4.15
ARG LAYA_HF_REVISION=7b928d828b7b0e022f929d9bd2e44165aa270148

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1 HF_HOME=/opt/hf

RUN pip install torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install "laya[serve]==${LAYA_VERSION}"

# Weights at the pinned commit. HF's offline lookup resolves the "main" ref, which a commit-pinned download does not create, so it is
# written by hand to the same commit.
RUN python -c "from huggingface_hub import snapshot_download; snapshot_download('convaiinnovations/laya', revision='${LAYA_HF_REVISION}', ignore_patterns=['multilingual/*','assets/*','eval/*'])" \
 && mkdir -p /opt/hf/hub/models--convaiinnovations--laya/refs \
 && printf '%s' "${LAYA_HF_REVISION}" > /opt/hf/hub/models--convaiinnovations--laya/refs/main \
 && chmod -R a+rX /opt/hf

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin laya
USER 10001

ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    LAYA_HOST=0.0.0.0 LAYA_PORT=8080 \
    LAYA_MODELS=english,typed-decisions LAYA_DEFAULT_MODEL=english LAYA_MAX_LOADED=2 LAYA_PRELOAD=1 LAYA_THREADS=4

EXPOSE 8080
CMD ["laya-serve"]
