# Primary sources checked during development

- [GitHub standard runner selection](https://docs.github.com/en/actions/how-tos/write-workflows/choose-where-workflows-run/choose-the-runner-for-a-job): standard public hosted runners and runner labels.
- [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions): public standard compute, always-paid larger runners, job logs excluded from artifact-storage allowance and separate repository cache allowance.
- [GitHub workflow dispatch API](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event): workflow dispatch inputs and ref.
- [GitHub job-log REST API](https://docs.github.com/en/rest/actions/workflow-jobs#download-job-logs-for-a-workflow-run) and [GitHub CLI API](https://cli.github.com/manual/gh_api): authenticated plain-text logs, following the API redirect rather than uploading storage-billed artifacts.
- [yt-dlp official repository](https://github.com/yt-dlp/yt-dlp): public URL extraction, format limits, retries and external JavaScript support. Access may fail on cloud IPs; this project does not bypass that restriction.
- [SmolVLM-256M official model card](https://huggingface.co/HuggingFaceTB/SmolVLM-256M-Instruct): Apache-2.0 license, CPU-compatible Transformers example, small-model limitations, input resolution controls. Model repository revision `7e3e67edbbed1bf9888184d9df282b700a323964` was read from its public Hub metadata.
- [SmolVLM research paper](https://arxiv.org/abs/2504.05299): model family architecture and efficiency; no assertion of trailer-judge performance follows from its generic benchmarks.
- [Transformers multimodal chat templates](https://huggingface.co/docs/transformers/v4.57.1/en/chat_templating_multimodal): chat preprocessing and output-token handling.
- [ffmpeg filters](https://ffmpeg.org/ffmpeg-filters.html#select_002c-aselect): timestamp-aware `select`, `selected_n` and `showinfo` presentation timestamps.
- [MCP discovery specification](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/server/discover.mdx) and [current stateless architecture](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/docs/2026-07-28/learn/architecture.mdx): `server/discover`, per-request metadata and independent tool calls.
- [MCP legacy stdio transport](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports): backward-compatible newline-delimited JSON-RPC and protocol-only stdout.
- [OpenAI plugin packaging](https://developers.openai.com/plugins/build/plugins) and [desktop MCP configuration](https://developers.openai.com/plugins/deploy/desktop-only): compatibility package, marketplace, `${PLUGIN_ROOT}` and stdio launch.

Dependency releases were checked against PyPI's public package metadata. The remote workflow pins worker dependencies and action commit hashes. Local tests use synthetic fixtures only; they do not substantiate observed reference-trailer counts or speed measurements.
