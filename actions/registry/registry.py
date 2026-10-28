"""Publish scanned images and retain one successful preview per internal PR."""

import argparse
import json
import os
import re
import subprocess
import urllib.error
import urllib.request

TAG = re.compile(r"pr-(\d+)-([0-9a-f]{40})-(\d+)-(\d+)$")
ALIAS = re.compile(r"pr-(\d+)$")


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
    matches = [TAG.fullmatch(tag) or ALIAS.fullmatch(tag) for tag in tags]
    if not all(matches):
        return None
    numbers = {int(match[1]) for match in matches}
    return numbers.pop() if len(numbers) == 1 else None


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
    if not any(
        (match := TAG.fullmatch(tag)) and match[2] == pull["head"]["sha"]
        for tag in tags
    ):
        return []
    return [
        version["id"] for version in candidates if version["id"] != current[0]["id"]
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
            # Publication and cleanup jobs use the same repository concurrency group.
            latest = api.pull(number)
            if (
                latest["state"] != pull["state"]
                or latest["head"]["sha"] != pull["head"]["sha"]
            ):
                break
            api.delete(version_id)
            print(f"Deleted preview version {version_id} for PR {number}")


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def verify_remote(manifest, scanned_id):
    if manifest.get("config", {}).get("digest") != scanned_id:
        raise ValueError("Published image differs from the scanned image")


def current_pull(api, number, head):
    pull = api.pull(number)
    return (
        pull["state"] == "open"
        and pull["head"]["repo"]["full_name"] == api.repository
        and pull["head"]["sha"] == head
    )


def publish(api, event):
    scanned_id = os.environ["SCANNED_IMAGE_ID"]
    if (
        docker("image", "inspect", "app-candidate:checked", "--format", "{{.Id}}")
        != scanned_id
    ):
        raise ValueError("Local image changed after scanning")
    number = event.get("number") if "pull_request" in event else None
    if number:
        head = event["pull_request"]["head"]["sha"]
        if not current_pull(api, number, head):
            print("PR closed or superseded; skip publication")
            return
        package = api.repo + "-pr"
        tag = f"pr-{number}-{head}-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}"
        alias = f"pr-{number}"
    else:
        if os.environ["GITHUB_REF"] != "refs/heads/main":
            raise ValueError("Production publication requires main")
        current = api.request(f"/repos/{api.repository}/git/ref/heads/main")["object"][
            "sha"
        ]
        if current != os.environ["GITHUB_SHA"]:
            print("Main build superseded; skip publication")
            return
        package = api.repo
        tag = f"sha-{current}-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}"
        alias = "main"
    repository = f"ghcr.io/{api.owner.lower()}/{package.lower()}"
    reference = repository + ":" + tag
    docker("tag", "app-candidate:checked", reference)
    print(docker("push", reference))
    digests = json.loads(
        docker("image", "inspect", reference, "--format", "{{json .RepoDigests}}")
    )
    digest = next(item for item in digests if item.startswith(repository + "@sha256:"))
    verify_remote(json.loads(docker("manifest", "inspect", digest)), scanned_id)
    if number and not current_pull(api, number, head):
        cleanup(api, only=number)
        return
    if (
        not number
        and api.request(f"/repos/{api.repository}/git/ref/heads/main")["object"]["sha"]
        != os.environ["GITHUB_SHA"]
    ):
        return
    docker("tag", "app-candidate:checked", repository + ":" + alias)
    print(docker("push", repository + ":" + alias))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as handle:
            handle.write(
                f"Published `{reference}`\n\nScanned and published image: `{digest}`\n"
            )
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
            handle.write(f"digest={digest}\n")
    if number:
        cleanup(api, only=number)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["publish", "cleanup"])
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
    if command == "publish":
        publish(api, event)
    else:
        cleanup(api, only=event.get("number") if "pull_request" in event else None)


if __name__ == "__main__":
    main()
