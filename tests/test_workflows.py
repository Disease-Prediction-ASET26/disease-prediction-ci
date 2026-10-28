import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


class WorkflowTests(unittest.TestCase):
    def test_pull_request_jobs_reject_forks(self):
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            for name, job in yaml.safe_load(path.read_text())["jobs"].items():
                if path.name == "owasp-refresh.yml":
                    continue
                condition = job.get("if", "")
                with self.subTest(workflow=path.name, job=name):
                    self.assertIn("github.event_name != 'pull_request'", condition)
                    self.assertIn(
                        "github.event.pull_request.head.repo.full_name == github.repository",
                        condition,
                    )

    def test_publish_uses_loaded_image_after_a_blocking_scan(self):
        steps = workflow("container.yml")["jobs"]["containerize"]["steps"]
        build = next(step for step in steps if step.get("name") == "containerize")
        scan = next(step for step in steps if step.get("name") == "scan")
        publish = next(
            step for step in steps if step.get("with", {}).get("command") == "publish"
        )
        self.assertTrue(build["with"]["load"])
        self.assertFalse(build["with"]["push"])
        self.assertEqual(scan["with"]["exit-code"], "1")
        self.assertEqual(scan["with"]["severity"], "HIGH,CRITICAL")
        self.assertFalse(scan["with"]["ignore-unfixed"])
        self.assertNotIn("continue-on-error", scan)
        self.assertLess(steps.index(scan), steps.index(publish))

    def test_publication_and_cleanup_share_registry_lock(self):
        publisher = workflow("container.yml")["jobs"]["containerize"]["concurrency"]
        cleanup = workflow("preview-cleanup.yml")["jobs"]["cleanup"]["concurrency"]
        self.assertEqual(publisher, cleanup)
        self.assertFalse(publisher["cancel-in-progress"])

    def test_refresh_is_serialized_and_publishes_after_packaged_smoke(self):
        document = workflow("owasp-refresh.yml")
        self.assertFalse(document["concurrency"]["cancel-in-progress"])
        steps = document["jobs"]["refresh"]["steps"]
        smoke = next(
            i
            for i, step in enumerate(steps)
            if step.get("with", {}).get("command") == "smoke"
        )
        publish = next(
            i
            for i, step in enumerate(steps)
            if step.get("name") == "Publish successful snapshot"
        )
        self.assertLess(smoke, publish)

    def test_scanner_base_and_schema_match(self):
        dockerfile = (ROOT / "scanner/Dockerfile").read_text()
        self.assertRegex(
            dockerfile.splitlines()[0],
            r"^FROM owasp/dependency-check:13\.0\.0@sha256:[0-9a-f]{64}$",
        )

    def test_actions_are_immutable_and_checkout_does_not_persist_credentials(self):
        for path in [
            *(ROOT / ".github/workflows").glob("*.yml"),
            *(ROOT / "actions").glob("*/action.yml"),
        ]:
            document = yaml.safe_load(path.read_text())
            jobs = document.get("jobs", {}).values()
            steps = [step for job in jobs for step in job.get("steps", [])]
            steps += document.get("runs", {}).get("steps", [])
            for step in steps:
                uses = step.get("uses", "")
                if not uses or uses.startswith("./"):
                    continue
                with self.subTest(path=path, uses=uses):
                    self.assertRegex(uses, re.compile(r"@[0-9a-f]{40}$"))
                    if uses.startswith("actions/checkout@"):
                        self.assertFalse(step["with"]["persist-credentials"])


if __name__ == "__main__":
    unittest.main()
