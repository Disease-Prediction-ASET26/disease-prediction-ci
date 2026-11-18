"""Resolve stable application versions into immutable, OCI-compatible tags."""

import argparse
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

CORE = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
PRERELEASE_IDENTIFIER = r"(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
SEMVER = re.compile(
    rf"^{CORE}(?:-{PRERELEASE_IDENTIFIER}(?:\.{PRERELEASE_IDENTIFIER})*)?$"
)
STABLE_VERSION = re.compile(rf"^{CORE}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
NUMBER = re.compile(r"^(?:0|[1-9][0-9]*)$")


def validate_version(value, *, stable=False):
    if not isinstance(value, str) or not SEMVER.fullmatch(value):
        raise ValueError("Version must use strict SemVer without build metadata")
    if stable and not STABLE_VERSION.fullmatch(value):
        raise ValueError("Version source must be a stable X.Y.Z version")
    return value


def read_version(path):
    source = Path(path)
    if source.name == "package.json":
        value = json.loads(source.read_text(encoding="utf-8")).get("version")
    elif source.name == "pom.xml":
        root = ET.parse(source).getroot()
        value = root.findtext("{*}version")
        if value is None:
            value = root.findtext("{*}parent/{*}version")
        if value and value.endswith("-SNAPSHOT"):
            value = value.removesuffix("-SNAPSHOT")
    else:
        value = source.read_text(encoding="utf-8").strip()
    return validate_version(value, stable=True)


def image_tag(version, event, *, ref, sha, run_id, run_attempt):
    validate_version(version, stable=True)
    if not SHA.fullmatch(sha):
        raise ValueError("GitHub commit SHA must contain 40 lowercase hex digits")
    if not NUMBER.fullmatch(run_id) or run_id == "0":
        raise ValueError("GitHub run ID must be a positive integer")
    if not NUMBER.fullmatch(run_attempt) or run_attempt == "0":
        raise ValueError("GitHub run attempt must be a positive integer")

    pull = event.get("pull_request")
    if pull:
        number = str(event.get("number", ""))
        if not NUMBER.fullmatch(number) or number == "0":
            raise ValueError("Pull request number must be a positive integer")
        head_sha = pull.get("head", {}).get("sha", "")
        if not SHA.fullmatch(head_sha):
            raise ValueError(
                "Pull request head SHA must contain 40 lowercase hex digits"
            )
        tag = f"{version}-pr.{number}.{head_sha[:7]}.{run_id}.{run_attempt}"
    elif ref == "refs/heads/develop":
        tag = f"{version}-dev.{sha[:7]}.{run_id}.{run_attempt}"
    elif ref == "refs/heads/main":
        tag = version
    else:
        raise ValueError("Image publication requires develop, main, or an internal PR")

    temporary_tags = (
        tag + "-amd64",
        tag + "-arm64",
        f"{tag}-index.{run_id}.{run_attempt}",
    )
    for candidate in (tag, *temporary_tags):
        if len(candidate) > 128:
            raise ValueError("Resolved OCI image tag exceeds 128 characters")
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}", candidate):
            raise ValueError("Resolved image tag is not OCI-compatible")
    return tag


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("version_file")
    parser.add_argument("--event-file", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    arguments = parser.parse_args()

    version = read_version(arguments.version_file)
    event = json.loads(Path(arguments.event_file).read_text(encoding="utf-8"))
    tag = image_tag(
        version,
        event,
        ref=arguments.ref,
        sha=arguments.sha,
        run_id=arguments.run_id,
        run_attempt=arguments.run_attempt,
    )
    print(f"version={version}")
    print(f"tag={tag}")


if __name__ == "__main__":
    main()
