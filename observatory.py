#!/usr/bin/env python3
"""Lightweight Mac control plane. Never imports video or ML dependencies."""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import html
import io
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import tarfile
import time
from urllib.parse import parse_qs, urlparse
import uuid

VERSION = "0.1.0"
ROOT = Path(__file__).resolve().parent
WORKFLOW = ".github/workflows/observe.yml"
MODEL = "HuggingFaceTB/SmolVLM-256M-Instruct"
MODEL_REVISION = "7e3e67edbbed1bf9888184d9df282b700a323964"
MAX_SECONDS = 1800
MAX_ARCHIVE_BYTES = 12_500_000 # Two shards together: at most 25 MB compressed.
MAX_LOG_BYTES = 50_000_000
MAX_EXTRACTED_BYTES = 80_000_000
MAX_CACHE_BYTES = 950_000_000


class ObservatoryError(ValueError):
    pass


class GitHubCLIError(ObservatoryError):
    def __init__(self,message,http_status=None):
        super().__init__(message)
        self.http_status=http_status


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def youtube_id(url: str) -> str:
    """Only public watch URL shapes; never pass arbitrary URLs to yt-dlp."""
    if not isinstance(url, str) or len(url) > 300:
        raise ObservatoryError("Expected a short public YouTube watch URL")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port:
        raise ObservatoryError("Use an HTTPS YouTube URL without credentials or port")
    host = (parsed.hostname or "").lower()
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if parsed.path != "/watch":
            raise ObservatoryError("Only individual /watch videos are supported")
        queries = parse_qs(parsed.query)
        if set(queries) - {"v", "t", "si"} or len(queries.get("v", [])) != 1:
            raise ObservatoryError("Playlists and arbitrary query parameters are forbidden")
        video = queries["v"][0]
    elif host == "youtu.be":
        video = parsed.path.strip("/")
        if set(parse_qs(parsed.query)) - {"t", "si"}:
            raise ObservatoryError("Unexpected YouTube URL parameters")
    else:
        raise ObservatoryError("Only youtube.com and youtu.be are supported")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video):
        raise ObservatoryError("Invalid YouTube video identifier")
    return video


def disney_page_id(url):
    if not isinstance(url,str) or len(url)>300:raise ObservatoryError("Expected a short official Disney video page URL")
    parsed=urlparse(url)
    if parsed.scheme!="https" or parsed.hostname!="video.disney.com" or parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment:
        raise ObservatoryError("Mirror must be an HTTPS video.disney.com page without credentials/query")
    match=re.fullmatch(r"/watch/[a-z0-9-]+-([a-f0-9]{24})",parsed.path)
    if not match:raise ObservatoryError("Mirror must be an individual official Disney watch page")
    return match.group(1)


def integer(value, label, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ObservatoryError(f"{label} must be an integer from {minimum} through {maximum}")
    return value


def validate_manifest(manifest: dict) -> dict:
    if not isinstance(manifest, dict) or set(manifest) - {
        "schema_version", "corpus_id", "expected_observations", "trailers"
    }:
        raise ObservatoryError("Manifest contains unsupported fields; never upload private movie data")
    if manifest.get("schema_version") != 1:
        raise ObservatoryError("Unsupported manifest schema")
    corpus = manifest.get("corpus_id", "")
    if not isinstance(corpus, str) or not re.fullmatch(r"[a-z0-9-]{1,64}", corpus):
        raise ObservatoryError("corpus_id must be a lowercase slug")
    trailers = manifest.get("trailers")
    if not isinstance(trailers, list) or not 1 <= len(trailers) <= 11:
        raise ObservatoryError("A dispatch accepts 1 through 11 trailers")
    seen, windows, normalized = set(), {}, []
    for trailer in trailers:
        if not isinstance(trailer, dict) or set(trailer) - {
            "id", "url", "title", "start_second", "seconds", "channel_id", "rights_note", "public_mirror"
        }:
            raise ObservatoryError("Trailer manifest contains unsupported fields")
        tid = trailer.get("id", "")
        if not isinstance(tid, str) or not re.fullmatch(r"[a-z0-9_-]{1,64}", tid) or tid in seen:
            raise ObservatoryError("Trailer IDs must be unique lowercase IDs with hyphens or underscores")
        seen.add(tid)
        vid = youtube_id(trailer.get("url"))
        start = integer(trailer.get("start_second"), "start_second", 0, 600)
        seconds = integer(trailer.get("seconds"), "seconds", 1, 300)
        for a, b in windows.setdefault(vid, []):
            if start < b and start + seconds > a:
                raise ObservatoryError("Same-video windows overlap; counts must represent unique bins")
        windows[vid].append((start, start + seconds))
        title = trailer.get("title", tid)
        if not isinstance(title, str) or not 1 <= len(title) <= 160:
            raise ObservatoryError("Public trailer title is too long or missing")
        item = dict(trailer, url=f"https://www.youtube.com/watch?v={vid}", title=title)
        channel = item.get("channel_id")
        if channel is not None and not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel):
            raise ObservatoryError("channel_id must be a YouTube channel ID")
        if len(str(item.get("rights_note", ""))) > 300:
            raise ObservatoryError("rights_note is too long")
        mirror=item.get("public_mirror")
        if mirror is not None:
            if not isinstance(mirror,dict) or set(mirror)-{"url","expected_title","version_note"}:
                raise ObservatoryError("Mirror contains unsupported fields")
            mirror_id="disney:"+disney_page_id(mirror.get("url"))
            for a,b in windows.setdefault(mirror_id,[]):
                if start<b and start+seconds>a:raise ObservatoryError("Same-mirror windows overlap; observed source bins must be unique")
            windows[mirror_id].append((start,start+seconds))
            for field in ("expected_title","version_note"):
                if field in mirror and (not isinstance(mirror[field],str) or len(mirror[field])>300):
                    raise ObservatoryError(f"Mirror {field} must be short public reference metadata")
        normalized.append(item)
    total = sum(t["seconds"] for t in normalized)
    if total > MAX_SECONDS:
        raise ObservatoryError(f"A dispatch is capped at {MAX_SECONDS} seconds")
    if integer(manifest.get("expected_observations"), "expected_observations", 1, MAX_SECONDS) != total:
        raise ObservatoryError(f"Expected count differs from declared windows ({total})")
    return dict(manifest, trailers=normalized)


def expected_bins(manifest):
    return {(t["id"], n) for t in manifest["trailers"]
            for n in range(t["start_second"], t["start_second"] + t["seconds"])}


def load_json(path):
    return json.loads(Path(path).read_text())


def dump_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def read_rows(path):
    with Path(path).open() as source:
        return [json.loads(line) for line in source if line.strip()]


def audit_rows(rows, manifest, evidence_root=None):
    """Count evidence, never count a failed or repeated row as an observation."""
    wanted = expected_bins(validate_manifest(manifest))
    seen, valid, failures, errors = set(), 0, 0, []
    trailers = {t["id"]: t for t in manifest["trailers"]}
    for row in rows:
        key = (row.get("trailer_id"), row.get("second"))
        if key in seen:
            errors.append(f"duplicate bin {key}")
        seen.add(key)
        if key not in wanted:
            errors.append(f"unexpected bin {key}")
            continue
        trailer = trailers[key[0]]
        mirror=trailer.get("public_mirror",{})
        source_is_mirror=row.get("source_url")==mirror.get("url") and row.get("source_platform")=="disney_official"
        if row.get("source_url") != trailer["url"] and not source_is_mirror:
            errors.append(f"wrong source URL {key}")
        if row.get("evidence_status") != "captured" or row.get("annotation_status") not in {
            "machine_unreviewed", "human_reviewed", "reused_exact_frame_unreviewed"
        }:
            failures += 1
            continue
        required = ["observation", "frame_sha256", "frame_path", "sample_time_seconds",
                    "decoded_frame_time_seconds",
                    "model_id", "model_revision", "worker_version", "source_video_id"]
        if any(row.get(field) in (None, "") for field in required):
            errors.append(f"incomplete evidence {key}")
            continue
        if row.get("partial_bin"):
            actual_end=row.get("actual_decoded_duration_seconds")
            if not isinstance(actual_end,(int,float)) or not key[1]<actual_end<key[1]+1:
                errors.append(f"invalid partial-bin EOF {key}")
                continue
            target=min(key[1]+0.5,key[1]+(actual_end-key[1])/2)
            if row["sample_time_seconds"]!=target or row["decoded_frame_time_seconds"]>=actual_end:
                errors.append(f"partial-bin sample outside decoded coverage {key}")
                continue
        elif row["sample_time_seconds"] != key[1] + 0.5:
            errors.append(f"wrong midpoint {key}")
            continue
        if not key[1] <= row["decoded_frame_time_seconds"] < key[1]+1:
            errors.append(f"decoded frame outside bin {key}")
            continue
        actual_id=disney_page_id(mirror["url"]) if source_is_mirror else youtube_id(trailer["url"])
        if row["source_video_id"] != actual_id:
            errors.append(f"wrong source identifier {key}")
            continue
        if source_is_mirror and (row.get("requested_source_url")!=trailer["url"] or row.get("youtube_equivalence")!="not_established"):
            errors.append(f"mirror provenance/equivalence missing {key}")
            continue
        if not re.fullmatch(r"[a-f0-9]{64}", row["frame_sha256"]):
            errors.append(f"invalid hash {key}")
            continue
        if evidence_root is not None:
            root = Path(evidence_root).resolve()
            frame = (root / row["frame_path"]).resolve()
            if not frame.is_relative_to(root) or not frame.is_file():
                errors.append(f"missing/unsafe frame {key}")
                continue
            if sha256(frame.read_bytes()) != row["frame_sha256"]:
                errors.append(f"frame hash mismatch {key}")
                continue
        valid += 1
    missing = sorted(wanted - seen)
    return {"expected_bins": len(wanted), "documented_observations": valid,
            "failed_bins": failures, "missing_bins": missing, "errors": errors,
            "complete": not errors and not missing and not failures and valid == len(wanted)}


def structural_metrics(rows):
    groups = {}
    for row in rows:
        groups.setdefault(row["trailer_id"], []).append(row)
    result = []
    for tid, group in sorted(groups.items()):
        group.sort(key=lambda r: r["second"])
        complete = all(r.get("evidence_status") == "captured" for r in group)
        cuts = [r["second"] for r in group if r.get("cut_candidate")]
        start, end = group[0]["second"], group[-1]["second"] + 1
        points = [start] + cuts + [end]
        shot_lengths = [b-a for a, b in zip(points, points[1:]) if b > a]
        changes = [r.get("pixel_change", 0) for r in group if r.get("evidence_status") == "captured"]
        quarters = []
        for q in range(4):
            low, high = start+(end-start)*q/4, start+(end-start)*(q+1)/4
            quarters.append(round(sum(low <= c < high for c in cuts)/((end-start)/4)*60, 3))
        result.append({"trailer_id": tid, "start_second": start, "end_second_exclusive": end,
                       "window_seconds": end-start, "evidence_complete": complete,
                       "sampled_cut_candidates": len(cuts), "cuts_per_minute_1fps_proxy": round(len(cuts)/(end-start)*60, 3),
                       "median_segment_seconds_1fps_proxy": statistics.median(shot_lengths),
                       "quarter_cut_rates": quarters,
                       "mean_pixel_change": round(statistics.mean(changes), 4) if changes else None,
                       "limits": "1 fps boundary proxy misses subsecond cuts; pixel change includes camera/lighting/motion. Not acting, story or audio quality."})
    return result


def compare(reference_path, candidate_path):
    refs, candidates = load_json(reference_path), load_json(candidate_path)
    if isinstance(refs, dict): refs = refs.get("metrics", refs.get("trailers", []))
    if isinstance(candidates, dict): candidates = candidates.get("metrics", candidates.get("trailers", []))
    if not isinstance(refs, list) or not refs or not isinstance(candidates, list) or not candidates:
        raise ObservatoryError("Compare expects nonempty metric lists")
    fields = ["cuts_per_minute_1fps_proxy", "median_segment_seconds_1fps_proxy", "mean_pixel_change"]
    baseline = {field: statistics.median([r[field] for r in refs if r.get(field) is not None])
                for field in fields if any(r.get(field) is not None for r in refs)}
    return {"reference_count": len(refs), "reference_medians": baseline,
            "comparisons": [{"trailer_id": c.get("trailer_id"), "metric_deltas": {
                field: round(c[field]-value, 4) for field, value in baseline.items() if c.get(field) is not None
            }} for c in candidates],
            "quality_parity": "not_established",
            "limitations": "These measured proxies support an edit review. Matching pace does not establish Disney/Pixar render, performance, animation, dialogue, music or story quality."}


def export_review(rows, out, manifest):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda r: (next(i for i,t in enumerate(manifest["trailers"]) if t["id"] == r["trailer_id"]), r["second"]))
    (out / "observations.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False)+"\n" for r in rows))
    columns = ["trailer_id", "second", "source_url", "sample_time_seconds", "decoded_frame_time_seconds", "partial_bin", "observed_bin_end_seconds_exclusive", "observation", "inference",
               "evidence_status", "annotation_status", "frame_path", "frame_sha256", "phash",
               "cut_candidate", "pixel_change", "model_id", "model_revision", "worker_version"]
    with (out / "observations.csv").open("w", newline="") as target:
        writer = csv.DictWriter(target, columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    metrics = structural_metrics(rows) if rows else []
    audit = audit_rows(rows, manifest, out)
    dump_json(out / "metrics.json", metrics)
    dump_json(out / "audit.json", audit)
    dump_json(out / "manifest.json", manifest)
    cards = []
    for r in rows:
        esc = lambda v: html.escape(str(v), quote=True)
        frame = r.get("frame_path", "")
        image = f'<img loading="lazy" src="{esc(frame)}" alt="Evidence {esc(r["second"])} seconds">' if frame else "<p>Frame unavailable</p>"
        source_link=r["source_url"]+(f'&t={r["second"]}' if r.get("source_platform")!="disney_official" else "")
        cards.append(f'<article data-trailer="{esc(r["trailer_id"])}"><h3>{esc(r["trailer_id"])} · {r["second"]:03d}s</h3>{image}<p>{esc(r.get("observation", ""))}</p><small>{esc(r.get("annotation_status"))} · {esc(r.get("evidence_status"))}</small><p>Inference: {esc(r.get("inference", ""))}</p><a href="{esc(source_link)}" target="_blank" rel="noopener">Public source page · sample {r["second"]}s</a><details><summary>Provenance</summary><pre>{esc(json.dumps(r, indent=2))}</pre></details></article>')
    document = '''<!doctype html><meta charset="utf-8"><title>Trailer Observatory evidence review</title>
<style>body{background:#11151c;color:#eef0f4;font:16px system-ui;margin:2rem}header{max-width:70rem}section{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1rem}article{background:#202735;padding:1rem;border-radius:8px}img{width:100%;aspect-ratio:16/9;object-fit:contain;background:black}small{color:#fcce76}a{color:#9acaff}pre{white-space:pre-wrap;word-break:break-all;font-size:11px}select{padding:.5rem;margin:1rem 0}</style>
<header><h1>Trailer Observatory</h1><p>Exact chronological one-second bins, sampled at their midpoint. Small-model captions are machine drafts until a reviewer checks each evidence frame. Missing/failed bins never count as observations.</p><p>''' + html.escape(json.dumps(audit)) + '''</p><select id="filter"><option value="">All trailers</option>''' + "".join(f'<option>{html.escape(t["id"])}</option>' for t in manifest["trailers"]) + '''</select></header><section>''' + "".join(cards) + '''</section><script>document.querySelector('#filter').addEventListener('change',e=>document.querySelectorAll('article').forEach(a=>a.hidden=Boolean(e.target.value&&a.dataset.trailer!==e.target.value)))</script>'''
    (out / "review.html").write_text(document)
    (out / "benchmark.md").write_text("# Evidence benchmark\n\n" + f"Documented machine observations: {audit['documented_observations']} / {audit['expected_bins']}. Complete: {audit['complete']}.\n\n" + "Measured quantities are 1 fps cut and pixel-change proxies. They cannot certify feature-animation quality. Each reference window is declared in manifest.json; content after its exclusive end is omitted, not claimed as analyzed.\n\n" + "\n".join(f"- {m['trailer_id']}: {m['window_seconds']} seconds; {m['sampled_cut_candidates']} sampled cut candidates; median sampled segment {m['median_segment_seconds_1fps_proxy']} seconds." for m in metrics) + "\n")
    return audit


def gh(arguments, input_data=None, timeout=90):
    try:
        process = subprocess.run(["gh", *arguments], input=input_data, capture_output=True,
                                 text=True, timeout=timeout, check=True)
        return process.stdout
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as error:
        detail = getattr(error, "stderr", "") or str(error)
        status=re.search(r"HTTP\s+(\d{3})\b",detail)
        raise GitHubCLIError(f"GitHub CLI failed: {detail[:2000]}",int(status.group(1)) if status else None) from error


def gh_json(arguments, input_data=None):
    return json.loads(gh(arguments, input_data))


def validate_repo(repo):
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ObservatoryError("Repository must be OWNER/REPO on github.com")
    return repo


def workflow_guard(content: bytes, expected: bytes):
    if content != expected:
        raise ObservatoryError("Remote workflow differs from this reviewed plugin. Refuse dispatch until reviewed files match.")
    if b"runs-on: ubuntu-24.04" not in content or b"max-parallel: 2" not in content:
        raise ObservatoryError("Standard Linux runner / concurrency guard is missing")
    if b"upload-artifact" in content:
        raise ObservatoryError("Artifact uploads are forbidden; export uses free job logs")
    for block in re.findall(rb"(?m)^    env:\n((?:      .*\n)*)",content):
        if re.search(rb"\$\{\{[^}]*\brunner\.",block):
            raise ObservatoryError("GitHub runner context is unavailable in job-level env; use a job-safe literal cache path")


def evidence_archive(directory):
    """Serialize bounded evidence without source movies or active file types."""
    directory=Path(directory).resolve()
    buffer=io.BytesIO()
    files=sorted(p for p in directory.rglob("*") if p.is_file())
    if sum(p.stat().st_size for p in files)>MAX_EXTRACTED_BYTES:
        raise ObservatoryError("Evidence exceeds the uncompressed transport cap")
    with tarfile.open(fileobj=buffer,mode="w:gz",compresslevel=6) as archive:
        for path in files:
            if path.is_symlink() or path.suffix.lower() not in {".json",".jsonl",".csv",".html",".md",".jpg"}:
                raise ObservatoryError("Evidence contains a forbidden file type or link")
            archive.add(path,arcname=str(path.relative_to(directory)),recursive=False)
    payload=buffer.getvalue()
    if len(payload)>MAX_ARCHIVE_BYTES:
        raise ObservatoryError("Evidence archive exceeds 12.5 MB shard cap")
    return payload


def emit_log_archive(directory,job_id,shard,stream=None):
    if not re.fullmatch(r"to-[a-f0-9]{16}",str(job_id)) or shard not in (0,1):
        raise ObservatoryError("Invalid owned job or shard for log export")
    payload=evidence_archive(directory)
    encoded=base64.b64encode(payload).decode()
    chunks=[encoded[i:i+4096] for i in range(0,len(encoded),4096)]
    metadata={"job_id":job_id,"shard":shard,"encoding":"tar-gzip-base64","bytes":len(payload),"sha256":sha256(payload),"chunks":len(chunks)}
    target=stream or sys.stdout
    print("TOARCHIVE_BEGIN "+json.dumps(metadata,separators=(",",":")),file=target,flush=True)
    for index,chunk in enumerate(chunks):
        print(f"TOARCHIVE_CHUNK {index} {chunk}",file=target)
    print("TOARCHIVE_END "+metadata["sha256"],file=target,flush=True)
    return metadata


def extract_log_archive(log_path,destination,job_id):
    log_path,destination=Path(log_path),Path(destination).resolve()
    if log_path.stat().st_size>MAX_LOG_BYTES:
        raise ObservatoryError("Job log exceeds the bounded download allowance")
    metadata,chunks,ended=None,{},False
    with log_path.open(errors="strict") as log_source:
        for line in log_source:
            if "TOARCHIVE_BEGIN " in line:
                if metadata is not None:raise ObservatoryError("Duplicate archive header")
                metadata=json.loads(line.split("TOARCHIVE_BEGIN ",1)[1])
                if metadata.get("job_id")!=job_id or metadata.get("shard") not in (0,1) or metadata.get("encoding")!="tar-gzip-base64":
                    raise ObservatoryError("Foreign or invalid log export header")
                integer(metadata.get("bytes"),"archive bytes",1,MAX_ARCHIVE_BYTES)
                integer(metadata.get("chunks"),"archive chunks",1,(MAX_ARCHIVE_BYTES*4//3+4095)//4096+1)
            elif "TOARCHIVE_CHUNK " in line:
                if metadata is None or ended:raise ObservatoryError("Log chunk outside archive")
                index,chunk=line.split("TOARCHIVE_CHUNK ",1)[1].strip().split(" ",1)
                index=int(index)
                if not 0<=index<metadata["chunks"] or index in chunks or len(chunk)>4096:
                    raise ObservatoryError("Duplicate, oversized or out-of-range log chunk")
                chunks[index]=chunk
            elif "TOARCHIVE_END " in line:
                if metadata is None or ended or line.split("TOARCHIVE_END ",1)[1].strip()!=metadata["sha256"]:
                    raise ObservatoryError("Invalid archive end marker")
                ended=True
    if metadata is None:return None
    if not ended or len(chunks)!=metadata["chunks"]:
        raise ObservatoryError("Incomplete log archive; do not claim or fetch partial evidence")
    payload=base64.b64decode("".join(chunks[i] for i in range(metadata["chunks"])),validate=True)
    if len(payload)!=metadata["bytes"] or sha256(payload)!=metadata["sha256"]:
        raise ObservatoryError("Log archive size/hash mismatch")
    destination.mkdir(parents=True,exist_ok=True)
    extracted,seen=0,set()
    with tarfile.open(fileobj=io.BytesIO(payload),mode="r:gz") as archive:
        for member in archive:
            name=Path(member.name)
            if name.is_absolute() or ".." in name.parts or member.name in seen or not member.isfile():
                raise ObservatoryError("Archive contains unsafe path, duplicate, directory, or link")
            seen.add(member.name)
            target=(destination/name).resolve()
            if not target.is_relative_to(destination) or target.suffix.lower() not in {".json",".jsonl",".csv",".html",".md",".jpg"}:
                raise ObservatoryError("Archive file escapes destination or uses a forbidden type")
            extracted+=member.size
            if extracted>MAX_EXTRACTED_BYTES or member.size>MAX_EXTRACTED_BYTES:
                raise ObservatoryError("Archive expansion exceeds bound")
            target.parent.mkdir(parents=True,exist_ok=True)
            source=archive.extractfile(member)
            if source is None:raise ObservatoryError("Missing archive member data")
            with target.open("xb") as output:
                while True:
                    part=source.read(65536)
                    if not part:break
                    output.write(part)
    return metadata


def download_job_log(repo,job_number,path):
    """Spool the authenticated plain-text job log, without printing archive bytes."""
    validate_repo(repo)
    integer(job_number,"GitHub job number",1,10**15)
    try:
        with Path(path).open("wb") as output:
            process=subprocess.run(["gh","api",f"repos/{repo}/actions/jobs/{job_number}/logs"],
                                   stdout=output,stderr=subprocess.PIPE,timeout=180,check=True)
        if Path(path).stat().st_size>MAX_LOG_BYTES:
            raise ObservatoryError("Remote log exceeded download cap")
    except (subprocess.CalledProcessError,FileNotFoundError,subprocess.TimeoutExpired) as error:
        raise ObservatoryError(f"Could not download owned job log: {str(error)[:1000]}") from error


class Client:
    def __init__(self, state_dir=None):
        self.state = Path(state_dir or os.environ.get("TRAILER_OBSERVATORY_STATE", Path.home()/".local/share/trailer-observatory"))

    def preflight(self, repo, ref="main"):
        validate_repo(repo)
        if not re.fullmatch(r"[A-Za-z0-9_./-]{1,128}", ref) or ".." in ref:
            raise ObservatoryError("Invalid Git ref")
        info = gh_json(["api", f"repos/{repo}"])
        if info.get("private") is not False or info.get("visibility") != "public":
            raise ObservatoryError("Refusing private worker repository: hosted minutes may cost money")
        content = gh_json(["api", f"repos/{repo}/contents/{WORKFLOW}?ref={ref}"])
        workflow_guard(base64.b64decode(content["content"]), (ROOT/WORKFLOW).read_bytes())
        for filename in ("worker.py", "observatory.py", "requirements-worker.txt"):
            remote = gh_json(["api", f"repos/{repo}/contents/{filename}?ref={ref}"])
            if sha256(base64.b64decode(remote["content"])) != sha256((ROOT/filename).read_bytes()):
                raise ObservatoryError(f"Remote {filename} differs from reviewed plugin")
        commit = gh_json(["api", f"repos/{repo}/commits/{ref}"])["sha"]
        return {"ok": True, "repo": repo, "ref": ref, "commit": commit,
                "runner": "ubuntu-24.04 (standard public GitHub-hosted)", "remote_parallelism": 2,
                "free_compute_guard": True, "export_transport":"job_logs", "artifact_uploads":False,
                "archive_cap_bytes_per_shard":MAX_ARCHIVE_BYTES,
                "note": "Only public reference URLs go to the public worker. GitHub job logs do not count toward artifact storage; no artifact is uploaded."}

    def submit(self, repo, manifest, ref="main"):
        manifest = validate_manifest(manifest)
        proof = self.preflight(repo, ref)
        owned = self._records()
        active = []
        for job in owned:
            if job["repo"] == repo and job.get("dispatch_status")!="rejected":
                runs = self.status(job["job_id"])
                if not runs["runs"] or any(r["status"] != "completed" for r in runs["runs"]): active.append(job)
        if active:
            raise ObservatoryError("An owned remote dispatch is still active or unlocated. Queue this corpus until it finishes.")
        job_id = "to-" + uuid.uuid4().hex[:16]
        payload = {"ref": ref, "inputs": {"manifest_b64": base64.b64encode(json.dumps(manifest, separators=(",", ":")).encode()).decode(), "job_id": job_id}}
        record = dict(proof, job_id=job_id, manifest=manifest, created_at=time.time(), dispatch_status="unknown")
        self.state.mkdir(parents=True, exist_ok=True)
        dump_json(self.state/f"{job_id}.json", record)
        try:
            gh(["api", "--method", "POST", f"repos/{repo}/actions/workflows/observe.yml/dispatches", "--input", "-"], json.dumps(payload))
        except GitHubCLIError as error:
            # These API responses definitively reject the request. A transport
            # timeout or server error stays unknown and blocks duplicate jobs.
            if error.http_status in {401,403,404,422}:record["dispatch_status"]="rejected"
            record["dispatch_error"]={"http_status":error.http_status,"message":str(error),"recorded_at":time.time()}
            dump_json(self.state/f"{job_id}.json",record)
            raise
        record["dispatch_status"] = "submitted"
        dump_json(self.state/f"{job_id}.json", record)
        return {"job_id": job_id, "repo": repo, "expected_bins": manifest["expected_observations"],
                "status": "submitted", "privacy": "Public trailer metadata only. Your original movie was not submitted."}

    def _records(self):
        return [load_json(p) for p in self.state.glob("to-*.json")] if self.state.is_dir() else []

    def _record(self, job_id):
        if not re.fullmatch(r"to-[a-f0-9]{16}", str(job_id)):
            raise ObservatoryError("Invalid owned job ID")
        path = self.state/f"{job_id}.json"
        if not path.is_file():
            raise ObservatoryError("This client does not own that job")
        return load_json(path)

    def status(self, job_id):
        record = self._record(job_id)
        data = gh_json(["api", f"repos/{record['repo']}/actions/workflows/observe.yml/runs?event=workflow_dispatch&per_page=100"])
        runs = [{k: r.get(k) for k in ("id", "status", "conclusion", "html_url", "head_sha", "created_at")}
                for r in data["workflow_runs"] if r.get("display_title") == job_id]
        for r in runs:
            if r["head_sha"] != record["commit"]:
                raise ObservatoryError("Worker commit changed between preflight and dispatch; inspect before fetching")
        return {"job_id": job_id, "repo": record["repo"], "runs": runs}

    def inspect_runs(self, repo):
        validate_repo(repo)
        return gh_json(["api", f"repos/{repo}/actions/workflows/observe.yml/runs?per_page=20"])

    def cancel(self, job_id):
        status = self.status(job_id)
        for run in status["runs"]:
            if run["status"] != "completed":
                gh(["api", "--method", "POST", f"repos/{status['repo']}/actions/runs/{run['id']}/cancel"])
        return {"job_id": job_id, "cancel_requested": True}

    def fetch(self, job_id, destination):
        record, status = self._record(job_id), self.status(job_id)
        if len(status["runs"]) != 1 or status["runs"][0]["status"] != "completed":
            raise ObservatoryError("Fetch requires exactly one completed owned run")
        run = status["runs"][0]
        jobs=gh_json(["api",f"repos/{record['repo']}/actions/runs/{run['id']}/jobs?per_page=20"])["jobs"]
        chosen=[j for j in jobs if j.get("name","").startswith("observe (")]
        if not chosen or len(chosen)>2:raise ObservatoryError("Expected one or two owned observation jobs")
        out = Path(destination).resolve()
        if out.exists() and any(out.iterdir()):
            raise ObservatoryError("Destination must be new or empty; never overwrite evidence")
        out.mkdir(parents=True, exist_ok=True)
        downloads = out/"shards"
        downloads.mkdir()
        received=set()
        for job in chosen:
            log_path=downloads/f"job-{job['id']}.log"
            download_job_log(record["repo"],job["id"],log_path)
            destination=downloads/f"job-{job['id']}"
            metadata=extract_log_archive(log_path,destination,job_id)
            if metadata:
                if metadata["shard"] in received:raise ObservatoryError("Duplicate shard archive")
                received.add(metadata["shard"])
            log_path.unlink() # requested evidence has been reconstructed and hashed
        if not received:raise ObservatoryError("Owned jobs contain no complete evidence export; inspect worker diagnostics")
        rows = []
        for source in sorted(downloads.glob("*/observations.jsonl")):
            for row in read_rows(source):
                frame = row.get("frame_path")
                if frame:
                    row["frame_path"] = str((source.parent/frame).relative_to(out))
                rows.append(row)
        audit = export_review(rows, out, record["manifest"])
        dump_json(out/"run-provenance.json", {"job_id": job_id, "run": run, "repo": record["repo"], "commit": record["commit"]})
        return {"directory": str(out), "audit": audit, "review": str(out/"review.html"), "run_conclusion": run["conclusion"]}


def observations(path, offset=0, limit=50, trailer_id=None):
    integer(offset, "offset", 0, MAX_SECONDS)
    integer(limit, "limit", 1, 100)
    rows = read_rows(path)
    if trailer_id: rows = [r for r in rows if r["trailer_id"] == trailer_id]
    return {"total": len(rows), "offset": offset, "rows": rows[offset:offset+limit],
            "next_offset": offset+limit if offset+limit < len(rows) else None}


TOOLS = [
    ("preflight", "Verify public worker visibility, reviewed source and free standard Linux runner before dispatch.", {"repo": "string", "ref": "string"}, ["repo"]),
    ("submit", "Dispatch an authorized public trailer corpus remotely; no local decoding or paid API.", {"repo": "string", "manifest": "object", "ref": "string"}, ["repo", "manifest"]),
    ("status", "Read progress of an owned job.", {"job_id": "string"}, ["job_id"]),
    ("inspect_runs", "Read recent remote worker runs without changing them.", {"repo": "string"}, ["repo"]),
    ("fetch", "Reconstruct bounded, hashed evidence from free job logs for one completed owned run in a new local directory.", {"job_id": "string", "destination": "string"}, ["job_id", "destination"]),
    ("observations", "Read at most 100 documented observations, with provenance and separate inference fields.", {"path": "string", "offset": "integer", "limit": "integer", "trailer_id": "string"}, ["path"]),
    ("compare", "Compare structural metric proxies. Does not certify animation quality parity.", {"reference_path": "string", "candidate_path": "string"}, ["reference_path", "candidate_path"]),
    ("cancel", "Cancel only a job dispatched and recorded by this client.", {"job_id": "string"}, ["job_id"]),
]


def mcp_request(request, client):
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                "error": {"code": -32600, "message": "Invalid Request"}}
    if "id" not in request: return None
    response = {"jsonrpc": "2.0", "id": request["id"]}
    method, params = request.get("method"), request.get("params") or {}
    if not isinstance(params,dict) or not isinstance(params.get("_meta",{}),dict):
        response["error"]={"code":-32602,"message":"Invalid params"}
        return response
    requested_version=params.get("_meta",{}).get("io.modelcontextprotocol/protocolVersion")
    modern=requested_version=="2026-07-28" or method=="server/discover"
    if requested_version and requested_version not in {"2026-07-28","2025-06-18","2025-11-25"}:
        response["error"]={"code":-32022,"message":"UnsupportedProtocolVersion","data":{"supported":["2026-07-28","2025-11-25","2025-06-18"]}}
        return response
    if method=="server/discover":
        response["result"]={"resultType":"complete","supportedVersions":["2026-07-28"],"capabilities":{"tools":{}},
                            "_meta":{"io.modelcontextprotocol/serverInfo":{"name":"trailer-observatory","version":VERSION}},
                            "ttlMs":3600000,"cacheScope":"public"}
    elif method == "initialize":
        response["result"] = {"protocolVersion": "2025-11-25" if params.get("protocolVersion")=="2025-11-25" else "2025-06-18", "capabilities": {"tools": {}},
                              "serverInfo": {"name": "trailer-observatory", "version": VERSION}}
    elif method == "ping": response["result"] = {}
    elif method == "tools/list":
        response["result"] = {"tools": [{"name": n, "description": d,
            "inputSchema": {"type": "object", "properties": {k: {"type": v} for k,v in p.items()},
                            "required": req, "additionalProperties": False},
            "annotations": {"readOnlyHint": n in {"preflight", "status", "inspect_runs", "observations", "compare"},
                            "destructiveHint": n == "cancel", "openWorldHint": n in {"preflight", "submit", "status", "inspect_runs", "fetch", "cancel"}}}
            for n,d,p,req in TOOLS]}
    elif method == "tools/call":
        name, arguments = params.get("name"), params.get("arguments", {})
        try:
            spec = next((t for t in TOOLS if t[0] == name), None)
            if not spec or not isinstance(arguments, dict) or set(arguments)-set(spec[2]) or set(spec[3])-set(arguments):
                raise ObservatoryError("Unknown tool or invalid arguments")
            if name in {"observations", "compare"}: result = globals()[name](**arguments)
            else: result = getattr(client, name)(**arguments)
            response["result"] = {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": False}
        except (ValueError, OSError, TypeError, KeyError) as error:
            response["result"] = {"content": [{"type": "text", "text": str(error)}], "isError": True}
    else: response["error"] = {"code": -32601, "message": "Method not found"}
    if modern and "result" in response:response["result"]["resultType"]="complete"
    return response


def serve(client):
    for line in sys.stdin:
        if len(line) > 1_000_000:
            result = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Message exceeds limit"}}
        else:
            try: result = mcp_request(json.loads(line), client)
            except json.JSONDecodeError: result = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if result is not None:
            print(json.dumps(result), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("mcp")
    p = sub.add_parser("validate"); p.add_argument("manifest")
    for command in ("preflight", "submit"):
        p = sub.add_parser(command); p.add_argument("repo"); p.add_argument("--ref", default="main")
        if command == "submit": p.add_argument("manifest")
    p = sub.add_parser("inspect-runs"); p.add_argument("repo")
    for command in ("status", "cancel", "fetch"):
        p = sub.add_parser(command); p.add_argument("job_id")
        if command == "fetch": p.add_argument("destination")
    p = sub.add_parser("observations"); p.add_argument("path"); p.add_argument("--offset", type=int, default=0); p.add_argument("--limit", type=int, default=50); p.add_argument("--trailer-id")
    p = sub.add_parser("compare"); p.add_argument("reference_path"); p.add_argument("candidate_path")
    p = sub.add_parser("audit"); p.add_argument("manifest"); p.add_argument("rows"); p.add_argument("--evidence-root")
    args = vars(parser.parse_args()); client = Client(args.pop("state_dir")); command = args.pop("command")
    try:
        if command == "mcp": serve(client); return
        if command == "validate": result = validate_manifest(load_json(args["manifest"]))
        elif command == "audit": result = audit_rows(read_rows(args["rows"]), load_json(args["manifest"]), args["evidence_root"])
        elif command == "observations": result = observations(**args)
        elif command == "compare": result = compare(**args)
        else:
            if command == "submit": args["manifest"] = load_json(args["manifest"])
            result = getattr(client, command.replace("-", "_"))(**args)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except (ValueError, OSError, KeyError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr); sys.exit(2)


if __name__ == "__main__": main()
