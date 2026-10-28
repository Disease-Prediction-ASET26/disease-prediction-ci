"""Use a private local copy of a versioned Dependency-Check database."""

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

VERSION = "13.0.0"
BASE = "owasp/dependency-check:13.0.0@sha256:0e8f69ad5b00d62000937f4e57189812343f6903d9c83ee712012d51542a020a"
SEED = "owasp/dependency-check-action@sha256:d32b3ebdbcf8dc7748f2da9c705f304cb1cff64462ad3139921e444f29a2767c"
CHANNEL = "ghcr.io/disease-prediction-aset26/owasp-dependency-check:dc-13.0.0"
MAX_AGE = dt.timedelta(hours=48)
FIXTURE_URL = "https://repo.maven.apache.org/maven2/org/apache/logging/log4j/log4j-core/2.14.1/log4j-core-2.14.1.jar"
FIXTURE_SHA512 = "6e41ff42f12deedb8da06cbed73d0a9a5389660b7ee058436f8fcb6b14a6ab3105faf8e3f2c007d38ccc85af1e704505b84be5a80d8e68a434aae82b54b85f70"


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def output(name, value):
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
            handle.write(f"{name}={value}\n")


def summary(text):
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as handle:
            handle.write(text + "\n")


def validate_snapshot(path, *, fresh=True, now=None):
    metadata = json.loads((path / "metadata.json").read_text())
    if metadata.get("dependency_check_version") != VERSION:
        raise ValueError("Snapshot uses an incompatible Dependency-Check version")
    refreshed = dt.datetime.fromisoformat(metadata["refreshed_at"])
    if refreshed.tzinfo is None:
        raise ValueError("Snapshot timestamp must include a timezone")
    age = (now or dt.datetime.now(dt.UTC)) - refreshed
    if age < -dt.timedelta(minutes=5) or (fresh and age > MAX_AGE):
        raise ValueError("Snapshot timestamp is invalid or older than 48 hours")
    database = path / "data" / "odc.mv.db"
    if not database.is_file() or database.stat().st_size == 0:
        raise ValueError("Snapshot has no populated H2 database")
    return metadata


def extract_snapshot(image, destination):
    container = run(
        "docker", "create", "--entrypoint", "/bin/true", image, capture_output=True
    ).stdout.strip()
    try:
        run("docker", "cp", f"{container}:/opt/odc-snapshot/.", str(destination))
    finally:
        run("docker", "rm", "-v", container, stdout=subprocess.DEVNULL)


def extract_seed(destination):
    run("docker", "pull", SEED)
    version = run(
        "docker", "run", "--rm", SEED, "--version", capture_output=True
    ).stdout
    if version.strip() != f"dependency-check-docker version {VERSION}":
        raise ValueError("Seed uses an incompatible Dependency-Check version")
    container = run(
        "docker", "create", "--entrypoint", "/bin/true", SEED, capture_output=True
    ).stdout.strip()
    try:
        data = destination / "data"
        data.mkdir()
        run(
            "docker", "cp", f"{container}:/usr/share/dependency-check/data/.", str(data)
        )
    finally:
        run("docker", "rm", "-v", container, stdout=subprocess.DEVNULL)
    database = data / "odc.mv.db"
    if not database.is_file() or database.stat().st_size == 0:
        raise ValueError("Seed has no populated H2 database")


def resolve_image(channel):
    if channel != CHANNEL:
        raise ValueError("Only the maintained scanner channel is supported")
    run("docker", "pull", channel)
    digests = json.loads(
        run(
            "docker",
            "image",
            "inspect",
            channel,
            "--format",
            "{{json .RepoDigests}}",
            capture_output=True,
        ).stdout
    )
    repository = channel.rsplit(":", 1)[0]
    matches = [
        digest for digest in digests if digest.startswith(repository + "@sha256:")
    ]
    if len(matches) != 1:
        raise ValueError("Cannot resolve a unique scanner digest")
    return matches[0]


def scan(image, data, source, reports, *, python=False, check=True):
    reports.mkdir(parents=True, exist_ok=True)
    args = [
        "docker",
        "run",
        "--rm",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--volume",
        f"{data}:/data",
        "--volume",
        f"{source}:/input:ro",
        "--volume",
        f"{reports}:/report",
        image,
        "--data",
        "/data",
        "--scan",
        "/input",
        "--out",
        "/report",
        "--format",
        "JSON",
        "--format",
        "HTML",
        "--noupdate",
        "--disableOssIndex",
        "--failOnCVSS",
        "7",
    ]
    if python:
        args.append("--enableExperimental")
    return subprocess.run(args, check=check, text=True)


def vulnerable_fixture(path):
    """Download a pinned scanner fixture without executing the JAR."""
    path.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(FIXTURE_URL, timeout=60) as response:
        data = response.read(3 * 1024 * 1024)
    if hashlib.sha512(data).hexdigest() != FIXTURE_SHA512:
        raise ValueError("Log4j fixture checksum does not match")
    (path / "log4j-core-2.14.1.jar").write_bytes(data)


def smoke(image, snapshot, scratch, *, reports=None):
    source = scratch / "fixture"
    vulnerable_fixture(source)
    data = scratch / "smoke-data"
    shutil.copytree(snapshot / "data", data)
    reports = reports or scratch / "smoke-reports"
    result = scan(image, data, source, reports, check=False)
    report = json.loads((reports / "dependency-check-report.json").read_text())
    findings = [
        v
        for dependency in report.get("dependencies", [])
        for v in dependency.get("vulnerabilities", [])
    ]
    scores = [(v.get("cvssv3") or {}).get("baseScore") or 0 for v in findings]
    if result.returncode != 15 or not any(float(score) >= 7 for score in scores):
        raise ValueError(
            "Scanner must reject the Log4j fixture with exit code 15 and a CVSS >=7 finding"
        )
    if report.get("scanInfo", {}).get("analysisExceptions"):
        raise ValueError("Scanner smoke test reported analysis errors")


def update(destination, *, bootstrap=False):
    key = os.environ.get("NVD_API_KEY", "")
    if not key or any(character in key for character in "\r\n"):
        raise ValueError("Configure a valid NVD_API_KEY secret in the CI repository")
    with tempfile.TemporaryDirectory() as directory:
        scratch = Path(directory)
        snapshot = scratch / "snapshot"
        snapshot.mkdir()
        if bootstrap:
            extract_seed(snapshot)
        elif destination.exists():
            validate_snapshot(destination, fresh=False)
            shutil.copytree(destination, snapshot, dirs_exist_ok=True)
        else:
            extract_snapshot(resolve_image(CHANNEL), snapshot)
            validate_snapshot(snapshot, fresh=False)
        secret = scratch / "nvd.properties"
        secret.write_text("nvd.api.key=" + key + "\n")
        secret.chmod(0o600)
        run(
            "docker",
            "run",
            "--rm",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--volume",
            f"{snapshot / 'data'}:/data",
            "--volume",
            f"{secret}:/run/nvd.properties:ro",
            BASE,
            "--data",
            "/data",
            "--propertyfile",
            "/run/nvd.properties",
            "--updateonly",
            "--nvdValidForHours",
            "0",
            "--disableOssIndex",
        )
        secret.unlink()
        (snapshot / "metadata.json").write_text(
            json.dumps(
                {
                    "dependency_check_version": VERSION,
                    "refreshed_at": dt.datetime.now(dt.UTC).isoformat(),
                },
                indent=2,
            )
            + "\n"
        )
        validate_snapshot(snapshot)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(snapshot, destination)
    summary(
        "OWASP database refreshed. Publication still requires the packaged smoke test."
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["update", "scan", "smoke"])
    args = parser.parse_args()
    if args.command == "update":
        update(
            Path("scanner/snapshot"),
            bootstrap=os.environ.get("SCANNER_BOOTSTRAP") == "true",
        )
        return
    with tempfile.TemporaryDirectory() as directory:
        scratch = Path(directory)
        snapshot = scratch / "snapshot"
        snapshot.mkdir()
        image = (
            "odc-candidate:checked"
            if args.command == "smoke"
            else resolve_image(CHANNEL)
        )
        extract_snapshot(image, snapshot)
        metadata = validate_snapshot(snapshot)
        summary(
            f"OWASP scanner: `{image}`. Snapshot refreshed: `{metadata['refreshed_at']}`."
        )
        output("scanner-image", image)
        if args.command == "smoke":
            smoke(
                image, snapshot, scratch, reports=Path("reports/owasp-smoke").resolve()
            )
        else:
            source = Path(os.environ["SCAN_PATH"]).resolve(strict=True)
            scan(
                image,
                snapshot / "data",
                source,
                Path("reports/owasp").resolve(),
                python=os.environ["ECOSYSTEM"] == "python",
            )


if __name__ == "__main__":
    main()
