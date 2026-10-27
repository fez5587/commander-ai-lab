# Laya in this repository

Laya (https://github.com/NandhaKishorM/laya, Apache-2.0) is a **decision model**: it answers typed multiple-choice, yes/no and score
questions about a piece of text in one forward pass and never writes text. Here it is a fast second opinion beside the LLM and CSIE.
Kev (https://github.com/jaredpalmer/kev) serves the same `POST /v1/systemone` API, so swapping is a URL change (Kev needs a GPU).

| File | What |
|---|---|
| `Dockerfile` | CPU image: `laya[serve]` 0.4.15 + CPU torch, English and `typed-decisions` checkpoints baked in at Hugging Face commit `7b928d8`; runs offline |
| `deployment-laya.yaml` | Kubernetes Deployment + Service + NetworkPolicy (no egress; only the app may call it). Currently `replicas: 0` |
| `build-farmhouse-laya.workflow.yml` | the CI workflow that builds and smoke-tests the image offline (it lives in `.github/workflows/` of the farmhouse-os GitOps repo) |
| `LAYA-EVALUATION.md` | why it is off: zero-shot both checkpoints agreed with the LLM less often than always guessing the commonest action |

The deployment source of truth is the farmhouse-os repo (`infrastructure/apps/commander-lab/`); these are reference copies so the
fork is self-contained. Things learned running it:

- A Kubernetes Service named `laya` injects `LAYA_PORT=tcp://...`, which `laya-serve` reads as its own port setting and refuses. Set
  `enableServiceLinks: false` and `LAYA_PORT=8080` explicitly.
- Two checkpoints resident use 3.6 GB; one uses about 1.9 GB. Do not place it on a control-plane node.
- The CPU answers in 2-4 s, not the ~33 ms quoted for a GPU.
- How the code in this repository connects to it, and how to fine-tune it: `docs/LAYA-CONNECTIONS.md`.
