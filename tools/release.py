"""Build a deterministic HACS archive; publish only with --publish in release CI."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.request
import zipfile

from validate_package import DOMAIN, RUNTIME_FILES, validate

MANIFEST = f"custom_components/{DOMAIN}/manifest.json"
ASSET = f"{DOMAIN}.zip"


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def version_tuple(version):
    if not isinstance(version, str) or not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version):
        raise ValueError("Release version must use major.minor.patch")
    return tuple(map(int, version.split(".")))


def changed_version(root, before):
    version = json.loads((root / MANIFEST).read_text())["version"]
    current = version_tuple(version)
    if not before or set(before) == {"0"}:
        return version
    if not re.fullmatch(r"[0-9a-f]{40}", before):
        raise ValueError("Invalid previous commit")
    previous = json.loads(git(root, "show", f"{before}:{MANIFEST}"))["version"]
    if version == previous:
        return None
    if current <= version_tuple(previous):
        raise ValueError("Manifest version must increase for a release")
    return version


def build(root, output):
    validate(root)
    version = json.loads((root / MANIFEST).read_text())["version"]
    version_tuple(version)
    package = root / "custom_components" / DOMAIN
    entries = []
    for name in RUNTIME_FILES:
        path = package / name
        if path.is_symlink() or any((package / parent).is_symlink() for parent in Path(name).parents):
            raise ValueError("Release archives must not contain symlinks")
        entries.append((name, path.read_bytes()))
    output.mkdir(parents=True, exist_ok=True)
    archive = output / ASSET
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for name, content in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            target.writestr(info, content)
    return version, archive


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, repository, token):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Invalid GitHub repository")
        self.base = f"https://api.github.com/repos/{repository}"
        self.upload_base = f"https://uploads.github.com/repos/{repository}"
        self.token = token

    def request(self, path, method="GET", payload=None, missing_ok=False, upload=False):
        raw = payload if isinstance(payload, bytes) else (json.dumps(payload).encode() if payload is not None else None)
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                   "User-Agent": "ld2410-tuner-release", "X-GitHub-Api-Version": "2022-11-28"}
        if raw is not None:
            headers["Content-Type"] = "application/zip" if upload else "application/json"
        request = urllib.request.Request((self.upload_base if upload else self.base)+path,
                                         data=raw, headers=headers, method=method)
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=90) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404 and missing_ok:
                return None
            raise RuntimeError(f"GitHub {method} {path} failed with HTTP {error.code}") from None


def publish(api, version, sha, archive):
    tag = f"v{version}"
    ref = api.request(f"/git/ref/tags/{tag}", missing_ok=True)
    if ref:
        obj = ref["object"]
        for _ in range(5):
            if obj["type"] != "tag":
                break
            obj = api.request(f"/git/tags/{obj['sha']}")["object"]
        if obj["type"] != "commit" or obj["sha"] != sha:
            raise ValueError("Existing version tag points to a different commit; use a new version")
    release = api.request(f"/releases/tags/{tag}", missing_ok=True)
    if release and not release["draft"] and not ref:
        raise ValueError("Published release has no verifiable version tag")
    if release and release["draft"] and release.get("target_commitish") != sha:
        raise ValueError("Existing release draft targets a different commit")
    content = archive.read_bytes()
    digest = "sha256:"+hashlib.sha256(content).hexdigest()
    if not release:
        release = api.request("/releases", "POST", {
            "tag_name": tag, "target_commitish": sha, "name": f"LD2410 Tuner {version}",
            "draft": True, "prerelease": False,
            "body": "Install or update through the HACS custom repository, then restart Home Assistant and reload the panel. This release does not submit the repository to the HACS catalogue.",
        })
    asset = next((a for a in release.get("assets", []) if a["name"] == ASSET), None)
    if asset:
        if asset.get("digest") != digest:
            raise ValueError("Existing release asset differs or cannot be verified; refusing to replace it")
    else:
        if not release["draft"]:
            raise ValueError("Published release is missing its asset; use a new version")
        api.request(f"/releases/{release['id']}/assets?name={ASSET}", "POST", content, upload=True)
    if release["draft"]:
        latest = api.request("/releases/latest", missing_ok=True)
        make_latest = latest is None or version_tuple(version) > version_tuple(latest["tag_name"].removeprefix("v"))
        api.request(f"/releases/{release['id']}", "PATCH", {"draft": False, "make_latest": str(make_latest).lower()})
    print(f"Release {tag} and {ASSET} are available")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist"))
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.publish:
        if os.environ.get("GITHUB_EVENT_NAME") != "push" or os.environ.get("GITHUB_REF") != "refs/heads/main":
            raise ValueError("Publishing requires a push to main")
        sha = os.environ["GITHUB_SHA"]
        if git(root, "rev-parse", "HEAD") != sha:
            raise ValueError("Checkout does not match the triggering commit")
        before = os.environ["PREVIOUS_SHA"]
        version = changed_version(root, before)
        if version is None:
            print("Manifest version unchanged; no release needed")
            return
    version, archive = build(root, args.output)
    print(f"Built {archive.name} for {version}")
    if args.publish:
        publish(GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"]), version, sha, archive)


if __name__ == "__main__":
    main()
