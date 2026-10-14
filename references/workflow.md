# Workflow and installation

## Prerequisites and privacy

The local client requires Python 3.10+ and GitHub CLI `gh` with a normal authenticated login. It has no Python package dependencies. Do not install `requirements-worker.txt` on the Mac. That file is for the remote worker.

Copy this code-only folder to a **dedicated public GitHub repository**. Its workflow must be enabled on `main` (or the explicitly selected branch). Do not copy your original film project into that repository. Only public-reference URL/window metadata is sent as workflow inputs. Worker logs are accessible through that public repository. Source video files are held in runner temporary storage, removed after each trailer, and excluded from log exports and Git.

Standard GitHub-hosted compute on public repositories is free. Larger runners are charged even on public repositories, so both code and workflow enforce `ubuntu-24.04`. The plugin uploads **zero Actions artifacts**. It exports bounded indexed gzip/base64 archives through job logs, which GitHub documents as excluded from artifact-storage allowance. Each shard archive is at most 12.5 MB; the two together are at most 25 MB. Evidence previews are 384×216 JPEGs to keep the transport useful and bounded. Source movies never enter the archive. The model cache is capped at 950 MB and both model/pip caches stay below the 10 GB repository cache allowance. The plugin never modifies a billing budget or purchases capacity.

## Install the local Codex plugin

This folder contains the supported `.codex-plugin/plugin.json` compatibility manifest, `.mcp.json`, skill, and local marketplace. After review, a local install can use:

```sh
codex plugin marketplace add /absolute/path/to/trailer-observatory
codex plugin add trailer-observatory@trailer-observatory-local
```

The MCP manifest uses the built-in `${PLUGIN_ROOT}` placeholder. The persistent client job ledger defaults to `~/.local/share/trailer-observatory`; override with `TRAILER_OBSERVATORY_STATE` or `--state-dir`. Authentication stays in `gh`, never in the plugin or corpus manifest. There is no public Plugin Directory submission or partner-registration step for this local development package.

## Corpus manifest

```json
{
  "schema_version": 1,
  "corpus_id": "my-public-trailer-references",
  "expected_observations": 142,
  "trailers": [
    {
      "id": "reference-one",
      "url": "https://www.youtube.com/watch?v=VALIDVIDEOID",
      "title": "Public official movie trailer",
      "start_second": 0,
      "seconds": 142,
      "channel_id": "UC_OFFICIAL_CHANNEL_ID_HERE",
      "rights_note": "Official public trailer, research and critique"
    }
  ]
}
```

`channel_id` and `rights_note` are optional; replace the example identifiers with real verified values. The URL must be an individual HTTPS YouTube video. Each window has an exclusive end `start_second + seconds`. A partial final bin is labeled and sampled inside its measured decoded coverage. Wholly absent bins fail. Content outside each declared window is trimmed and never claimed as analyzed. One dispatch accepts up to 11 references and 1,800 unique one-second bins; each trailer window is at most 300 seconds. The target 11/1,561 corpus must explicitly declare windows summing to 1,561, rather than letting the worker pad or invent timestamps.

## Run and retrieve

`preflight` reads public repository visibility and verifies all executable worker/control-plane files against this reviewed package. It refuses altered or paid-runner workflows. `submit` records an owned job ID before dispatch; it refuses overlapping owned dispatches. The workflow queues across the repository and uses 2 balanced shards. The vision model loads once per shard and repeated identical evidence reuses verified captions. Each progress line is compact.

`status JOB_ID` is read-only and verifies the run's source commit. Use `inspect-runs OWNER/REPO` to inspect general runs. `cancel JOB_ID` acts only on a run dispatched and recorded by this client, never unrelated jobs. `fetch JOB_ID DESTINATION` requires a completed owned run and an empty/new destination. It retrieves plain-text logs only from the run's observation jobs, reconstructs indexed archives, verifies compressed hashes/size/counts, rejects traversal/links/unsafe types and caps expansion before writing. Then it merges shards, verifies every evidence hash, and creates `observations.jsonl`, `observations.csv`, `review.html`, `metrics.json`, `audit.json`, `benchmark.md`, `manifest.json`, and run provenance. Downloaded raw logs are removed once evidence is reconstructed.

Public YouTube often rejects cloud IPs or requires sign-in. A rejected source becomes timestamped failed bins, empty observations, and diagnostics in the log export. Do not add cookies, authentication tricks or a paid scraper to conceal that limitation. Inspect failures, fix an ordinary documented dependency issue if present, then run a narrowly scoped retry. If source access remains denied, report it honestly.

The MCP server accepts current `2026-07-28` stateless per-request metadata and `server/discover`, plus legacy `initialize` for `2025-06-18` and `2025-11-25` clients. Any stateless tool call works without a previous handshake. Unsupported requested versions return a supported-version error; job ownership is an explicit application handle, not hidden protocol session state.

## Smoke test before the full corpus

Dispatch 2–4 declared seconds from one verified official public reference, then check that the returned frame hash, midpoint timestamp, small-model caption, model revision, and source identity agree. Do not count that smoke test as the 1,561-row corpus. No local unit test downloads or decodes a video.
