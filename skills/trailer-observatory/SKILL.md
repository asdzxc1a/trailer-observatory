---
name: trailer-observatory
description: Analyze an authorized corpus of public YouTube movie trailers remotely, export timestamped visual evidence, and compare measurable trailer pacing. Use for trailer research and evidence-based edit review.
---

Use the bundled `trailer_observatory` MCP tools or the plugin's `observatory.py` CLI. The local process only controls GitHub and reads evidence; all source-video decoding and vision inference run on standard GitHub-hosted Linux workers in a dedicated public code-only repository.

Resolve the worker repository from the session or ask for it if absent. For setup and corpus schema, read [the workflow guide](../../references/workflow.md). For interpretation, read [evidence semantics](../../references/evidence.md).

Use `preflight` before `submit`. Public visibility, reviewed code, standard runner labels and concurrency are enforced in code. Zero Actions artifacts are uploaded; bounded evidence travels through free job logs. Existing task authorization to research trailers covers dispatching their public URLs. Do not include the user's original movie, story, private links or private files in the public manifest.

Declare each analyzed window with integer `start_second` and `seconds`, plus its public URL. A requested count is a target, never a claim of completed research. After a run finishes, use `fetch` and its audit. Failed bins, missing evidence, incomplete captions, duplicated source seconds, and unreviewed captions must stay visible. State exactly how many evidence-backed rows exist and how many were human reviewed. Do not equate reading captions with watching motion or hearing dialogue.

Check each relevant evidence frame when making a specific visual claim. Small-model captions are drafts and can misread animation. Keep `observation` separate from `inference`; preserve the source URL, second, image hash, model revision, and review status when correcting rows.

`compare` measures pacing proxies only. Use an independent creative judge for story clarity, performance, staging, character design and sound. A matching metric never establishes Disney/Pixar quality parity. Revise concrete defects and compare the actual revised output again.

The plugin has no vidIQ, paid inference endpoint, subscription, artifact-storage billing, authenticated-cookie download, or access bypass. Do not add one as a fallback. Public YouTube may block cloud download. An explicitly selected `public_mirror` on video.disney.com can supply a separately identified official publisher version using its normal public progressive stream. Preserve actual source identity and `youtube_equivalence: not_established`; never silently substitute a teaser for a longer trailer. Otherwise report failed access, keep diagnostics, and continue work that does not depend on unseen footage.
