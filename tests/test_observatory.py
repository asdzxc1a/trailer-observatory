"""Synthetic-only tests: these do not download, decode or claim trailer research."""
import base64
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import observatory as o
import worker


def manifest():
    return {"schema_version":1,"corpus_id":"synthetic-test","expected_observations":3,
            "trailers":[{"id":"test","url":"https://www.youtube.com/watch?v=abcdefghijk",
                         "title":"Synthetic protocol fixture", "start_second":4,"seconds":3}]}


def rows(root):
    result=[]
    for second in range(4,7):
        path=root/f"{second}.bin";path.write_bytes(f"synthetic-evidence-{second}".encode())
        result.append({"trailer_id":"test","second":second,"source_url":manifest()["trailers"][0]["url"],
            "source_video_id":"abcdefghijk","sample_time_seconds":second+0.5,
            "decoded_frame_time_seconds":second+0.5,
            "frame_path":path.name,"frame_sha256":o.sha256(path.read_bytes()),
            "observation":"Synthetic fixture only","inference":"","evidence_status":"captured",
            "annotation_status":"machine_unreviewed","model_id":"synthetic","model_revision":"synthetic",
            "worker_version":"test","cut_candidate":second==5,"pixel_change":0.3 if second==5 else 0})
    return result


class ManifestTests(unittest.TestCase):
    def test_normalizes_short_url(self):
        m=manifest();m["trailers"][0]["url"]="https://youtu.be/abcdefghijk?si=test"
        self.assertEqual(o.validate_manifest(m)["trailers"][0]["url"],manifest()["trailers"][0]["url"])

    def test_exact_chronological_bins(self):
        self.assertEqual(o.expected_bins(manifest()),{("test",4),("test",5),("test",6)})

    def test_declared_count_must_match(self):
        m=manifest();m["expected_observations"]=1561
        with self.assertRaises(o.ObservatoryError):o.validate_manifest(m)

    def test_1561_bin_shape_is_only_a_declared_plan(self):
        m=manifest();m["trailers"]=[]
        for i in range(11):
            m["trailers"].append({"id":f"test-{i}","url":f"https://youtu.be/{i:011d}","start_second":0,"seconds":141 if i==10 else 142})
        m["expected_observations"]=1561
        self.assertEqual(len(o.expected_bins(o.validate_manifest(m))),1561)
        self.assertEqual(o.audit_rows([],m)["documented_observations"],0)

    def test_rejects_overlapping_same_video_windows(self):
        m=manifest();t=dict(m["trailers"][0],id="test-b",start_second=6)
        m["trailers"].append(t);m["expected_observations"]=6
        with self.assertRaises(o.ObservatoryError):o.validate_manifest(m)

    def test_rejects_private_payload_fields(self):
        m=manifest();m["movie_story"]="Private original film"
        with self.assertRaises(o.ObservatoryError):o.validate_manifest(m)

    def test_rejects_fractional_or_boolean_timestamp(self):
        for start in [True,4.1,"4",-1]:
            m=manifest();m["trailers"][0]["start_second"]=start
            with self.assertRaises(o.ObservatoryError):o.validate_manifest(m)

    def test_rejects_unsafe_urls(self):
        for url in ["https://evil.test/abcdefghijk","http://youtu.be/abcdefghijk",
                    "https://youtube.com.evil.test/watch?v=abcdefghijk",
                    "https://user:pass@youtube.com/watch?v=abcdefghijk",
                    "https://youtube.com/watch?v=abcdefghijk&list=playlist",
                    "https://youtube.com:443/watch?v=abcdefghijk",
                    "https://youtube.com/playlist?list=test"]:
            with self.assertRaises(o.ObservatoryError):o.youtube_id(url)

    def test_shards_preserve_all_bins(self):
        m=manifest();m["trailers"].append({"id":"test-b","url":"https://youtu.be/12345678901","start_second":0,"seconds":4});m["expected_observations"]=7
        a,b=worker.select_shard(m,0,2),worker.select_shard(m,1,2)
        self.assertEqual(o.expected_bins(a)|o.expected_bins(b),o.expected_bins(m))
        self.assertFalse(o.expected_bins(a)&o.expected_bins(b))


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.rows=rows(self.root)
    def tearDown(self):self.temp.cleanup()
    def audit(self):return o.audit_rows(self.rows,manifest(),self.root)

    def test_complete_hash_verified_rows(self):self.assertTrue(self.audit()["complete"])
    def test_missing_row_is_not_counted(self):
        self.rows.pop();audit=self.audit();self.assertEqual(audit["documented_observations"],2);self.assertEqual(audit["missing_bins"],[("test",6)])
    def test_duplicate_row_refuses_completion(self):
        self.rows.append(dict(self.rows[0]));self.assertFalse(self.audit()["complete"])
    def test_failed_caption_is_not_observation(self):
        self.rows[0]["annotation_status"]="failed";audit=self.audit();self.assertEqual(audit["documented_observations"],2);self.assertEqual(audit["failed_bins"],1)
    def test_missing_frame_is_not_observation(self):
        (self.root/"4.bin").unlink();self.assertEqual(self.audit()["documented_observations"],2)
    def test_mutated_frame_refuses_completion(self):
        (self.root/"4.bin").write_bytes(b"changed");self.assertFalse(self.audit()["complete"])
    def test_wrong_midpoint_refuses_completion(self):
        self.rows[0]["sample_time_seconds"]=4;self.assertFalse(self.audit()["complete"])
    def test_actual_decoded_time_must_be_inside_bin(self):
        self.rows[0]["decoded_frame_time_seconds"]=5.1;self.assertFalse(self.audit()["complete"])
    def test_partial_bin_inside_actual_eof_is_evidence(self):
        row=self.rows[-1];row.update(partial_bin=True,actual_decoded_duration_seconds=6.2,sample_time_seconds=6.1,decoded_frame_time_seconds=6.12)
        self.assertTrue(self.audit()["complete"])
        row["decoded_frame_time_seconds"]=6.21;self.assertFalse(self.audit()["complete"])
    def test_frame_traversal_refuses_completion(self):
        self.rows[0]["frame_path"]="../outside";self.assertFalse(self.audit()["complete"])
    def test_wrong_source_refuses_completion(self):
        self.rows[0]["source_url"]="https://youtu.be/12345678901";self.assertFalse(self.audit()["complete"])
    def test_failure_rows_have_timestamps_without_fake_captions(self):
        failed=worker.failed_rows(manifest()["trailers"][0],"access denied")
        audit=o.audit_rows(failed,manifest());self.assertEqual(audit["documented_observations"],0);self.assertEqual(audit["failed_bins"],3)
        self.assertEqual([r["second"] for r in failed],[4,5,6]);self.assertTrue(all(not r["observation"] for r in failed))
    @patch.object(worker.subprocess,"run")
    def test_extraction_refuses_missing_frame_without_local_decoding(self,process):
        directory=self.root/"synthetic-frames";directory.mkdir()
        (directory/"000001.jpg").write_bytes(b"synthetic");(directory/"000002.jpg").write_bytes(b"synthetic")
        process.return_value.returncode=0
        process.return_value.stderr="".join(f"[Parsed_showinfo_1 @ synthetic] pts_time:{i}\n" for i in range(3))
        with self.assertRaises(o.ObservatoryError):worker.extract_frames(self.root/"not-a-video",manifest()["trailers"][0],directory)
    def test_metrics_are_explicit_proxies(self):
        metrics=o.structural_metrics(self.rows)[0];self.assertEqual(metrics["sampled_cut_candidates"],1);self.assertIn("Not acting",metrics["limits"])
    def test_export_and_pagination_preserve_records(self):
        audit=o.export_review(self.rows,self.root,manifest());self.assertTrue(audit["complete"])
        page=o.observations(self.root/"observations.jsonl",offset=1,limit=1);self.assertEqual(page["rows"][0]["second"],5);self.assertEqual(page["next_offset"],2)
        self.assertEqual(len(o.read_rows(self.root/"observations.jsonl")),3)


class GuardProtocolTests(unittest.TestCase):
    def test_changed_paid_workflow_is_refused(self):
        expected=(o.ROOT/o.WORKFLOW).read_bytes();changed=expected.replace(b"ubuntu-24.04",b"ubuntu-32core")
        with self.assertRaises(o.ObservatoryError):o.workflow_guard(changed,expected)
    def test_reviewed_standard_workflow_is_accepted(self):
        expected=(o.ROOT/o.WORKFLOW).read_bytes();o.workflow_guard(expected,expected)
    def test_job_env_rejects_runner_context_before_remote_dispatch(self):
        valid=(o.ROOT/o.WORKFLOW).read_bytes()
        invalid=valid.replace(b"HF_HOME: /tmp/trailer-observatory-hf-cache",b"HF_HOME: ${{ runner.temp }}/hf-cache")
        # An exact reviewed-byte match alone cannot establish Actions syntax.
        with self.assertRaises(o.ObservatoryError):o.workflow_guard(invalid,invalid)
    @patch.object(o,"gh_json")
    def test_private_worker_is_refused_before_dispatch(self,api):
        api.return_value={"private":True,"visibility":"private"}
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(o.ObservatoryError):o.Client(t).preflight("owner/worker")
        self.assertEqual(api.call_count,1)
    @patch.object(o,"gh")
    def test_cannot_cancel_unowned_job(self,gh):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(o.ObservatoryError):o.Client(t).cancel("to-0123456789abcdef")
        gh.assert_not_called()
    def test_mac_never_runs_heavy_worker(self):
        with patch.dict("os.environ",{},clear=True):
            with self.assertRaises(o.ObservatoryError):worker.hosted_guard()
    def test_ffmpeg_timestamp_provenance_parser(self):
        log="[Parsed_showinfo_1 @ test] n: 0 pts: 0 pts_time:0.008\n[Parsed_showinfo_1 @ test] n: 1 pts: 1000 pts_time:1.008\n"
        self.assertEqual(worker.parse_frame_times(log,4.5,2),[4.508,5.508])
        with self.assertRaises(o.ObservatoryError):worker.parse_frame_times("",4.5,2)
    def test_partial_eof_target_and_absent_bin_policy(self):
        self.assertEqual(worker.sample_targets(manifest()["trailers"][0],6.2),[4.5,5.5,6.1])
        with self.assertRaises(o.ObservatoryError):worker.sample_targets(manifest()["trailers"][0],6)
    def test_mcp_initialization_and_tools(self):
        response=o.mcp_request({"jsonrpc":"2.0","id":1,"method":"initialize"},o.Client())
        self.assertEqual(response["result"]["serverInfo"]["name"],"trailer-observatory")
        response=o.mcp_request({"jsonrpc":"2.0","id":2,"method":"tools/list"},o.Client())
        self.assertIn("compare",[t["name"] for t in response["result"]["tools"]])
    def test_mcp_notifications_have_no_response(self):self.assertIsNone(o.mcp_request({"jsonrpc":"2.0","method":"notifications/initialized"},o.Client()))
    def test_stateless_discovery_and_inline_call(self):
        meta={"io.modelcontextprotocol/protocolVersion":"2026-07-28"}
        discovery=o.mcp_request({"jsonrpc":"2.0","id":1,"method":"server/discover","params":{"_meta":meta}},o.Client())
        self.assertEqual(discovery["result"]["supportedVersions"],["2026-07-28"])
        # New process/client receives an inline request with no initialization.
        listing=o.mcp_request({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{"_meta":meta}},o.Client())
        self.assertEqual(listing["result"]["resultType"],"complete")
    def test_unsupported_stateless_version_reports_supported_versions(self):
        response=o.mcp_request({"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2099-01-01"}}},o.Client())
        self.assertEqual(response["error"]["code"],-32022)
    def test_mcp_invalid_call_is_tool_error(self):
        response=o.mcp_request({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"cancel","arguments":{"job_id":"../foreign"}}},o.Client())
        self.assertTrue(response["result"]["isError"])
    def test_stdio_is_newline_json_and_recovers_after_parse_error(self):
        process=subprocess.run([sys.executable,str(o.ROOT/"observatory.py"),"mcp"],input='bad json\n'+json.dumps({"jsonrpc":"2.0","id":1,"method":"ping"})+'\n',text=True,capture_output=True,check=True)
        responses=[json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(responses[0]["error"]["code"],-32700);self.assertEqual(responses[1]["result"],{})
        self.assertEqual(process.stderr,"")


class LogTransportTests(unittest.TestCase):
    job_id="to-0123456789abcdef"
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def make_log(self,payload):
        encoded=base64.b64encode(payload).decode()
        metadata={"job_id":self.job_id,"shard":0,"encoding":"tar-gzip-base64","bytes":len(payload),"sha256":o.sha256(payload),"chunks":1}
        return "TOARCHIVE_BEGIN "+json.dumps(metadata)+"\nTOARCHIVE_CHUNK 0 "+encoded+"\nTOARCHIVE_END "+metadata["sha256"]+"\n"
    def extract(self,text):
        log=self.root/"job.log";log.write_text(text)
        return o.extract_log_archive(log,self.root/"reconstructed",self.job_id)
    def test_roundtrip_from_timestamped_job_log(self):
        source=self.root/"source";source.mkdir();(source/"audit.json").write_text('{"synthetic":true}')
        (source/"frame.jpg").write_bytes(b"synthetic image bytes only")
        stream=io.StringIO();metadata=o.emit_log_archive(source,self.job_id,0,stream)
        text="".join("2026-10-14T00:00:00Z "+line+"\n" for line in stream.getvalue().splitlines())
        self.assertEqual(self.extract(text)["sha256"],metadata["sha256"])
        self.assertEqual((self.root/"reconstructed/frame.jpg").read_bytes(),b"synthetic image bytes only")
    def test_incomplete_log_chunks_are_refused(self):
        payload=io.BytesIO()
        with tarfile.open(fileobj=payload,mode="w:gz"):pass
        text=self.make_log(payload.getvalue())
        text="\n".join(line for line in text.splitlines() if not line.startswith("TOARCHIVE_CHUNK"))
        with self.assertRaises(o.ObservatoryError):self.extract(text)
    def test_foreign_job_archive_is_refused(self):
        payload=io.BytesIO()
        with tarfile.open(fileobj=payload,mode="w:gz"):pass
        with self.assertRaises(o.ObservatoryError):self.extract(self.make_log(payload.getvalue()).replace(self.job_id,"to-fedcba9876543210"))
    def test_archive_traversal_is_refused(self):
        payload=io.BytesIO()
        with tarfile.open(fileobj=payload,mode="w:gz") as archive:
            info=tarfile.TarInfo("../escape.json");info.size=2;archive.addfile(info,io.BytesIO(b"{}"))
        with self.assertRaises(o.ObservatoryError):self.extract(self.make_log(payload.getvalue()))
        self.assertFalse((self.root/"escape.json").exists())
    def test_archive_symlink_is_refused(self):
        payload=io.BytesIO()
        with tarfile.open(fileobj=payload,mode="w:gz") as archive:
            info=tarfile.TarInfo("link.json");info.type=tarfile.SYMTYPE;info.linkname="/tmp/foreign";archive.addfile(info)
        with self.assertRaises(o.ObservatoryError):self.extract(self.make_log(payload.getvalue()))
    def test_corrupt_payload_hash_is_refused(self):
        payload=io.BytesIO()
        with tarfile.open(fileobj=payload,mode="w:gz"):pass
        text=self.make_log(payload.getvalue());digest=o.sha256(payload.getvalue())
        with self.assertRaises(o.ObservatoryError):self.extract(text.replace(digest,"0"*64))
    def test_source_movie_cannot_be_exported(self):
        source=self.root/"source";source.mkdir();(source/"source.mp4").write_bytes(b"synthetic")
        with self.assertRaises(o.ObservatoryError):o.evidence_archive(source)
    def test_declared_archive_expansion_is_bounded(self):
        # A synthetic header only; no huge allocation or real media.
        info=tarfile.TarInfo("oversized.json");info.size=o.MAX_EXTRACTED_BYTES+1
        import gzip
        payload=gzip.compress(info.tobuf()+b"\0"*1024)
        with self.assertRaises(o.ObservatoryError):self.extract(self.make_log(payload))


if __name__=="__main__":unittest.main()
