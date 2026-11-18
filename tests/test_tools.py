import copy
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scanner = load("scanner", "actions/scanner/scanner.py")
registry = load("registry", "actions/registry/registry.py")
image_version = load("image_version", "actions/registry/version.py")
summary = load("summary", "actions/ci-summary/summary.py")
SHA = "a" * 40
OTHER_SHA = "b" * 40
REPOSITORY = "Disease-Prediction-ASET26/disease-prediction-ai"


def pull(number=1, *, state="open", sha=SHA, repository=REPOSITORY):
    return {
        "number": number,
        "state": state,
        "head": {"sha": sha, "repo": {"full_name": repository}},
    }


def version(identifier, *tags):
    return {"id": identifier, "metadata": {"container": {"tags": list(tags)}}}


class FakeGitHub:
    repository = REPOSITORY
    owner, repo = REPOSITORY.split("/")

    def __init__(self, versions=None, pulls=None):
        self.items = versions or []
        self.pulls = pulls or {1: pull()}
        self.deleted = []

    def versions(self):
        return self.items

    def pull(self, number):
        return copy.deepcopy(self.pulls[number])

    def delete(self, identifier):
        self.deleted.append(identifier)


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.old = version(10, f"pr-1-{OTHER_SHA}-10-1")
        self.current_tag = f"0.1.2-pr.1.{SHA[:7]}.11.1"
        self.current = version(11, self.current_tag, "pr-1")
        self.other_pr = version(12, f"0.1.2-pr.2.{SHA[:7]}.12.1", "pr-2")
        self.production = version(13, "main", "sha-" + SHA)

    def test_keep_current_preview_and_other_prs(self):
        items = [self.old, self.current, self.other_pr, self.production]
        self.assertEqual(registry.deletions(items, pull(), REPOSITORY), [10])

    def test_keep_the_current_preview_platforms(self):
        old_arch = version(15, f"pr-1-{OTHER_SHA}-10-1-amd64")
        amd64 = version(16, self.current_tag + "-amd64")
        arm64 = version(17, self.current_tag + "-arm64")
        candidate = version(18, self.current_tag + "-index.11.1")
        items = [self.old, self.current, old_arch, amd64, arm64, candidate]
        self.assertEqual(registry.deletions(items, pull(), REPOSITORY), [10, 15])
        self.assertEqual(
            registry.deletions(items, pull(state="closed"), REPOSITORY),
            [10, 11, 15, 16, 17, 18],
        )

    def test_closed_pr_removes_only_its_versions(self):
        items = [self.old, self.current, self.other_pr, self.production]
        self.assertEqual(
            registry.deletions(items, pull(state="closed"), REPOSITORY), [10, 11]
        )

    def test_failed_or_superseded_replacement_keeps_previous(self):
        self.assertEqual(
            registry.deletions(
                [self.old, self.current], pull(sha=OTHER_SHA), REPOSITORY
            ),
            [],
        )

    def test_missing_or_ambiguous_alias_keeps_images(self):
        self.assertEqual(registry.deletions([self.old], pull(), REPOSITORY), [])
        duplicate = version(14, f"pr-1-{SHA}-14-1", "pr-1")
        self.assertEqual(
            registry.deletions([self.current, duplicate], pull(), REPOSITORY), []
        )

    def test_unknown_mixed_and_untagged_versions_are_preserved(self):
        for item in [
            version(1),
            version(2, "latest"),
            version(3, "pr-1", "main"),
            version(4, "pr-1", f"pr-2-{SHA}-1-1"),
            version(5, self.current_tag, "main"),
        ]:
            self.assertIsNone(registry.preview_number(item))

    def test_forks_cannot_delete_previews(self):
        self.assertEqual(
            registry.deletions(
                [self.current],
                pull(state="closed", repository="outside/fork"),
                REPOSITORY,
            ),
            [],
        )

    def test_cleanup_rechecks_pr_state_before_deletion(self):
        api = FakeGitHub([self.old, self.current])
        with patch.object(api, "pull", side_effect=[pull(), pull(sha=OTHER_SHA)]):
            registry.cleanup(api)
        self.assertEqual(api.deleted, [])

    def test_cleanup_is_scoped_to_selected_pr(self):
        api = FakeGitHub(
            [self.old, self.current, self.other_pr],
            {1: pull(state="closed"), 2: pull(2)},
        )
        registry.cleanup(api, only=1)
        self.assertEqual(api.deleted, [10, 11])

    def test_delete_endpoint_cannot_target_production_package(self):
        api = registry.GitHub(REPOSITORY, "test-token")
        with patch.object(api, "request") as request:
            api.delete(42)
        self.assertEqual(
            request.call_args.args,
            (
                "/orgs/Disease-Prediction-ASET26/packages/container/disease-prediction-ai-pr/versions/42",
                "DELETE",
            ),
        )


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeGitHub()
        self.event = {"number": 1, "pull_request": pull()}
        self.environment = patch.dict(
            os.environ,
            {
                "SCANNED_IMAGE_ID": "sha256:config",
                "GITHUB_RUN_ID": "50",
                "GITHUB_RUN_ATTEMPT": "1",
                "GITHUB_REF": "refs/pull/1/merge",
                "GITHUB_SHA": SHA,
                "IMAGE_VERSION": "0.1.2",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_resolves_service_specific_versioned_tags(self):
        self.assertEqual(
            registry.target(self.api, self.event)[1],
            f"0.1.2-pr.1.{SHA[:7]}.50.1",
        )
        with patch.dict(
            os.environ,
            {"GITHUB_REF": "refs/heads/develop", "GITHUB_SHA": SHA},
        ):
            self.assertEqual(
                registry.target(self.api, {})[1],
                f"0.1.2-dev.{SHA[:7]}.50.1",
            )
        with patch.dict(
            os.environ,
            {"GITHUB_REF": "refs/heads/main", "GITHUB_SHA": SHA},
        ):
            self.assertEqual(registry.target(self.api, {})[1], "0.1.2")

    def test_existing_version_tag_cannot_be_retargeted(self):
        self.assertIsNone(
            registry.check_tag_digest("1.2.3", "sha256:" + "a" * 64, None)
        )
        self.assertEqual(
            registry.check_tag_digest(
                "1.2.3", "sha256:" + "a" * 64, "sha256:" + "a" * 64
            ),
            "sha256:" + "a" * 64,
        )
        with self.assertRaisesRegex(ValueError, "bump the service version"):
            registry.check_tag_digest(
                "1.2.3", "sha256:" + "a" * 64, "sha256:" + "b" * 64
            )

    def test_registry_manifest_must_match_scanned_config(self):
        registry.verify_remote({"config": {"digest": "sha256:config"}}, "sha256:config")
        registry.verify_remote(
            {"config": {"digest": "sha256:config"}},
            "sha256:manifest",
            "sha256:manifest",
        )
        for manifest in [{}, {"config": {"digest": "sha256:other"}}, {"manifests": []}]:
            with self.assertRaises(ValueError):
                registry.verify_remote(manifest, "sha256:config")

    def test_unknown_blob_push_retries_with_a_limit(self):
        with (
            patch.object(
                registry,
                "docker",
                side_effect=[
                    subprocess.CalledProcessError(1, "push", output="unknown blob"),
                    "pushed",
                ],
            ) as docker,
            patch.object(registry.time, "sleep"),
        ):
            self.assertEqual(registry.push_image("image:tag"), "pushed")
        self.assertEqual(docker.call_count, 2)


class ImageVersionTests(unittest.TestCase):
    def test_strict_semver_and_oci_tag_rules(self):
        for value in ("0.1.0", "1.2.3-rc.1", "1.2.3-dev.abcdef0.12.1"):
            self.assertEqual(image_version.validate_version(value), value)
        for value in (
            "01.2.3",
            "1.02.3",
            "1.2",
            "1.2.3+build.1",
            "1.2.3-01",
            "1.2.3-",
            "v1.2.3",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                image_version.validate_version(value)
        with self.assertRaisesRegex(ValueError, "stable X.Y.Z"):
            image_version.validate_version("1.2.3-rc.1", stable=True)

    def test_reads_package_maven_snapshot_and_plain_version_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "package.json"
            package.write_text('{"name":"app","version":"2.3.4"}')
            self.assertEqual(image_version.read_version(package), "2.3.4")
            pom = root / "pom.xml"
            pom.write_text(
                '<project xmlns="http://maven.apache.org/POM/4.0.0">'
                "<modelVersion>4.0.0</modelVersion><version>0.0.1-SNAPSHOT</version>"
                "</project>"
            )
            self.assertEqual(image_version.read_version(pom), "0.0.1")
            plain = root / "VERSION"
            plain.write_text("3.4.5\n")
            self.assertEqual(image_version.read_version(plain), "3.4.5")

    def test_generates_stable_development_and_prerelease_tags(self):
        event = {"number": 12, "pull_request": {"head": {"sha": SHA}}}
        self.assertEqual(
            image_version.image_tag(
                "1.2.3",
                {},
                ref="refs/heads/main",
                sha=SHA,
                run_id="42",
                run_attempt="2",
            ),
            "1.2.3",
        )
        self.assertEqual(
            image_version.image_tag(
                "1.2.3",
                {},
                ref="refs/heads/develop",
                sha=SHA,
                run_id="42",
                run_attempt="2",
            ),
            f"1.2.3-dev.{SHA[:7]}.42.2",
        )
        self.assertEqual(
            image_version.image_tag(
                "1.2.3",
                event,
                ref="refs/pull/12/merge",
                sha=OTHER_SHA,
                run_id="42",
                run_attempt="2",
            ),
            f"1.2.3-pr.12.{SHA[:7]}.42.2",
        )

    def test_rejects_invalid_version_sources_and_context(self):
        for source in ("1.2.3-SNAPSHOT", "1.2.3+build", "01.2.3"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                image_version.validate_version(source, stable=True)
        with self.assertRaisesRegex(ValueError, "develop, main"):
            image_version.image_tag(
                "1.2.3",
                {},
                ref="refs/heads/feature",
                sha=SHA,
                run_id="42",
                run_attempt="1",
            )


class MultiPlatformTests(unittest.TestCase):
    def setUp(self):
        self.repository = "ghcr.io/disease-prediction-aset26/disease-prediction-ai-pr"
        self.digests = {
            "amd64": self.repository + "@sha256:" + "a" * 64,
            "arm64": self.repository + "@sha256:" + "b" * 64,
        }
        self.manifest = {
            "manifests": [
                {
                    "digest": reference.rsplit("@", 1)[1],
                    "platform": {"os": "linux", "architecture": architecture},
                }
                for architecture, reference in self.digests.items()
            ]
        }

    def test_index_contains_exact_scanned_architectures(self):
        registry.verify_index(self.manifest, self.digests)
        for change in (
            {"manifests": self.manifest["manifests"][:1]},
            {"manifests": self.manifest["manifests"] * 2},
            {"manifests": self.manifest["manifests"][:1] * 2},
        ):
            with self.assertRaises(ValueError):
                registry.verify_index(change, self.digests)
        swapped = copy.deepcopy(self.manifest)
        swapped["manifests"][0]["digest"] = swapped["manifests"][1]["digest"]
        with self.assertRaises(ValueError):
            registry.verify_index(swapped, self.digests)

    def test_stage_rejects_wrong_architecture_before_push(self):
        api = FakeGitHub()
        event = {"number": 1, "pull_request": pull()}
        config_id = "sha256:" + "c" * 64
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {
                    "SCANNED_IMAGE_ID": config_id,
                    "GITHUB_RUN_ID": "50",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "GITHUB_REF": "refs/pull/1/merge",
                    "GITHUB_SHA": SHA,
                    "IMAGE_VERSION": "0.1.2",
                    "IMAGE_ARCH": "amd64",
                    "STAGE_OUTPUT": str(Path(directory) / "stage.json"),
                },
            ),
            patch.object(
                registry, "docker", side_effect=[config_id, "linux/arm64"]
            ) as docker,
            self.assertRaisesRegex(ValueError, "platform"),
        ):
            registry.stage(api, event)
        self.assertEqual(docker.call_count, 2)

    def test_index_rejects_unscanned_platform_before_publish(self):
        api = FakeGitHub()
        event = {"number": 1, "pull_request": pull()}
        with tempfile.TemporaryDirectory() as directory:
            for arch, reference in self.digests.items():
                (Path(directory) / f"stage-{arch}.json").write_text(
                    json.dumps(
                        {
                            "architecture": arch,
                            "tag": f"0.1.2-pr.1.{SHA[:7]}.50.1-{arch}",
                            "digest": reference,
                            "scanned_id": "sha256:" + "c" * 64,
                        }
                    )
                )
            with (
                patch.dict(
                    os.environ,
                    {
                        "STAGE_DIR": directory,
                        "GITHUB_RUN_ID": "50",
                        "GITHUB_RUN_ATTEMPT": "1",
                        "GITHUB_REF": "refs/pull/1/merge",
                        "GITHUB_SHA": SHA,
                        "IMAGE_VERSION": "0.1.2",
                    },
                ),
                patch.object(
                    registry,
                    "remote_digest",
                    side_effect=lambda reference: self.digests[
                        reference.rsplit("-", 1)[1]
                    ].rsplit("@", 1)[1],
                ),
                patch.object(
                    registry,
                    "remote_manifest",
                    return_value={"config": {"digest": "sha256:" + "d" * 64}},
                ),
                patch.object(registry, "docker") as docker,
                self.assertRaisesRegex(ValueError, "differs from the scanned image"),
            ):
                registry.index(api, event)
            docker.assert_not_called()

    def test_index_publishes_version_tag_and_digest_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "outputs"
            summary_path = Path(directory) / "summary"
            for architecture, reference in self.digests.items():
                (Path(directory) / f"stage-{architecture}.json").write_text(
                    json.dumps(
                        {
                            "architecture": architecture,
                            "tag": f"1.2.3-{architecture}",
                            "digest": reference,
                            "scanned_id": "sha256:" + "c" * 64,
                        }
                    )
                )

            index_digest = "sha256:" + "e" * 64
            candidate_ref = self.repository + ":1.2.3-index.50.1"
            release_ref = self.repository + ":1.2.3"
            alias_ref = self.repository + ":main"
            index_tags = {candidate_ref, release_ref, alias_ref}

            def remote(reference):
                if reference in index_tags:
                    return index_digest
                return self.digests[reference.rsplit("-", 1)[1]].rsplit("@", 1)[1]

            def manifest(reference):
                if reference.endswith(index_digest):
                    return self.manifest
                return {"config": {"digest": "sha256:" + "c" * 64}}

            tag_state = {"release_exists": False}

            def remote_with_immutable_release(reference):
                if reference == release_ref and not tag_state["release_exists"]:
                    raise subprocess.CalledProcessError(
                        1, ["docker", "inspect", reference], output="manifest unknown"
                    )
                return remote(reference)

            def docker(*args):
                if args[:3] == ("buildx", "imagetools", "create"):
                    if "--metadata-file" in args:
                        metadata = Path(args[args.index("--metadata-file") + 1])
                        metadata.write_text(
                            json.dumps(
                                {"containerimage.descriptor": {"digest": index_digest}}
                            )
                        )
                    elif "--tag" in args:
                        tag = args[args.index("--tag") + 1]
                        if tag == release_ref:
                            tag_state["release_exists"] = True
                return ""

            with (
                patch.dict(
                    os.environ,
                    {
                        "STAGE_DIR": directory,
                        "GITHUB_RUN_ID": "50",
                        "GITHUB_RUN_ATTEMPT": "1",
                        "GITHUB_REF": "refs/heads/main",
                        "GITHUB_SHA": SHA,
                        "IMAGE_VERSION": "1.2.3",
                        "GITHUB_OUTPUT": str(output),
                        "GITHUB_STEP_SUMMARY": str(summary_path),
                    },
                ),
                patch.object(
                    registry,
                    "target",
                    return_value=(self.repository, "1.2.3", "main", None, lambda: True),
                ),
                patch.object(
                    registry, "remote_digest", side_effect=remote_with_immutable_release
                ),
                patch.object(registry, "remote_manifest", side_effect=manifest),
                patch.object(registry, "docker", side_effect=docker),
            ):
                registry.index(FakeGitHub(), {})

            self.assertIn(f"digest={index_digest}", output.read_text(encoding="utf-8"))
            self.assertIn(
                f"image-reference={release_ref}@{index_digest}",
                output.read_text(encoding="utf-8"),
            )
            self.assertIn("tag=1.2.3", output.read_text(encoding="utf-8"))
            self.assertIn(release_ref, summary_path.read_text(encoding="utf-8"))


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "snapshot"
        (self.path / "data").mkdir(parents=True)
        (self.path / "data" / "odc.mv.db").write_bytes(b"database")
        self.now = dt.datetime(2026, 10, 28, tzinfo=dt.UTC)
        self.metadata = {
            "dependency_check_version": scanner.VERSION,
            "refreshed_at": self.now.isoformat(),
        }
        self.write()

    def write(self):
        (self.path / "metadata.json").write_text(json.dumps(self.metadata))

    def test_freshness_boundary(self):
        scanner.validate_snapshot(self.path, now=self.now + dt.timedelta(hours=48))
        with self.assertRaises(ValueError):
            scanner.validate_snapshot(
                self.path, now=self.now + dt.timedelta(hours=48, seconds=1)
            )
        scanner.validate_snapshot(
            self.path, fresh=False, now=self.now + dt.timedelta(days=20)
        )

    def test_incompatible_empty_future_and_naive_snapshots_fail(self):
        for change in [
            {"dependency_check_version": "old"},
            {"refreshed_at": (self.now + dt.timedelta(hours=1)).isoformat()},
            {"refreshed_at": "2026-10-28T00:00:00"},
        ]:
            with self.subTest(change=change):
                original = self.metadata.copy()
                self.metadata.update(change)
                self.write()
                with self.assertRaises(ValueError):
                    scanner.validate_snapshot(self.path, now=self.now)
                self.metadata = original
        self.write()
        (self.path / "data" / "odc.mv.db").write_bytes(b"")
        with self.assertRaises(ValueError):
            scanner.validate_snapshot(self.path, now=self.now)

    def test_scan_disables_updates_and_enables_python_analyzers(self):
        with patch.object(scanner.subprocess, "run") as run:
            scanner.scan(
                "image@sha256:digest",
                self.path / "data",
                self.path,
                self.path / "reports",
                python=True,
            )
        arguments = run.call_args.args[0]
        for argument in ["--noupdate", "--disableOssIndex", "--enableExperimental"]:
            self.assertIn(argument, arguments)
        self.assertEqual(arguments[arguments.index("--failOnCVSS") + 1], "7")

    def test_scan_omits_suppression_when_file_absent(self):
        with patch.object(scanner.subprocess, "run") as run:
            scanner.scan(
                "image@sha256:digest",
                self.path / "data",
                self.path,
                self.path / "reports",
                python=True,
            )
        arguments = run.call_args.args[0]
        self.assertNotIn("--suppression", arguments)

    def test_scan_adds_suppression_when_file_present(self):
        (self.path / "owasp-suppression.xml").write_text("<suppressions/>")
        with patch.object(scanner.subprocess, "run") as run:
            scanner.scan(
                "image@sha256:digest",
                self.path / "data",
                self.path,
                self.path / "reports",
                python=True,
            )
        arguments = run.call_args.args[0]
        self.assertIn("--suppression", arguments)
        self.assertEqual(
            arguments[arguments.index("--suppression") + 1],
            "/input/owasp-suppression.xml",
        )

    def test_cold_bootstrap_requires_explicit_choice(self):
        destination = Path(self.directory.name) / "prepared"
        with (
            patch.dict(os.environ, {"NVD_API_KEY": "test-key"}),
            patch.object(
                scanner,
                "resolve_image",
                side_effect=RuntimeError("registry unavailable"),
            ),
            self.assertRaises(RuntimeError),
        ):
            scanner.update(destination)
        self.assertFalse(destination.exists())

    def test_failed_update_does_not_publish_or_overwrite_snapshot(self):
        destination = Path(self.directory.name) / "prepared"
        with (
            patch.dict(os.environ, {"NVD_API_KEY": "test-key"}),
            patch.object(
                scanner,
                "extract_seed",
                side_effect=lambda target: shutil.copytree(
                    self.path / "data", target / "data"
                ),
            ),
            patch.object(
                scanner, "run", side_effect=subprocess.CalledProcessError(1, "update")
            ),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            scanner.update(destination, bootstrap=True)
        self.assertFalse(destination.exists())
        self.assertEqual((self.path / "data" / "odc.mv.db").read_bytes(), b"database")

    def test_smoke_rejects_scanner_errors_and_missing_findings(self):
        def fake_scan(image, data, source, reports, **kwargs):
            reports.mkdir()
            (reports / "dependency-check-report.json").write_text(
                json.dumps({"dependencies": []})
            )
            return subprocess.CompletedProcess([], 1)

        with (
            patch.object(scanner, "vulnerable_fixture"),
            patch.object(scanner, "scan", side_effect=fake_scan),
            self.assertRaises(ValueError),
        ):
            scanner.smoke("image", self.path, Path(self.directory.name))

    def test_smoke_accepts_expected_vulnerability_exit(self):
        def fake_scan(image, data, source, reports, **kwargs):
            reports.mkdir()
            (reports / "dependency-check-report.json").write_text(
                json.dumps(
                    {
                        "dependencies": [
                            {"vulnerabilities": [{"cvssv3": {"baseScore": 10}}]}
                        ]
                    }
                )
            )
            return subprocess.CompletedProcess([], 15)

        with (
            patch.object(scanner, "vulnerable_fixture"),
            patch.object(scanner, "scan", side_effect=fake_scan),
        ):
            scanner.smoke("image", self.path, Path(self.directory.name))

    def test_smoke_handles_missing_cvss_v3(self):
        def fake_scan(image, data, source, reports, **kwargs):
            reports.mkdir()
            (reports / "dependency-check-report.json").write_text(
                json.dumps(
                    {
                        "dependencies": [
                            {
                                "vulnerabilities": [
                                    {"cvssv3": None},
                                    {"cvssv3": {"baseScore": None}},
                                    {"cvssv3": {"baseScore": 10}},
                                ]
                            }
                        ]
                    }
                )
            )
            return subprocess.CompletedProcess([], 15)

        with (
            patch.object(scanner, "vulnerable_fixture"),
            patch.object(scanner, "scan", side_effect=fake_scan),
        ):
            scanner.smoke("image", self.path, Path(self.directory.name))

    def test_successful_refresh_restores_seed_without_packaging_secret(self):
        destination = Path(self.directory.name) / "prepared"

        def restore(image, target):
            shutil.copytree(self.path, target, dirs_exist_ok=True)

        def update(*arguments, **kwargs):
            mount = next(value for value in arguments if value.endswith(":/data"))
            (Path(mount.removesuffix(":/data")) / "odc.mv.db").write_bytes(b"updated")
            return subprocess.CompletedProcess(arguments, 0)

        with (
            patch.dict(os.environ, {"NVD_API_KEY": "private-test-key"}),
            patch.object(
                scanner, "resolve_image", return_value="seed@sha256:test"
            ) as resolve,
            patch.object(scanner, "extract_snapshot", side_effect=restore),
            patch.object(scanner, "run", side_effect=update),
            patch.object(scanner, "smoke"),
        ):
            scanner.update(destination)
        resolve.assert_called_once_with(scanner.CHANNEL)
        scanner.validate_snapshot(destination)
        self.assertEqual((destination / "data" / "odc.mv.db").read_bytes(), b"updated")
        self.assertEqual((self.path / "data" / "odc.mv.db").read_bytes(), b"database")
        for path in destination.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"private-test-key", path.read_bytes())

    def test_bootstrap_prepares_cache_before_downstream_validation(self):
        destination = Path(self.directory.name) / "prepared"

        def update(*arguments, **kwargs):
            mount = next(value for value in arguments if value.endswith(":/data"))
            (Path(mount.removesuffix(":/data")) / "odc.mv.db").write_bytes(b"updated")

        with (
            patch.dict(os.environ, {"NVD_API_KEY": "private-test-key"}),
            patch.object(
                scanner,
                "extract_seed",
                side_effect=lambda target: shutil.copytree(
                    self.path / "data", target / "data"
                ),
            ),
            patch.object(scanner, "run", side_effect=update),
            patch.object(scanner, "smoke") as smoke,
        ):
            scanner.update(destination, bootstrap=True)
        smoke.assert_not_called()
        scanner.validate_snapshot(destination)

    def test_seed_rejects_incompatible_scanner_version(self):
        target = Path(self.directory.name) / "seed"
        target.mkdir()
        with (
            patch.object(
                scanner,
                "run",
                side_effect=[
                    None,
                    subprocess.CompletedProcess(
                        [], 0, "dependency-check-docker version 12.2.2"
                    ),
                ],
            ),
            self.assertRaisesRegex(ValueError, "incompatible"),
        ):
            scanner.extract_seed(target)
        self.assertFalse((target / "data").exists())

    def test_cached_snapshot_avoids_registry_restore(self):
        destination = Path(self.directory.name) / "prepared"
        shutil.copytree(self.path, destination)
        with (
            patch.dict(os.environ, {"NVD_API_KEY": "private-test-key"}),
            patch.object(scanner, "resolve_image") as resolve,
            patch.object(scanner, "run"),
        ):
            scanner.update(destination)
        resolve.assert_not_called()
        scanner.validate_snapshot(destination)

    def test_failed_refresh_preserves_cached_database(self):
        destination = Path(self.directory.name) / "prepared"
        shutil.copytree(self.path, destination)
        with (
            patch.dict(os.environ, {"NVD_API_KEY": "private-test-key"}),
            patch.object(
                scanner, "run", side_effect=subprocess.CalledProcessError(1, "update")
            ),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            scanner.update(destination)
        self.assertEqual((destination / "data" / "odc.mv.db").read_bytes(), b"database")

    def test_fixture_rejects_changed_download(self):
        destination = Path(self.directory.name) / "fixture"
        with (
            patch.object(
                scanner.urllib.request,
                "urlopen",
                return_value=io.BytesIO(b"wrong artifact"),
            ),
            self.assertRaises(ValueError),
        ):
            scanner.vulnerable_fixture(destination)
        self.assertFalse((destination / "log4j-core-2.14.1.jar").exists())

    def test_fixture_accepts_matching_checksum(self):
        destination = Path(self.directory.name) / "fixture"
        data = b"fixture bytes"
        with (
            patch.object(
                scanner.urllib.request, "urlopen", return_value=io.BytesIO(data)
            ),
            patch.object(scanner, "FIXTURE_SHA512", hashlib.sha512(data).hexdigest()),
        ):
            scanner.vulnerable_fixture(destination)
        self.assertEqual((destination / "log4j-core-2.14.1.jar").read_bytes(), data)


class GateTests(unittest.TestCase):
    def test_disabled_sonar_does_not_block_enabled_stages(self):
        failed, report = summary.evaluate(
            {"build": {"result": "success"}, "sonarqube": {"result": "skipped"}},
            {"sonarqube": "Configure SonarQube"},
        )
        self.assertEqual(failed, [])
        self.assertIn("Not configured: Configure SonarQube", report)

    def test_failures_and_unexpected_skips_fail_the_gate(self):
        for result in ["failure", "cancelled", "skipped"]:
            failed, _ = summary.evaluate({"build": {"result": result}}, {})
            self.assertEqual(failed, ["build"])

    def test_disabled_job_failure_is_not_ignored(self):
        failed, _ = summary.evaluate(
            {"unit-test": {"result": "failure"}}, {"unit-test": "Add tests"}
        )
        self.assertEqual(failed, ["unit-test"])

    def test_enabled_stage_cannot_hide_behind_old_prerequisite_message(self):
        failed, _ = summary.evaluate(
            {"sonarqube": {"result": "skipped"}},
            {"sonarqube": "Configure SonarQube"},
            ["sonarqube"],
        )
        self.assertEqual(failed, ["sonarqube"])


if __name__ == "__main__":
    unittest.main()
