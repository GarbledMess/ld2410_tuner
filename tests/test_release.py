"""Offline release checks: no GitHub writes and no account credentials."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import release


class ReleaseTests(unittest.TestCase):
    def test_publish_is_not_allowed_on_pull_requests(self):
        with patch.dict(release.os.environ, {"GITHUB_EVENT_NAME":"pull_request","GITHUB_REF":"refs/heads/main"}), patch.object(sys,"argv",["release.py","--publish"]), patch.object(release,"publish") as publish:
            with self.assertRaises(ValueError):release.main()
            publish.assert_not_called()

    def test_versions_only_release_on_increase(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / release.MANIFEST
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"version": "1.9.2"}))
            with patch.object(release, "git", return_value=json.dumps({"version": "1.9.2"})):
                self.assertIsNone(release.changed_version(root, "a"*40))
            with patch.object(release, "git", return_value=json.dumps({"version": "1.9.1"})):
                self.assertEqual(release.changed_version(root, "a"*40), "1.9.2")
            with patch.object(release, "git", return_value=json.dumps({"version": "2.0.0"})):
                with self.assertRaises(ValueError): release.changed_version(root, "a"*40)
            self.assertEqual(release.changed_version(root, "0"*40), "1.9.2")
            for bad in ("../x", "v1.0.0", "1.0.0; command", "01.0.0"):
                with self.assertRaises(ValueError): release.version_tuple(bad)

    def test_archive_is_deterministic_and_excludes_private_untracked_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            package=root/"custom_components"/release.DOMAIN
            for name in release.RUNTIME_FILES:
                path=package/name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"synthetic fixture")
            (package/"manifest.json").write_text(json.dumps({"version":"1.9.2"}))
            subprocess.run(["git","init","-q",str(root)],check=True)
            subprocess.run(["git","-C",str(root),"add","custom_components"],check=True)
            (package/"private.json").write_text('{"private":"fixture"}')
            (root/"ld2410_tuner.data").write_text("private fixture")
            with patch.object(release,"validate"):
                _,archive=release.build(root,root/"dist")
                before=archive.read_bytes()
                release.build(root,root/"dist")
            self.assertEqual(before,archive.read_bytes())
            with zipfile.ZipFile(archive) as z:
                self.assertEqual(set(z.namelist()),set(release.RUNTIME_FILES))
                self.assertEqual(json.loads(z.read("manifest.json"))["version"],"1.9.2")

    def test_publish_uploads_draft_before_exposing_release(self):
        calls=[]
        class API:
            def request(self,path,method="GET",payload=None,**kwargs):
                calls.append((path,method,payload,kwargs))
                if path=="/releases" and method=="POST":return {"id":7,"draft":True,"assets":[]}
                return None
        with tempfile.TemporaryDirectory() as directory:
            archive=Path(directory)/release.ASSET;archive.write_bytes(b"archive")
            release.publish(API(),"1.9.2","a"*40,archive)
        mutations=[c for c in calls if c[1]!="GET"]
        self.assertEqual([c[1] for c in mutations],["POST","POST","PATCH"])
        self.assertTrue(mutations[0][2]["draft"])
        self.assertEqual(mutations[0][2]["target_commitish"],"a"*40)
        self.assertTrue(mutations[1][3]["upload"])
        self.assertFalse(mutations[2][2]["draft"])

    def test_upload_failure_leaves_release_unpublished(self):
        calls=[]
        class API:
            def request(self,path,method="GET",payload=None,**kwargs):
                calls.append(method)
                if kwargs.get("upload"):raise RuntimeError("upload failed")
                if method=="POST":return {"id":7,"draft":True,"assets":[]}
                return None
        with tempfile.TemporaryDirectory() as directory:
            archive=Path(directory)/release.ASSET;archive.write_bytes(b"archive")
            with self.assertRaises(RuntimeError):release.publish(API(),"1.9.2","a"*40,archive)
        self.assertNotIn("PATCH",calls)

    def test_published_release_is_idempotent_and_immutable(self):
        for wrong_digest in (False,True):
            calls=[]
            class API:
                def request(self,path,method="GET",payload=None,**kwargs):
                    calls.append(method)
                    if path.startswith("/git/ref/"):return {"object":{"type":"commit","sha":"a"*40}}
                    return {"id":7,"draft":False,"assets":[{"name":release.ASSET,"digest":"wrong" if wrong_digest else "sha256:"+hashlib.sha256(b"archive").hexdigest()}]}
            with tempfile.TemporaryDirectory() as directory:
                archive=Path(directory)/release.ASSET;archive.write_bytes(b"archive")
                if wrong_digest:
                    with self.assertRaises(ValueError):release.publish(API(),"1.9.2","a"*40,archive)
                else:release.publish(API(),"1.9.2","a"*40,archive)
            self.assertTrue(all(method=="GET" for method in calls))

    def test_existing_tag_cannot_be_retargeted(self):
        class API:
            def request(self,*args,**kwargs):return {"object":{"type":"commit","sha":"b"*40}}
        with self.assertRaises(ValueError):release.publish(API(),"1.9.2","a"*40,Path("unused"))

    def test_late_older_release_does_not_replace_latest(self):
        patches=[]
        class API:
            def request(self,path,method="GET",payload=None,**kwargs):
                if path=="/releases/latest":return {"tag_name":"v1.10.0"}
                if method=="POST" and path=="/releases":return {"id":7,"draft":True,"assets":[]}
                if method=="PATCH":patches.append(payload)
                return None
        with tempfile.TemporaryDirectory() as directory:
            archive=Path(directory)/release.ASSET;archive.write_bytes(b"archive")
            release.publish(API(),"1.9.2","a"*40,archive)
        self.assertEqual(patches,[{"draft":False,"make_latest":"false"}])
