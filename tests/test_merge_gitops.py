import copy
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "merge_gitops", ROOT / "actions/merge-gitops/merge.py"
)
merge_gitops = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(merge_gitops)

BASE = "a" * 40
HEAD = "b" * 40
SOURCE = "c" * 40
IMAGE = "ghcr.io/disease-prediction-aset26/disease-prediction-be-dev@sha256:" + "d" * 64
PATH = "clusters/oracle/disease-prediction/deployments.yaml"
ORIGINAL = "kind: Deployment\nmetadata:\n  name: backend\nspec:\n  image: old\n"
UPDATED = ORIGINAL.replace("image: old", "image: " + IMAGE)


def config():
    return merge_gitops.settings(
        {
            "PR_NUMBER": "123",
            "EXPECTED_HEAD": HEAD,
            "MANIFEST_PATH": PATH,
            "DEPLOYMENT": "backend",
            "IMAGE_REF": IMAGE,
            "SOURCE_REPOSITORY": "Disease-Prediction-ASET26/disease-prediction-be",
            "SOURCE_BRANCH": "develop",
            "SOURCE_SHA": SOURCE,
        }
    )


def pull():
    return {
        "state": "open",
        "draft": False,
        "user": {"type": "Bot"},
        "base": {
            "ref": "main",
            "sha": BASE,
            "repo": {"full_name": merge_gitops.REPOSITORY},
        },
        "head": {
            "ref": "automation/update-backend-dev",
            "sha": HEAD,
            "repo": {"full_name": merge_gitops.REPOSITORY},
        },
    }


def check(
    name, identifier, status="completed", conclusion="success", head=HEAD, run=12
):
    return {
        "name": name,
        "id": identifier,
        "app": {"slug": "github-actions"},
        "head_sha": head,
        "status": status,
        "conclusion": conclusion,
        "details_url": f"https://github.com/{merge_gitops.REPOSITORY}/actions/runs/{run}/job/{identifier}",
    }


def checks(*items):
    return {"total_count": len(items), "check_runs": list(items)}


def successful_checks():
    return checks(check("validate", 1), check("secret-scan / secret-scan", 2))


def workflow_run():
    return {
        "path": ".github/workflows/validate.yml",
        "event": "pull_request",
        "head_sha": HEAD,
        "head_repository": {"full_name": merge_gitops.REPOSITORY},
        "status": "completed",
        "conclusion": "success",
    }


class FakeAPI:
    def __init__(self):
        self.requests = []
        self.current_pull = pull()
        self.source = SOURCE
        self.check_runs = successful_checks()
        self.run = workflow_run()

    def pull(self, number):
        return copy.deepcopy(self.current_pull)

    def request(self, path, *, method="GET", body=None):
        self.requests.append((path, method, body))
        if path.endswith("/commits/develop"):
            return {"sha": self.source}
        if "/compare/" in path:
            return {"behind_by": 0}
        if path.endswith("/check-runs?filter=latest&per_page=100"):
            return copy.deepcopy(self.check_runs)
        if path.endswith("/actions/runs/12"):
            return copy.deepcopy(self.run)
        if path.endswith("/pulls/123/merge"):
            return {"merged": True}
        raise AssertionError((path, method, body))


class TargetTests(unittest.TestCase):
    def test_allows_expected_source_and_digest(self):
        self.assertEqual(config()["image"], IMAGE)

    def test_rejects_wrong_source_or_digest(self):
        for change in (
            {"SOURCE_REPOSITORY": "evil/repo"},
            {"SOURCE_BRANCH": "main"},
            {"SOURCE_SHA": ""},
            {"IMAGE_REF": IMAGE.replace("be-dev", "be")},
            {"MANIFEST_PATH": "clusters/oracle/other.yaml"},
        ):
            environment = {
                "PR_NUMBER": "123",
                "EXPECTED_HEAD": HEAD,
                "MANIFEST_PATH": PATH,
                "DEPLOYMENT": "backend",
                "IMAGE_REF": IMAGE,
                "SOURCE_REPOSITORY": "Disease-Prediction-ASET26/disease-prediction-be",
                "SOURCE_BRANCH": "develop",
                "SOURCE_SHA": SOURCE,
            }
            environment.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                merge_gitops.settings(environment)

    def test_exact_deployment_replacement_preserves_other_bytes(self):
        content = (
            ORIGINAL
            + "\n---\nkind: Deployment\nmetadata:\n  name: frontend\nspec:\n  image: existing\n\n"
        )
        self.assertEqual(
            merge_gitops.image_change(content, "backend", IMAGE),
            content.replace("image: old", "image: " + IMAGE),
        )
        with self.assertRaises(ValueError):
            merge_gitops.image_change(content, "missing", IMAGE)

    def test_rejects_additional_content_change(self):
        api = FakeAPI()
        files = [
            {"filename": PATH, "status": "modified", "additions": 1, "deletions": 1}
        ]
        with (
            patch.object(api, "request", return_value=files),
            patch.object(
                merge_gitops,
                "contents",
                side_effect=[ORIGINAL, UPDATED + "other: true\n"],
            ),
            self.assertRaisesRegex(ValueError, "more than"),
        ):
            merge_gitops.verify_change(api, config(), pull(), HEAD)

    def test_rejects_wrong_pr_author_and_head(self):
        for change in ("author", "head"):
            item = pull()
            if change == "author":
                item["user"]["type"] = "User"
            else:
                item["head"]["sha"] = "f" * 40
            with self.subTest(change=change), self.assertRaises(ValueError):
                merge_gitops.verify_pull(item, config(), HEAD)


class CheckTests(unittest.TestCase):
    def test_requires_both_checks_from_expected_workflow(self):
        api = FakeAPI()
        self.assertTrue(merge_gitops.checked(api, HEAD))
        api.check_runs = checks(check("validate", 1))
        self.assertFalse(merge_gitops.checked(api, HEAD))

    def test_latest_failing_or_pending_check_cannot_use_old_success(self):
        api = FakeAPI()
        api.check_runs = checks(
            check("validate", 1),
            check("secret-scan / secret-scan", 2),
            check("validate", 3, conclusion="failure"),
        )
        with self.assertRaisesRegex(ValueError, "did not pass"):
            merge_gitops.checked(api, HEAD)
        api.check_runs["check_runs"][2].update(status="in_progress", conclusion=None)
        self.assertFalse(merge_gitops.checked(api, HEAD))

    def test_rejects_checks_from_wrong_workflow_or_commit(self):
        api = FakeAPI()
        api.run["path"] = ".github/workflows/unrelated.yml"
        with self.assertRaisesRegex(ValueError, "expected GitOps"):
            merge_gitops.checked(api, HEAD)
        api = FakeAPI()
        api.check_runs = checks(
            check("validate", 1, head=BASE), check("secret-scan / secret-scan", 2)
        )
        self.assertFalse(merge_gitops.checked(api, HEAD))


class MergeTests(unittest.TestCase):
    def test_merges_only_checked_head(self):
        api = FakeAPI()
        with patch.object(merge_gitops, "verify_change") as verify:
            merge_gitops.merge(api, config())
        self.assertEqual(verify.call_count, 1)
        self.assertIn(
            (
                f"{merge_gitops.REPO_PATH}/pulls/123/merge",
                "PUT",
                {"sha": HEAD, "merge_method": "squash"},
            ),
            api.requests,
        )

    def test_moved_source_never_merges(self):
        api = FakeAPI()
        api.source = "f" * 40
        merge_gitops.merge(api, config())
        self.assertFalse(any(method != "GET" for _, method, _ in api.requests))

    def test_source_moving_after_checks_never_merges(self):
        api = FakeAPI()
        with (
            patch.object(merge_gitops, "source_current", side_effect=[True, False]),
            patch.object(merge_gitops, "verify_change"),
        ):
            merge_gitops.merge(api, config())
        self.assertFalse(any(method != "GET" for _, method, _ in api.requests))

    def test_changed_head_never_merges(self):
        api = FakeAPI()
        api.current_pull["head"]["sha"] = "e" * 40
        with self.assertRaises(ValueError):
            merge_gitops.merge(api, config())
        self.assertFalse(any(method != "GET" for _, method, _ in api.requests))

    def test_check_failure_never_merges(self):
        api = FakeAPI()
        api.check_runs = checks(
            check("validate", 1, conclusion="failure"),
            check("secret-scan / secret-scan", 2),
        )
        with self.assertRaises(ValueError):
            merge_gitops.merge(api, config())
        self.assertFalse(any(method != "GET" for _, method, _ in api.requests))

    def test_base_refresh_accepts_only_expected_merge_commit(self):
        api = FakeAPI()
        new_head = "e" * 40
        api.current_pull["head"]["sha"] = new_head
        with (
            patch.object(
                api,
                "request",
                side_effect=[None, {"parents": [{"sha": HEAD}, {"sha": BASE}]}],
            ) as request,
            patch.object(merge_gitops, "includes_base", return_value=True),
        ):
            self.assertEqual(
                merge_gitops.updated_head(api, config(), HEAD, 10**20), new_head
            )
        self.assertEqual(
            request.call_args_list[0].kwargs,
            {"method": "PUT", "body": {"expected_head_sha": HEAD}},
        )

        with (
            patch.object(
                api,
                "request",
                side_effect=[None, {"parents": [{"sha": "f" * 40}, {"sha": BASE}]}],
            ),
            self.assertRaisesRegex(ValueError, "outside"),
        ):
            merge_gitops.updated_head(api, config(), HEAD, 10**20)


if __name__ == "__main__":
    unittest.main()
