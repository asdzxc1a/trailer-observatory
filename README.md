# Trailer Observatory

A custom Codex plugin and CLI for **remote public YouTube trailer observation**. The Mac uses Python's standard library and an existing `gh` login. Video decoding, image extraction and the free open vision model run on two standard GitHub-hosted Linux workers. No vidIQ, paid model endpoint, subscription, browser session or local vision workload is involved.

The plugin returns individually documented one-second bins with image evidence, source/version/time hashes, draft captions, review status, CSV/JSONL/HTML, and measured cut/pacing proxies. It distinguishes visible observations from story inference. Failed downloads and model failures remain failures; they never become successful rows to reach a target count.

Exports travel through free GitHub Actions job logs as indexed, hashed archives (at most 25 MB across 2 shards). The workflow uploads zero storage-billed artifacts. The MCP server supports current stateless discovery and legacy Codex initialization.

See [workflow and installation](references/workflow.md), [evidence semantics](references/evidence.md), and [official sources](references/sources.md).

Version 0.1.1 packages the worker validated in [run 38537883903](https://github.com/asdzxc1a/trailer-observatory/actions/runs/38537883903): 11 official Disney/Pixar publisher trailers, 1,561 captured and documented second bins, zero failed or missing bins. Two CPU shards measured 45.7 and 44.9 minutes of worker processing. YouTube blocked anonymous access; the declared official Disney alternates supplied the actual evidence, with YouTube equivalence explicitly unverified. The tiny model's captions remain drafts and contain factual errors. See the [changelog](CHANGELOG.md) for provenance and limits.

```sh
python3 -m unittest discover -s tests -v
python3 observatory.py validate /absolute/path/to/public-corpus.json
python3 observatory.py preflight OWNER/PUBLIC-CODE-WORKER
python3 observatory.py submit OWNER/PUBLIC-CODE-WORKER /absolute/path/to/public-corpus.json
python3 observatory.py status to-OWNEDJOBID
python3 observatory.py fetch to-OWNEDJOBID /absolute/path/to/new-evidence-directory
```

These are operational commands, not a record that the target research has finished. The output audit is the evidence of completed counts. Small-model captions require visual review before strong creative claims. Pacing similarity cannot certify Disney/Pixar production quality.
