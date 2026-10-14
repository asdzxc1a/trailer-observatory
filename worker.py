#!/usr/bin/env python3
"""Remote-only evidence worker. Source movies are deleted and never uploaded."""
from __future__ import annotations
import argparse
import base64
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen
from observatory import (MODEL, MODEL_REVISION, VERSION, MAX_ARCHIVE_BYTES, MAX_CACHE_BYTES,
                         ObservatoryError, sha256, validate_manifest, youtube_id,
                         export_review, dump_json,emit_log_archive)

CAPTION_PROMPT = (
    "Describe only visible image content in two short sentences: subjects, their pose, "
    "screen position, setting, lighting and shot scale. Do not guess names, intentions, "
    "dialogue, plot, camera movement or unseen events. If unclear, say unclear."
)


def hosted_guard():
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
        raise ObservatoryError("Heavy processing only runs on a GitHub-hosted Actions runner")
    if platform.system() != "Linux" or os.environ.get("GITHUB_SERVER_URL") != "https://github.com":
        raise ObservatoryError("Worker requires standard public github.com Linux infrastructure")
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GH_TOKEN")
    if not repo or not token:
        raise ObservatoryError("Missing ephemeral Actions read token")
    req = Request(f"https://api.github.com/repos/{repo}", headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    with urlopen(req, timeout=30) as response: info = json.load(response)
    if info.get("private") is not False or info.get("visibility") != "public":
        raise ObservatoryError("Refusing private repository")


def run(arguments, timeout=300):
    result = subprocess.run(arguments, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise ObservatoryError(f"{arguments[0]} failed: {result.stderr[-1500:]}")
    return result.stdout


def select_shard(manifest, shard, shards):
    # Greedy bin packing keeps both CPU workers close in declared duration.
    buckets, costs = [[] for _ in range(shards)], [0]*shards
    for t in sorted(manifest["trailers"], key=lambda t: -t["seconds"]):
        target = min(range(shards), key=lambda i: costs[i])
        buckets[target].append(t); costs[target] += t["seconds"]
    ids = {t["id"] for t in buckets[shard]}
    selected = [t for t in manifest["trailers"] if t["id"] in ids]
    return dict(manifest, trailers=selected, expected_observations=sum(t["seconds"] for t in selected))


def download_public(trailer, directory):
    """No cookies, authenticated access, playlists, DRM or bypass options."""
    import yt_dlp
    options = {"format": "bestvideo[height<=480][ext=mp4]/best[height<=480]/bestvideo[height<=480]",
               "outtmpl": str(directory/"source.%(ext)s"), "noplaylist": True,
               "quiet": True, "no_warnings": True, "socket_timeout": 30,
               "retries": 1, "fragment_retries": 1, "cachedir": False,
               "max_filesize": 100_000_000, "js_runtimes": {"node": {}},
               "remote_components": {"ejs:github"}}
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(trailer["url"], download=False)
        if info.get("id") != youtube_id(trailer["url"]):
            raise ObservatoryError("Downloader resolved a different source video")
        if info.get("availability") not in {"public", None} or info.get("is_live"):
            raise ObservatoryError("Only public non-live references are supported")
        if info.get("age_limit", 0) or info.get("has_drm"):
            raise ObservatoryError("Restricted/DRM video is not supported")
        if trailer.get("channel_id") and info.get("channel_id") != trailer["channel_id"]:
            raise ObservatoryError("Source does not match declared official channel")
        duration = float(info.get("duration") or 0)
        if duration <= trailer["start_second"]+trailer["seconds"]-1:
            raise ObservatoryError(f"Declared window contains a wholly absent bin at reported duration {duration}")
        if duration > 600:
            raise ObservatoryError("References must be trailers no longer than 10 minutes")
        ydl.process_info(info)
        source = Path(ydl.prepare_filename(info))
        if not source.is_file() or source.stat().st_size > 100_000_000:
            raise ObservatoryError("Source download missing or exceeds 100 MB cap")
    return source, {k: info.get(k) for k in ("id", "title", "channel_id", "channel", "duration", "upload_date", "webpage_url", "fps", "format_id")}


def probe_decoded_end(source):
    data=json.loads(run(["ffprobe","-v","error","-select_streams","v:0","-show_frames",
                         "-show_entries","frame=best_effort_timestamp_time,duration_time,pkt_duration_time:stream=avg_frame_rate",
                         "-of","json",str(source)]))
    frames=data.get("frames",[])
    if not frames or "best_effort_timestamp_time" not in frames[-1]:
        raise ObservatoryError("Cannot establish decoded video EOF")
    final=frames[-1]
    duration=float(final.get("duration_time",final.get("pkt_duration_time",0)))
    if duration<=0:
        from fractions import Fraction
        rate=float(Fraction(data.get("streams",[{}])[0].get("avg_frame_rate","0/1")))
        if rate<=0:raise ObservatoryError("Missing final frame duration")
        duration=1/rate
        precision="last decoded frame timestamp plus average-frame-rate duration fallback"
    else:precision="last decoded frame timestamp plus decoded frame duration"
    return float(final["best_effort_timestamp_time"])+duration,precision


def sample_targets(trailer, decoded_end):
    start,end=trailer["start_second"],trailer["start_second"]+trailer["seconds"]
    if decoded_end<=end-1:
        raise ObservatoryError("Declared window contains at least one wholly absent second bin")
    return [min(second+0.5,second+(decoded_end-second)/2) if decoded_end<second+1 else second+0.5
            for second in range(start,end)]


def extract_frames(source, trailer, directory, decoded_end=None):
    directory.mkdir(parents=True, exist_ok=True)
    midpoint = trailer["start_second"] + 0.5
    targets=sample_targets(trailer,decoded_end) if decoded_end is not None else [trailer["start_second"]+i+0.5 for i in range(trailer["seconds"])]
    regular_count=len(targets)-(targets[-1]!=trailer["start_second"]+trailer["seconds"]-0.5)
    # Select the first decoded source frame at/after each target midpoint.
    # showinfo retains the source PTS before resizing; no resampler duplicates.
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info", "-threads", "2",
         "-ss", str(midpoint), "-i", str(source), "-an", "-sn", "-dn",
         "-vf", "select='gte(t,selected_n)',showinfo,scale=384:216:force_original_aspect_ratio=decrease,pad=384:216:(ow-iw)/2:(oh-ih)/2",
         "-fps_mode", "vfr", "-frames:v", str(regular_count), "-q:v", "8", str(directory/"%06d.jpg")]
    selected_times=[]
    if regular_count:
        process = subprocess.run(command,text=True,capture_output=True,timeout=300)
        if process.returncode:
            raise ObservatoryError(f"ffmpeg failed: {process.stderr[-1500:]}")
        selected_times=parse_frame_times(process.stderr,midpoint,regular_count)
    if regular_count<len(targets):
        target=targets[-1]
        partial=subprocess.run(["ffmpeg","-nostdin","-hide_banner","-loglevel","info","-threads","2",
            "-ss",str(target),"-i",str(source),"-an","-sn","-dn",
            "-vf","showinfo,scale=384:216:force_original_aspect_ratio=decrease,pad=384:216:(ow-iw)/2:(oh-ih)/2",
            "-frames:v","1","-q:v","8",str(directory/f"{len(targets):06d}.jpg")],text=True,capture_output=True,timeout=60)
        if partial.returncode:raise ObservatoryError(f"Partial-bin extraction failed: {partial.stderr[-1500:]}")
        selected_times.extend(parse_frame_times(partial.stderr,target,1))
    frames = sorted(directory.glob("*.jpg"))
    if len(frames) != trailer["seconds"]:
        raise ObservatoryError(f"Frame count {len(frames)} differs from window {trailer['seconds']}")
    if any(not trailer["start_second"]+i <= t < trailer["start_second"]+i+1 for i,t in enumerate(selected_times)):
        raise ObservatoryError("A decoded frame falls outside its declared one-second bin")
    if decoded_end is not None and any(t>=decoded_end for t in selected_times):
        raise ObservatoryError("Evidence timestamp reaches past decoded EOF")
    return frames, selected_times, targets


def parse_frame_times(stderr, midpoint, count):
    relative = [float(value) for value in re.findall(r"\[Parsed_showinfo[^\]]*\].*?\bpts_time:([-+0-9.eE]+)",stderr)]
    if len(relative) < count:
        raise ObservatoryError("Missing showinfo timestamp evidence")
    times = [round(midpoint+t,6) for t in relative[:count]]
    if any(b<=a for a,b in zip(times,times[1:])):
        raise ObservatoryError("Decoded frame timestamps are not strictly chronological")
    return times


def failed_rows(trailer, error, source=None):
    return [{"schema_version": 1, "trailer_id": trailer["id"], "second": second,
             "source_url": trailer["url"], "source_video_id": youtube_id(trailer["url"]),
             "sample_time_seconds": second+0.5, "observation": "", "inference": "",
             "evidence_status": "failed", "annotation_status": "failed", "error": str(error)[:1500],
             "model_id": MODEL, "model_revision": MODEL_REVISION, "worker_version": VERSION,
             "source_metadata": source or {}}
            for second in range(trailer["start_second"], trailer["start_second"]+trailer["seconds"])]


class Captioner:
    def __init__(self):
        import torch
        from transformers import AutoProcessor, AutoModelForVision2Seq
        torch.set_num_threads(4)
        torch.set_num_interop_threads(1)
        self.torch = torch
        self.processor = AutoProcessor.from_pretrained(MODEL, revision=MODEL_REVISION, trust_remote_code=False)
        self.processor.image_processor.size = {"longest_edge": 512}
        if hasattr(self.processor.image_processor, "do_image_splitting"):
            self.processor.image_processor.do_image_splitting = False
        self.model = AutoModelForVision2Seq.from_pretrained(
            MODEL, revision=MODEL_REVISION, trust_remote_code=False, use_safetensors=True,
            torch_dtype=torch.float32, attn_implementation="eager").eval().to("cpu")
        cache_root=Path(os.environ.get("HF_HOME",Path.home()/".cache/huggingface"))
        physical_bytes=sum(p.stat().st_size for p in cache_root.rglob("*") if p.is_file() and not p.is_symlink())
        if physical_bytes>MAX_CACHE_BYTES:
            raise ObservatoryError("Model cache exceeds the bounded 950 MB worker allowance")
        self.prompt = self.processor.apply_chat_template(
            [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": CAPTION_PROMPT}]}], add_generation_prompt=True)
        self.processor.tokenizer.padding_side = "left"

    def caption(self, paths):
        from PIL import Image
        images = [Image.open(path).convert("RGB") for path in paths]
        try:
            inputs = self.processor(text=[self.prompt]*len(images), images=[[img] for img in images],
                                    return_tensors="pt", padding=True)
            with self.torch.inference_mode():
                generated = self.model.generate(**inputs, max_new_tokens=64, do_sample=False, use_cache=True)
            return [text.strip() for text in self.processor.batch_decode(
                generated[:,inputs["input_ids"].shape[1]:], skip_special_tokens=True)]
        finally:
            for image in images: image.close()


def make_rows(trailer, metadata, paths, sample_times, targets, root, captioner, caption_cache):
    import imagehash
    import numpy as np
    from PIL import Image
    rows, pending, pending_by_hash = [], [], {}
    previous, previous_phash = None, None
    for index, path in enumerate(paths):
        digest = sha256(path.read_bytes())
        with Image.open(path) as image:
            perceptual = imagehash.phash(image)
            current = np.asarray(image.convert("RGB").resize((64,36)), dtype=np.float32)/255
        pixel_change = float(np.mean(np.abs(current-previous))) if previous is not None else 0
        phash_distance = int(perceptual-previous_phash) if previous_phash is not None else 0
        second = trailer["start_second"]+index
        row = {"schema_version": 1, "trailer_id": trailer["id"], "second": second,
               "bin_start_seconds": second, "bin_end_seconds_exclusive": second+1,
               "source_url": trailer["url"], "source_video_id": metadata["id"],
               "source_metadata": metadata, "sample_time_seconds": targets[index],
               "nominal_sample_time_seconds": second+0.5,
               "decoded_frame_time_seconds": sample_times[index],
               "actual_decoded_duration_seconds": metadata["actual_decoded_duration_seconds"],
               "partial_bin": metadata["actual_decoded_duration_seconds"]<second+1,
               "observed_bin_end_seconds_exclusive": min(second+1,metadata["actual_decoded_duration_seconds"]),
               "sampling_method": "accurate ffmpeg seek at window start + 0.5 seconds, then select the first decoded frame at or after each one-second target; showinfo PTS recorded",
               "timestamp_precision_note": "sample_time_seconds is the target midpoint; decoded_frame_time_seconds is the measured presentation timestamp. Not a claim of a whole second viewed in motion.",
               "frame_path": str(path.relative_to(root)), "frame_sha256": digest,
               "phash": str(perceptual), "phash_distance_previous": phash_distance,
               "pixel_change": round(pixel_change,6), "cut_candidate": bool(pixel_change>=0.18 and phash_distance>=18),
               "observation": "", "inference": "", "evidence_status": "captured",
               "annotation_status": "machine_unreviewed", "model_id": MODEL, "model_revision": MODEL_REVISION,
               "worker_version": VERSION, "human_reviewed": False,
               "limitations": "A small model draft of a still frame; may misidentify animated subjects. No inferred acting motion, dialogue or story intent."}
        previous, previous_phash = current, perceptual
        rows.append(row)
        # SHA verifies exact pixels before pHash grouping is permitted to reuse a
        # caption. Similar frames keep individual captions to preserve acting.
        cache_key = (str(perceptual),digest)
        if cache_key in caption_cache:
            row["observation"] = caption_cache[cache_key]
            row["annotation_status"] = "reused_exact_frame_unreviewed"
        else:
            if cache_key not in pending_by_hash:
                pending.append((cache_key,path)); pending_by_hash[cache_key] = []
            pending_by_hash[cache_key].append(row)
    for start in range(0,len(pending),2):
        batch = pending[start:start+2]
        began = time.monotonic()
        try:
            captions = captioner.caption([path for _,path in batch])
            if len(captions) != len(batch) or any(not c for c in captions):
                raise ObservatoryError("Empty or incomplete model output")
            for (key,_), text in zip(batch,captions):
                caption_cache[key] = text
                for i,row in enumerate(pending_by_hash[key]):
                    row["observation"] = text
                    if i: row["annotation_status"] = "reused_exact_frame_unreviewed"
                    row["caption_wall_seconds_batch"] = round(time.monotonic()-began,3)
        except Exception as error:
            for key,_ in batch:
                for row in pending_by_hash[key]:
                    row["annotation_status"] = "failed"
                    row["error"] = f"Model inference failed: {error}"[:1500]
        print(json.dumps({"trailer": trailer["id"], "captioned_unique_frames": min(start+2,len(pending)), "unique_frames": len(pending)}), flush=True)
    return rows


def worker(manifest, out, shard, shards):
    hosted_guard()
    full_manifest = validate_manifest(manifest)
    manifest = select_shard(full_manifest,shard,shards)
    if not manifest["trailers"]: return
    out.mkdir(parents=True,exist_ok=True)
    rows, cache, captioner, started = [], {}, None, time.monotonic()
    source_manifest = []
    versions = {pkg: importlib.metadata.version(pkg) for pkg in ("yt-dlp","transformers","torch","Pillow","imagehash")}
    for trailer in manifest["trailers"]:
        metadata = None
        try:
            with tempfile.TemporaryDirectory(prefix="public-trailer-") as temporary:
                source,metadata = download_public(trailer,Path(temporary))
                metadata["source_file_sha256"] = sha256(source.read_bytes())
                decoded_end,eof_precision=probe_decoded_end(source)
                metadata["actual_decoded_duration_seconds"]=decoded_end
                metadata["decoded_eof_precision"]=eof_precision
                paths, sample_times, targets = extract_frames(source,trailer,out/"frames"/trailer["id"],decoded_end)
                if captioner is None: captioner = Captioner()
                rows.extend(make_rows(trailer,metadata,paths,sample_times,targets,out,captioner,cache))
            source_manifest.append({"trailer_id":trailer["id"],"metadata":metadata,"status":"processed"})
        except Exception as error:
            # Do not create successful-looking filler captions after a failure.
            rows.extend(failed_rows(trailer,error,metadata))
            source_manifest.append({"trailer_id":trailer["id"],"status":"failed","error":str(error)[:1500]})
        export_review(rows,out,dict(manifest,trailers=[t for t in manifest["trailers"] if any(r["trailer_id"]==t["id"] for r in rows)],expected_observations=len(rows)))
    audit = export_review(rows,out,manifest)
    dump_json(out/"worker-provenance.json", {"worker_version":VERSION,"model_id":MODEL,"model_revision":MODEL_REVISION,
        "package_versions":versions,"python":sys.version,"ffmpeg_version":run(["ffmpeg","-version"]).splitlines()[0],
        "run_id":os.environ.get("GITHUB_RUN_ID"),"commit":os.environ.get("GITHUB_SHA"),
        "shard":shard,"shards":shards,"wall_seconds":round(time.monotonic()-started,3),
        "source_manifest":source_manifest,"caption_prompt":CAPTION_PROMPT,"source_movies_uploaded":False})
    if not audit["complete"]:
        print(json.dumps(audit),file=sys.stderr)
        return 3
    return 0


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest-b64",default=os.environ.get("MANIFEST_B64"))
    p.add_argument("--out",type=Path,required=True)
    p.add_argument("--shard",type=int,default=0)
    p.add_argument("--shards",type=int,default=2)
    p.add_argument("--emit-logs",action="store_true")
    p.add_argument("--job-id",default=os.environ.get("OBSERVATORY_JOB_ID"))
    args=p.parse_args()
    if args.shards not in (1,2) or not 0<=args.shard<args.shards: p.error("Only 1 or 2 shards are supported")
    try:
        manifest=json.loads(base64.b64decode(args.manifest_b64,validate=True))
        code=worker(manifest,args.out,args.shard,args.shards) or 0
        if args.emit_logs and args.out.is_dir():emit_log_archive(args.out,args.job_id,args.shard)
        sys.exit(code)
    except (ValueError,OSError,TypeError) as error:
        print(str(error),file=sys.stderr);sys.exit(2)


if __name__=="__main__": main()
