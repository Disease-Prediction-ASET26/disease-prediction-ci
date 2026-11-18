"""Publish scanned images and retain one successful preview per internal PR."""

import argparse
import importlib.util
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

_VERSION_SPEC = importlib.util.spec_from_file_location(
    "registry_version", Path(__file__).with_name("version.py")
)
_VERSION = importlib.util.module_from_spec(_VERSION_SPEC)
_VERSION_SPEC.loader.exec_module(_VERSION)

LEGACY_PR_TAG = re.compile(
    r"^pr-(?P<number>[1-9][0-9]*)-(?P<sha>[0-9a-f]{40})-"
    r"(?P<run>[1-9][0-9]*)-(?P<attempt>[1-9][0-9]*)"
    r"(?P<suffix>-(?:amd64|arm64))?$"
)
SEMVER_PR_TAG = re.compile(
    rf"^(?P<version>{_VERSION.CORE})-pr\."
    r"(?P<number>[1-9][0-9]*)\.(?P<sha>[0-9a-f]{7})\."
    r"(?P<run>[1-9][0-9]*)\.(?P<attempt>[1-9][0-9]*)"
    r"(?P<suffix>-(?:amd64|arm64)|-index\.[1-9][0-9]*\.[1-9][0-9]*)?$"
)
ALIAS = re.compile(r"pr-([1-9][0-9]*)$")


class GitHub:
    def __init__(self, repository, token):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Invalid repository")
        self.repository = repository
        self.owner, self.repo = repository.split("/")
        self.token = token

    def request(self, path, method="GET"):
        request = urllib.request.Request(
            "https://api.github.com" + path,
            method=method,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            return json.loads(body) if body else None

    def pull(self, number):
        return self.request(f"/repos/{self.repository}/pulls/{number}")

    def versions(self):
        versions = []
        page = 1
        while True:
            try:
                batch = self.request(
                    f"/orgs/{self.owner}/packages/container/{self.repo}-pr/versions?per_page=100&page={page}"
                )
            except urllib.error.HTTPError as error:
                if error.code == 404 and page == 1:
                    return []
                raise
            versions.extend(batch)
            if len(batch) < 100:
                return versions
            page += 1

    def delete(self, version_id):
        # The production package name is never accepted as an input.
        self.request(
            f"/orgs/{self.owner}/packages/container/{self.repo}-pr/versions/{int(version_id)}",
            "DELETE",
        )


def preview_number(version):
    tags = version.get("metadata", {}).get("container", {}).get("tags", [])
    if not tags:
        return None
    matches = [preview_tag(tag) for tag in tags]
    if any(match is None for match in matches):
        return None
    numbers = {int(match["number"]) for match in matches if match is not None}
    return numbers.pop() if len(numbers) == 1 else None


def preview_tag(tag):
    for pattern in (SEMVER_PR_TAG, LEGACY_PR_TAG):
        match = pattern.fullmatch(tag)
        if match:
            return match.groupdict()
    alias = ALIAS.fullmatch(tag)
    if alias:
        return {
            "number": alias[1],
            "sha": None,
            "run": None,
            "attempt": None,
            "suffix": None,
        }
    return None


def deletions(versions, pull, repository):
    number = pull["number"]
    if pull.get("head", {}).get("repo", {}).get("full_name") != repository:
        return []
    candidates = [version for version in versions if preview_number(version) == number]
    if pull["state"] == "closed":
        return [version["id"] for version in candidates]
    current = [
        version
        for version in candidates
        if f"pr-{number}" in version["metadata"]["container"]["tags"]
    ]
    if len(current) != 1:
        return []
    tags = current[0]["metadata"]["container"]["tags"]
    current_tags = [
        match
        for tag in tags
        if (match := preview_tag(tag))
        and match["sha"]
        and pull["head"]["sha"].startswith(match["sha"])
        and match["suffix"] is None
    ]
    if len(current_tags) != 1:
        return []
    current_run = tuple(
        current_tags[0][key] for key in ("number", "sha", "run", "attempt")
    )
    return [
        version["id"]
        for version in candidates
        if version["id"] != current[0]["id"]
        and not any(
            (match := preview_tag(tag))
            and tuple(match[key] for key in ("number", "sha", "run", "attempt"))
            == current_run
            and match["suffix"] is not None
            for tag in version["metadata"]["container"]["tags"]
        )
    ]


def cleanup(api, *, only=None):
    versions = api.versions()
    numbers = sorted(
        {
            number
            for version in versions
            if (number := preview_number(version)) is not None
        }
    )
    for number in numbers:
        if only is not None and number != only:
            continue
        pull = api.pull(number)
        for version_id in deletions(versions, pull, api.repository):
            # Preview publication and cleanup share a concurrency group.
            latest = api.pull(number)
            if (
                latest["state"] != pull["state"]
                or latest["head"]["sha"] != pull["head"]["sha"]
            ):
                break
            api.delete(version_id)
            print(f"Deleted preview version {version_id} for PR {number}")


def docker(*args):
    command = ["docker", *args]
    if args and args[0] == "push":
        return subprocess.check_output(
            command, text=True, stderr=subprocess.STDOUT
        ).strip()
    return subprocess.check_output(command, text=True).strip()


def push_image(reference):
    for attempt in range(3):
        try:
            return docker("push", reference)
        except subprocess.CalledProcessError as error:
            output = error.output or ""
            if "unknown blob" not in output.lower() or attempt == 2:
                if output:
                    print(output)
                raise
            delay = 2 ** (attempt + 1)
            print(output)
            print(f"GHCR returned unknown blob; retrying image push in {delay}s.")
            time.sleep(delay)
    raise RuntimeError("Image push retry loop exited unexpectedly")


def verify_remote(manifest, scanned_id, manifest_digest=None):
    # The classic Docker store uses the config digest as image ID. The
    # containerd image store can use the manifest digest as image ID instead.
    config_digest = manifest.get("config", {}).get("digest")
    if not config_digest or (
        config_digest != scanned_id and manifest_digest != scanned_id
    ):
        raise ValueError("Published image differs from the scanned image")


def current_pull(api, number, head):
    pull = api.pull(number)
    return (
        pull["state"] == "open"
        and pull["head"]["repo"]["full_name"] == api.repository
        and pull["head"]["sha"] == head
    )


def target(api, event):
    tag = _VERSION.image_tag(
        os.environ["IMAGE_VERSION"],
        event,
        ref=os.environ["GITHUB_REF"],
        sha=os.environ["GITHUB_SHA"],
        run_id=os.environ["GITHUB_RUN_ID"],
        run_attempt=os.environ["GITHUB_RUN_ATTEMPT"],
    )
    number = event.get("number") if "pull_request" in event else None
    if number:
        head = event["pull_request"]["head"]["sha"]
        package = api.repo + "-pr"
        alias = f"pr-{number}"

        def current():
            return current_pull(api, number, head)
    else:
        ref = os.environ["GITHUB_REF"]
        branches = {
            "refs/heads/develop": (api.repo + "-dev", "develop"),
            "refs/heads/main": (api.repo, "main"),
        }
        if ref not in branches:
            raise ValueError("Image publication requires the develop or main branch")
        branch = ref.removeprefix("refs/heads/")
        package, alias = branches[ref]

        def current():
            return (
                api.request(f"/repos/{api.repository}/git/ref/heads/{branch}")[
                    "object"
                ]["sha"]
                == os.environ["GITHUB_SHA"]
            )

    return (
        f"ghcr.io/{api.owner.lower()}/{package.lower()}",
        tag,
        alias,
        number,
        current,
    )


def remote_manifest(reference):
    return json.loads(docker("buildx", "imagetools", "inspect", "--raw", reference))


def remote_digest(reference):
    info = json.loads(
        docker(
            "buildx",
            "imagetools",
            "inspect",
            reference,
            "--format",
            "{{json .Manifest}}",
        )
    )
    digest = info.get("digest", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Registry did not return a valid image digest")
    return digest


def remote_digest_if_present(reference):
    try:
        return remote_digest(reference)
    except subprocess.CalledProcessError as error:
        output = (error.output or "").lower()
        if any(
            message in output
            for message in ("manifest unknown", "no such manifest", "not found")
        ):
            return None
        raise


def check_tag_digest(tag, candidate_digest, existing_digest):
    if existing_digest is not None and existing_digest != candidate_digest:
        raise ValueError(
            f"Version tag {tag} already points to a different image; bump the service version"
        )
    return existing_digest


def stage(api, event):
    architecture = os.environ["IMAGE_ARCH"]
    if architecture not in {"amd64", "arm64"}:
        raise ValueError("Unsupported image architecture")
    output = Path(os.environ["STAGE_OUTPUT"])
    repository, tag, _, _, current = target(api, event)
    if not current():
        output.write_text(json.dumps({"state": "superseded"}), encoding="utf-8")
        print("Build was superseded; skip publication")
        return
    scanned_id = os.environ["SCANNED_IMAGE_ID"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", scanned_id):
        raise ValueError("Invalid scanned image ID")
    actual_id = docker(
        "image", "inspect", "app-candidate:checked", "--format", "{{.Id}}"
    )
    if actual_id != scanned_id:
        raise ValueError("Local image changed after scanning")
    actual_platform = docker(
        "image",
        "inspect",
        "app-candidate:checked",
        "--format",
        "{{.Os}}/{{.Architecture}}",
    )
    if actual_platform != "linux/" + architecture:
        raise ValueError("Scanned image platform does not match the runner")
    reference = f"{repository}:{tag}-{architecture}"
    docker("tag", "app-candidate:checked", reference)
    print(push_image(reference))
    digest = repository + "@" + remote_digest(reference)
    verify_remote(remote_manifest(digest), scanned_id, digest.rsplit("@", 1)[1])
    output.write_text(
        json.dumps(
            {
                "architecture": architecture,
                "tag": f"{tag}-{architecture}",
                "digest": digest,
                "scanned_id": scanned_id,
            }
        ),
        encoding="utf-8",
    )


def verify_index(manifest, images):
    entries = manifest.get("manifests", [])
    if len(entries) != len(images):
        raise ValueError("Published index does not contain exactly two platform images")
    actual = {}
    for entry in entries:
        platform = entry.get("platform", {})
        architecture = platform.get("architecture")
        if platform.get("os") != "linux" or architecture in actual:
            raise ValueError("Published index has an unexpected platform")
        actual[architecture] = entry.get("digest")
    expected = {
        architecture: ref.rsplit("@", 1)[1] for architecture, ref in images.items()
    }
    if actual != expected:
        raise ValueError("Published index differs from the scanned images")


def index(api, event):
    repository, tag, alias, number, current = target(api, event)
    if not current():
        print("Build was superseded; skip publication")
        return
    images = {}
    for architecture in ("amd64", "arm64"):
        path = Path(os.environ["STAGE_DIR"]) / f"stage-{architecture}.json"
        info = json.loads(path.read_text(encoding="utf-8"))
        digest = info.get("digest", "")
        scanned_id = info.get("scanned_id", "")
        if (
            info.get("architecture") != architecture
            or info.get("tag") != f"{tag}-{architecture}"
            or not re.fullmatch(re.escape(repository) + r"@sha256:[0-9a-f]{64}", digest)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", scanned_id)
        ):
            raise ValueError("Invalid scanned platform image metadata; rerun all jobs")
        if (
            remote_digest(f"{repository}:{tag}-{architecture}")
            != digest.rsplit("@", 1)[1]
        ):
            raise ValueError("Staged platform tag does not match its scanned digest")
        verify_remote(remote_manifest(digest), scanned_id, digest.rsplit("@", 1)[1])
        images[architecture] = digest
    reference = f"{repository}:{tag}"
    candidate_tag = (
        f"{tag}-index.{os.environ['GITHUB_RUN_ID']}.{os.environ['GITHUB_RUN_ATTEMPT']}"
    )
    candidate = f"{repository}:{candidate_tag}"
    with tempfile.TemporaryDirectory() as directory:
        metadata = Path(directory) / "index.json"
        docker(
            "buildx",
            "imagetools",
            "create",
            "--tag",
            candidate,
            "--metadata-file",
            str(metadata),
            *images.values(),
        )
        digest = json.loads(metadata.read_text(encoding="utf-8"))[
            "containerimage.descriptor"
        ]["digest"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Invalid published index digest")
    digest = repository + "@" + digest
    verify_index(remote_manifest(digest), images)
    if remote_digest(candidate) != digest.rsplit("@", 1)[1]:
        raise ValueError("Candidate tag does not match the verified index")
    if not current():
        print("Build was superseded; keep the existing alias")
        return
    existing = remote_digest_if_present(reference)
    check_tag_digest(tag, digest.rsplit("@", 1)[1], existing)
    if existing is None:
        docker("buildx", "imagetools", "create", "--tag", reference, digest)
    if remote_digest(reference) != digest.rsplit("@", 1)[1]:
        raise ValueError("Published version tag does not match the verified index")
    docker("buildx", "imagetools", "create", "--tag", f"{repository}:{alias}", digest)
    if remote_digest(f"{repository}:{alias}") != digest.rsplit("@", 1)[1]:
        raise ValueError("Published alias does not match the verified index")
    if not current():
        print("Build was superseded after publication; skip the GitOps update")
        return
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as handle:
            handle.write(
                f"Published `{reference}@{digest.rsplit('@', 1)[1]}`\n\n"
                f"Scanned image index digest: `{digest.rsplit('@', 1)[1]}`\n"
            )
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
            handle.write(
                f"digest={digest.rsplit('@', 1)[1]}\n"
                f"image-reference={reference}@{digest.rsplit('@', 1)[1]}\n"
                f"tag={tag}\n"
            )
    if number:
        cleanup(api, only=number)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["stage", "index", "cleanup"])
    command = parser.parse_args().command
    api = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
    with open(os.environ["GITHUB_EVENT_PATH"]) as handle:
        event = json.load(handle)
    if (
        "pull_request" in event
        and event["pull_request"]["head"].get("repo", {}).get("full_name")
        != api.repository
    ):
        raise ValueError("Fork events cannot publish or clean images")
    if command == "stage":
        stage(api, event)
    elif command == "index":
        index(api, event)
    else:
        cleanup(api, only=event.get("number") if "pull_request" in event else None)


if __name__ == "__main__":
    main()
