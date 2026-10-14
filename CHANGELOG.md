# Changelog

## 0.1.1 — 2026-10-14

- Package the free public-runner workflow, supported Codex marketplace registration, rejected-dispatch recovery, measured decoded-frame timestamps, partial EOF bins, and explicitly declared official Disney publisher alternates validated from commit `808cf2529ff61e3494dade592f82b7e959f6a37c`.
- [Integration run 38537883903](https://github.com/asdzxc1a/trailer-observatory/actions/runs/38537883903) completed on two standard GitHub-hosted CPU workers: 11 trailers, 1,561 unique chronological second bins, zero failed or missing bins, and verified SHA-256 evidence frames. Monotonic worker times were 2,739.313 and 2,696.709 seconds (45.7 and 44.9 minutes), excluding queue and runner setup. This is measured throughput, not a promise of faster future runs.
- Anonymous YouTube downloads were bot-blocked. The manifest's separately identified, public official Disney publisher versions supplied all footage. Their source URLs, media hashes and decoded durations are preserved; frame equivalence to the requested YouTube versions is **not established**. No cookies, proxy, access bypass or paid service was used.
- SmolVLM-256M generated 1,550 new-caption rows and 11 rows reusing exact-frame captions. All remain machine drafts, and visual spot review found factual errors and repetition. One sampled still per second does not establish motion, dialogue, audio quality or Disney/Pixar production parity. The evidence dataset is kept separately; this repository publishes code only.
- Export through bounded, indexed and hash-checked job logs with zero artifact uploads. Strengthen media/evidence exclusions and keep observed version 0.1.0 provenance unchanged; future workers report 0.1.1.
- Validation: 54 synthetic-fixture unit tests, modern stateless MCP discovery, legacy Codex MCP initialization, and eight-tool discovery. No research footage or model is decoded/downloaded on the client.

## 0.1.0

Initial plugin checkpoint. Preserved tag and Git history include the first workflow, marketplace and remote smoke-test iterations.
