"""Merge one image update after the GitOps checks pass for its current head."""

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

OWNER = "Disease-Prediction-ASET26"
REPOSITORY = OWNER + "/disease-prediction-gitops"
REPO_PATH = "/repos/" + REPOSITORY
SHA = re.compile(r"[0-9a-f]{40}$")
REQUIRED_CHECKS = {"validate", "secret-scan / secret-scan"}
MERGE_RETRY_LIMIT = 4


def http_error_detail(error):
    try:
        body = error.read(8192)
    except (OSError, ValueError, AttributeError):
        body = b""
    finally:
        error.close()
    message = str(error.reason or "request rejected")
    try:
        payload = json.loads(body)
        if isinstance(payload, dict) and isinstance(payload.get("message"), str):
            message = payload["message"]
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    message = re.sub(r"[^\x20-\x7e]", " ", message)[:400].replace("%", "%25")
    request_id = (error.headers or {}).get("X-GitHub-Request-Id", "")
    request_id = re.sub(r"[^A-Za-z0-9:-]", "", request_id)[:80]
    detail = f"GitHub HTTP {error.code}: {message}"
    return f"{detail} (request {request_id})" if request_id else detail


def targets():
    result = {}
    for environment, directory, suffix in (
        ("dev", "disease-prediction", "-dev"),
        ("prod", "disease-prediction-prod", ""),
    ):
        for deployment, package in (("backend", "be"), ("frontend", "fe")):
            path = f"clusters/oracle/{directory}/deployments.yaml"
            result[(path, deployment)] = (
                f"ghcr.io/disease-prediction-aset26/disease-prediction-{package}{suffix}",
                f"automation/update-{deployment}-{environment}",
                f"{OWNER}/disease-prediction-{package}",
            )
        path = f"clusters/proxmox/disease-prediction-ai/{environment}/deployment.yaml"
        result[(path, "ai")] = (
            "ghcr.io/disease-prediction-aset26/disease-prediction-ai-runtime",
            f"automation/update-model-runtime-{environment}",
            f"{OWNER}/disease-prediction-ai",
        )
    return result


class API:
    def __init__(self, token):
        if not token:
            raise ValueError("A GitHub App token is required")
        self.token = token

    def request(self, path, *, method="GET", body=None, authenticated=True):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "disease-prediction-gitops-publisher",
        }
        if authenticated:
            headers["Authorization"] = "Bearer " + self.token
        if body is not None:
            headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            "https://api.github.com" + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                content = response.read()
        except urllib.error.HTTPError as error:
            # The source and GitOps repositories are public. Reading check runs
            # can fall back to public access without broadening the app token.
            if method == "GET" and authenticated and error.code in (403, 404):
                return self.request(path, authenticated=False)
            raise
        return json.loads(content) if content else None

    def pull(self, number):
        return self.request(f"{REPO_PATH}/pulls/{number}")


def settings(environ):
    number = environ["PR_NUMBER"]
    head = environ["EXPECTED_HEAD"]
    manifest = environ["MANIFEST_PATH"]
    deployment = environ["DEPLOYMENT"]
    image = environ["IMAGE_REF"]
    if not re.fullmatch(r"[1-9][0-9]*", number) or not SHA.fullmatch(head):
        raise ValueError("Invalid pull request number or expected head")
    target = targets().get((manifest, deployment))
    if target is None:
        raise ValueError("Unsupported GitOps manifest and Deployment")
    package, branch, source_repository = target
    if not re.fullmatch(re.escape(package) + r"@sha256:[0-9a-f]{64}", image):
        raise ValueError("Image digest does not match the target package")
    source = (
        environ.get("SOURCE_REPOSITORY", ""),
        environ.get("SOURCE_BRANCH", ""),
        environ.get("SOURCE_SHA", ""),
    )
    if any(source) and (
        source[0] != source_repository
        or source[1] != ("develop" if branch.endswith("-dev") else "main")
        or not SHA.fullmatch(source[2])
    ):
        raise ValueError("Invalid source repository, branch, or SHA")
    if deployment != "ai" and not all(source):
        raise ValueError("Application image updates require a current source head")
    timeout = int(environ.get("TIMEOUT_SECONDS", "600"))
    if timeout < 60 or timeout > 1200:
        raise ValueError("Timeout must be between 60 and 1200 seconds")
    return {
        "number": int(number),
        "head": head,
        "manifest": manifest,
        "deployment": deployment,
        "image": image,
        "branch": branch,
        "source": source,
        "timeout": timeout,
    }


def verify_pull(pull, settings, expected_head):
    if (
        pull.get("state") != "open"
        or pull.get("draft")
        or pull.get("user", {}).get("type") != "Bot"
        or pull.get("base", {}).get("ref") != "main"
        or pull.get("base", {}).get("repo", {}).get("full_name") != REPOSITORY
        or pull.get("head", {}).get("ref") != settings["branch"]
        or pull.get("head", {}).get("repo", {}).get("full_name") != REPOSITORY
        or pull.get("head", {}).get("sha") != expected_head
        or not SHA.fullmatch(pull.get("base", {}).get("sha", ""))
    ):
        raise ValueError(
            "GitOps pull request changed or does not match the expected bot PR"
        )


def image_change(original, deployment, image):
    sections = original.split("\n---\n")
    matches = 0
    for index, section in enumerate(sections):
        if not re.search(r"(?m)^kind: Deployment\r?$", section):
            continue
        if not re.search(rf"(?m)^  name: {re.escape(deployment)}\r?$", section):
            continue
        pattern = r"(?m)^([ \t]*image:[ \t]*)[^\r\n]+"
        if len(re.findall(pattern, section)) != 1:
            raise ValueError("Expected one image in the target Deployment")
        sections[index] = re.sub(
            pattern, lambda match: match[1] + image, section, count=1
        )
        matches += 1
    if matches != 1:
        raise ValueError("Expected one target Deployment")
    return "\n---\n".join(sections)


def contents(api, path, revision):
    escaped = urllib.parse.quote(path, safe="/")
    payload = api.request(f"{REPO_PATH}/contents/{escaped}?ref={revision}")
    if payload.get("encoding") != "base64":
        raise ValueError("Could not read the GitOps manifest")
    return base64.b64decode(payload["content"]).decode("utf-8")


def verify_change(api, settings, pull, expected_head):
    files = api.request(f"{REPO_PATH}/pulls/{settings['number']}/files?per_page=100")
    if (
        len(files) != 1
        or files[0].get("filename") != settings["manifest"]
        or files[0].get("status") != "modified"
        or files[0].get("additions") != 1
        or files[0].get("deletions") != 1
    ):
        raise ValueError("GitOps PR must change exactly one image line")
    original = contents(api, settings["manifest"], pull["base"]["sha"])
    updated = contents(api, settings["manifest"], expected_head)
    if image_change(original, settings["deployment"], settings["image"]) != updated:
        raise ValueError("GitOps PR changes more than the intended Deployment image")


def source_current(api, source):
    repository, branch, sha = source
    if not any(source):
        return True
    result = api.request(f"/repos/{repository}/commits/{branch}")
    return result.get("sha") == sha


def checked(api, head):
    result = api.request(
        f"{REPO_PATH}/commits/{head}/check-runs?filter=latest&per_page=100"
    )
    if result.get("total_count", 0) > 100:
        raise ValueError("Too many checks to verify safely")
    selected = {}
    for check in result.get("check_runs", []):
        name = check.get("name")
        if (
            name not in REQUIRED_CHECKS
            or check.get("app", {}).get("slug") != "github-actions"
        ):
            continue
        if check.get("head_sha") != head:
            continue
        if name not in selected or check.get("id", 0) > selected[name].get("id", 0):
            selected[name] = check
    if selected.keys() != REQUIRED_CHECKS:
        return False
    for check in selected.values():
        if check.get("status") != "completed":
            return False
        if check.get("conclusion") != "success":
            raise ValueError(f"GitOps check {check['name']} did not pass")
    run_ids = set()
    for check in selected.values():
        match = re.fullmatch(
            rf"https://github\.com/{re.escape(REPOSITORY)}/actions/runs/(\d+)/job/\d+",
            check.get("details_url", ""),
        )
        if not match:
            raise ValueError("GitOps check does not link to an expected workflow run")
        run_ids.add(match[1])
    if len(run_ids) != 1:
        return False
    run = api.request(f"{REPO_PATH}/actions/runs/{run_ids.pop()}")
    if (
        run.get("path") != ".github/workflows/validate.yml"
        or run.get("event") != "pull_request"
        or run.get("head_sha") != head
        or run.get("head_repository", {}).get("full_name") != REPOSITORY
    ):
        raise ValueError("Checks do not belong to the expected GitOps PR workflow")
    if run.get("status") != "completed":
        return False
    if run.get("conclusion") != "success":
        raise ValueError("GitOps validation workflow did not pass")
    return True


def includes_base(api, base, head):
    result = api.request(f"{REPO_PATH}/compare/{base}...{head}")
    return result.get("behind_by") == 0


def main_head(api):
    result = api.request(f"{REPO_PATH}/git/ref/heads/main")
    head = result.get("object", {}).get("sha", "")
    if not SHA.fullmatch(head):
        raise ValueError("Could not read the GitOps main branch head")
    return head


def updated_head(api, settings, old_head, deadline):
    api.request(
        f"{REPO_PATH}/pulls/{settings['number']}/update-branch",
        method="PUT",
        body={"expected_head_sha": old_head},
    )
    while time.monotonic() < deadline:
        pull = api.pull(settings["number"])
        new_head = pull.get("head", {}).get("sha", "")
        if new_head != old_head:
            if not SHA.fullmatch(new_head):
                raise ValueError("Invalid updated PR head")
            verify_pull(pull, settings, new_head)
            commit = api.request(f"{REPO_PATH}/git/commits/{new_head}")
            parents = [parent.get("sha") for parent in commit.get("parents", [])]
            if (
                len(parents) != 2
                or parents[0] != old_head
                or not SHA.fullmatch(parents[1] or "")
                or not includes_base(api, parents[1], main_head(api))
            ):
                raise ValueError("PR head changed outside the expected base update")
            print("Updated the PR branch; waiting for checks on its new head.")
            return new_head
        time.sleep(5)
    raise TimeoutError("Timed out updating the GitOps PR branch")


def merge(api, settings):
    deadline = time.monotonic() + settings["timeout"]
    head = settings["head"]
    known_base = ""
    merge_failures = 0
    failure_target = None
    if not source_current(api, settings["source"]):
        print("Source branch moved; skip the superseded GitOps update.")
        return
    while time.monotonic() < deadline:
        pull = api.pull(settings["number"])
        verify_pull(pull, settings, head)
        base = main_head(api)
        if pull["base"]["sha"] != base:
            print("Waiting for the GitOps PR to reflect the latest main branch.")
            time.sleep(5)
            continue
        if failure_target != (head, base):
            failure_target = (head, base)
            merge_failures = 0
        if base != known_base:
            if not includes_base(api, base, head):
                head = updated_head(api, settings, head, deadline)
                known_base = ""
                continue
            known_base = base
        if not checked(api, head):
            print("Waiting for GitOps validation and secret scanning.")
            time.sleep(15)
            continue
        pull = api.pull(settings["number"])
        verify_pull(pull, settings, head)
        if pull["base"]["sha"] != base or main_head(api) != base:
            continue
        verify_change(api, settings, pull, head)
        if not checked(api, head):
            continue
        pull = api.pull(settings["number"])
        verify_pull(pull, settings, head)
        if pull["base"]["sha"] != base or main_head(api) != base:
            continue
        if not source_current(api, settings["source"]):
            print("Source branch moved; skip the superseded GitOps update.")
            return
        try:
            result = api.request(
                f"{REPO_PATH}/pulls/{settings['number']}/merge",
                method="PUT",
                body={"sha": head, "merge_method": "squash"},
            )
        except urllib.error.HTTPError as error:
            status = error.code
            detail = http_error_detail(error)
            if status not in (405, 409, 422):
                raise ValueError(detail) from error
            latest = api.pull(settings["number"])
            verify_pull(latest, settings, head)
            current_base = main_head(api)
            if current_base != base or latest["base"]["sha"] != current_base:
                print(
                    "GitOps main changed during merge; refresh the PR and check again."
                )
                known_base = ""
                continue
            if latest.get("mergeable_state") == "dirty":
                raise ValueError(
                    f"{detail}; the GitOps PR has merge conflicts"
                ) from error
            merge_failures += 1
            if merge_failures >= MERGE_RETRY_LIMIT:
                raise ValueError(f"{detail}; merge retries exhausted") from error
            print(f"{detail}; waiting before rechecking GitOps mergeability.")
            known_base = ""
            time.sleep(5)
            continue
        if not result.get("merged"):
            raise ValueError("GitHub did not merge the GitOps PR")
        print(f"Merged GitOps PR #{settings['number']} after validation.")
        return
    raise TimeoutError("Timed out waiting for GitOps checks")


def main():
    try:
        merge(API(os.environ["GITOPS_TOKEN"]), settings(os.environ))
    except urllib.error.HTTPError as error:
        print(f"::error::{http_error_detail(error)}", file=sys.stderr)
        raise SystemExit(1) from error
    except (KeyError, ValueError, TimeoutError, urllib.error.URLError) as error:
        print(f"::error::{error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
