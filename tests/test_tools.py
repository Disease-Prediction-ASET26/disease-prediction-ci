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
        self.current = version(11, f"pr-1-{SHA}-11-1", "pr-1")
        self.other_pr = version(12, f"pr-2-{SHA}-12-1", "pr-2")
        self.production = version(13, "main", "sha-" + SHA)

    def test_keep_current_preview_and_other_prs(self):
        items = [self.old, self.current, self.other_pr, self.production]
        self.assertEqual(registry.deletions(items, pull(), REPOSITORY), [10])

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
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_changed_local_image_cannot_publish(self):
        with (
            patch.object(registry, "docker", return_value="sha256:changed") as docker,
            self.assertRaises(ValueError),
        ):
            registry.publish(self.api, self.event)
        self.assertEqual(docker.call_count, 1)

    def test_stale_pr_cannot_publish(self):
        self.api.pulls[1] = pull(sha=OTHER_SHA)
        with patch.object(registry, "docker", return_value="sha256:config") as docker:
            registry.publish(self.api, self.event)
        self.assertEqual(docker.call_count, 1)

    def test_registry_manifest_must_match_scanned_config(self):
        registry.verify_remote({"config": {"digest": "sha256:config"}}, "sha256:config")
        for manifest in [{}, {"config": {"digest": "sha256:other"}}, {"manifests": []}]:
            with self.assertRaises(ValueError):
                registry.verify_remote(manifest, "sha256:config")

    def test_failed_push_never_cleans_previous_preview(self):
        with (
            patch.object(
                registry,
                "docker",
                side_effect=[
                    "sha256:config",
                    "",
                    subprocess.CalledProcessError(1, "push"),
                ],
            ),
            patch.object(registry, "cleanup") as cleanup,
            self.assertRaises(subprocess.CalledProcessError),
        ):
            registry.publish(self.api, self.event)
        cleanup.assert_not_called()

    def test_alias_moves_only_after_remote_identity_passes(self):
        commands = []
        repository = "ghcr.io/disease-prediction-aset26/disease-prediction-ai-pr"

        def docker(*args):
            commands.append(args)
            if args[:2] == ("image", "inspect"):
                return (
                    json.dumps([repository + "@sha256:manifest"])
                    if "RepoDigests" in args[-1]
                    else "sha256:config"
                )
            if args[:2] == ("manifest", "inspect"):
                return json.dumps({"config": {"digest": "sha256:config"}})
            return ""

        with (
            patch.object(registry, "docker", side_effect=docker),
            patch.object(registry, "cleanup") as cleanup,
        ):
            registry.publish(self.api, self.event)
        manifest_index = next(
            i
            for i, command in enumerate(commands)
            if command[:2] == ("manifest", "inspect")
        )
        alias_index = commands.index(("push", repository + ":pr-1"))
        self.assertLess(manifest_index, alias_index)
        cleanup.assert_called_once_with(self.api, only=1)


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
