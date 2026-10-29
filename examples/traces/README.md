# Traces

- Root `*.json` files are earlier phase 3/4 real-model traces (`mode: real`).
- `real/` contains phase 5/6 real-model evaluation and repeated-sampling traces.
- `fake/` contains deterministic FakeLLM traces (`mode: fake`).
- `../repeated_sampling*.json` are phase 6 sampling result summaries.
- `../reviews/trace_review.json` is AI-assisted, non-human review material.
- `../reviews/human_review_packet/` is raw evidence for pending independent human review.

Never treat a `mode: fake` trace as evidence of real model behavior, never relabel
a fake trace as real, and never treat AI-assisted review as independent human
review. Traces do not store API keys, Authorization headers, or request headers.

The phase 6 real sampling batch used the default 6-step / 5-tool budget and
`MAX_FORMAT_REPAIRS=2` / `MAX_FINAL_VALIDATION_RETRIES=2`. The E12B candidate
sampling file uses 8/7 and is supplementary only.
