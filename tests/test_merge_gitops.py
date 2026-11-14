import copy
import importlib.util
import io
import unittest
import urllib.error
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
        self.base = BASE
        self.source = SOURCE
        self.check_runs = successful_checks()
        self.run = workflow_run()

    def pull(self, number):
        return copy.deepcopy(self.current_pull)

    def request(self, path, *, method="GET", body=None):
        self.requests.append((path, method, body))
        if path.endswith("/commits/develop"):
            return {"sha": self.source}
        if path.endswith("/git/ref/heads/main"):
            return {"object": {"sha": self.base}}
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
            patch.object(merge_gitops, "main_head", return_value=BASE),
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

    def test_retry_merge_only_when_the_base_has_moved(self):
        for status in (405, 409, 422):
            with self.subTest(status=status):
                self.check_retry_after_base_move(status)

    def check_retry_after_base_move(self, status):
        api = FakeAPI()
        previous = api.request
        new_head = "e" * 40
        new_base = "f" * 40
        merge_attempts = []

        def request(path, *, method="GET", body=None):
            if path.endswith("/pulls/123/merge"):
                merge_attempts.append(body["sha"])
                if len(merge_attempts) == 1:
                    api.current_pull["base"]["sha"] = new_base
                    api.base = new_base
                    raise urllib.error.HTTPError(
                        path, status, "merge rejected", {}, None
                    )
            return previous(path, method=method, body=body)

        def update(_api, _settings, head, _deadline):
            self.assertEqual(head, HEAD)
            api.current_pull["head"]["sha"] = new_head
            return new_head

        with (
            patch.object(api, "request", side_effect=request),
            patch.object(
                merge_gitops, "includes_base", side_effect=[True, False, True]
            ),
            patch.object(merge_gitops, "updated_head", side_effect=update),
            patch.object(merge_gitops, "verify_change"),
            patch.object(merge_gitops, "checked", return_value=True),
        ):
            merge_gitops.merge(api, config())
        self.assertEqual(merge_attempts, [HEAD, new_head])

    def test_persistent_merge_error_is_reported_after_bounded_retries(self):
        api = FakeAPI()
        previous = api.request

        def request(path, *, method="GET", body=None):
            if path.endswith("/pulls/123/merge"):
                raise urllib.error.HTTPError(path, 405, "merge rejected", {}, None)
            return previous(path, method=method, body=body)

        with (
            patch.object(api, "request", side_effect=request),
            patch.object(merge_gitops, "verify_change"),
            patch.object(merge_gitops.time, "sleep"),
            self.assertRaisesRegex(ValueError, "retries exhausted"),
        ):
            merge_gitops.merge(api, config())

    def test_merge_conflict_with_a_new_pr_head_is_not_retried(self):
        api = FakeAPI()
        previous = api.request

        def request(path, *, method="GET", body=None):
            if path.endswith("/pulls/123/merge"):
                api.current_pull["base"]["sha"] = "f" * 40
                api.current_pull["head"]["sha"] = "e" * 40
                raise urllib.error.HTTPError(path, 409, "head changed", {}, None)
            return previous(path, method=method, body=body)

        with (
            patch.object(api, "request", side_effect=request),
            patch.object(merge_gitops, "verify_change"),
            self.assertRaisesRegex(ValueError, "changed"),
        ):
            merge_gitops.merge(api, config())

    def test_transient_merge_rejection_rechecks_before_retry(self):
        api = FakeAPI()
        previous = api.request
        attempts = []

        def request(path, *, method="GET", body=None):
            if path.endswith("/pulls/123/merge"):
                attempts.append(body["sha"])
                if len(attempts) == 1:
                    raise urllib.error.HTTPError(
                        path,
                        405,
                        "Method Not Allowed",
                        {},
                        io.BytesIO(b'{"message":"Mergeability is being calculated"}'),
                    )
            return previous(path, method=method, body=body)

        with (
            patch.object(api, "request", side_effect=request),
            patch.object(merge_gitops, "verify_change"),
            patch.object(merge_gitops, "checked", return_value=True) as checked,
            patch.object(merge_gitops.time, "sleep"),
        ):
            merge_gitops.merge(api, config())
        self.assertEqual(attempts, [HEAD, HEAD])
        self.assertEqual(checked.call_count, 4)

    def test_merge_rejection_rechecks_base_ancestry(self):
        api = FakeAPI()
        previous = api.request
        new_head = "e" * 40
        attempts = []

        def request(path, *, method="GET", body=None):
            if path.endswith("/pulls/123/merge"):
                attempts.append(body["sha"])
                if len(attempts) == 1:
                    raise urllib.error.HTTPError(path, 405, "not mergeable", {}, None)
            return previous(path, method=method, body=body)

        def update(_api, _settings, head, _deadline):
            api.current_pull["head"]["sha"] = new_head
            return new_head

        with (
            patch.object(api, "request", side_effect=request),
            patch.object(
                merge_gitops, "includes_base", side_effect=[True, False, True]
            ),
            patch.object(merge_gitops, "updated_head", side_effect=update) as refresh,
            patch.object(merge_gitops, "verify_change"),
            patch.object(merge_gitops, "checked", return_value=True) as checked,
            patch.object(merge_gitops.time, "sleep"),
        ):
            merge_gitops.merge(api, config())
        self.assertEqual(attempts, [HEAD, new_head])
        self.assertTrue(
            any(call.args == (api, new_head) for call in checked.call_args_list)
        )
        refresh.assert_called_once()

    def test_main_ref_change_waits_for_stale_pr_and_refreshes_head(self):
        api = FakeAPI()
        new_base = "f" * 40
        new_head = "e" * 40
        api.base = new_base
        pull_calls = 0

        def stale_pull(number):
            nonlocal pull_calls
            result = copy.deepcopy(api.current_pull)
            pull_calls += 1
            if pull_calls == 1:
                api.current_pull["base"]["sha"] = new_base
            return result

        def update(_api, _settings, head, _deadline):
            self.assertEqual(head, HEAD)
            api.current_pull["head"]["sha"] = new_head
            return new_head

        with (
            patch.object(api, "pull", side_effect=stale_pull),
            patch.object(merge_gitops, "includes_base", side_effect=[False, True]),
            patch.object(merge_gitops, "updated_head", side_effect=update) as refresh,
            patch.object(merge_gitops, "verify_change"),
            patch.object(merge_gitops, "checked", return_value=True),
            patch.object(merge_gitops.time, "sleep") as sleep,
        ):
            merge_gitops.merge(api, config())
        sleep.assert_called_once_with(5)
        refresh.assert_called_once()
        self.assertIn(
            (
                f"{merge_gitops.REPO_PATH}/pulls/123/merge",
                "PUT",
                {"sha": new_head, "merge_method": "squash"},
            ),
            api.requests,
        )

    def test_merge_405_recovers_when_pr_base_api_is_stale(self):
        api = FakeAPI()
        previous = api.request
        new_base, new_head = "f" * 40, "e" * 40
        attempts = []
        stale_reads = 0

        def request(path, *, method="GET", body=None):
            if path.endswith("/pulls/123/merge"):
                attempts.append(body["sha"])
                if len(attempts) == 1:
                    api.base = new_base
                    raise urllib.error.HTTPError(path, 405, "not mergeable", {}, None)
            return previous(path, method=method, body=body)

        def stale_pull(number):
            nonlocal stale_reads
            result = copy.deepcopy(api.current_pull)
            if api.base != api.current_pull["base"]["sha"]:
                stale_reads += 1
                if stale_reads == 2:
                    api.current_pull["base"]["sha"] = new_base
            return result

        def update(_api, _settings, head, _deadline):
            self.assertEqual(head, HEAD)
            api.current_pull["head"]["sha"] = new_head
            return new_head

        with (
            patch.object(api, "request", side_effect=request),
            patch.object(api, "pull", side_effect=stale_pull),
            patch.object(
                merge_gitops, "includes_base", side_effect=[True, False, True]
            ),
            patch.object(merge_gitops, "updated_head", side_effect=update),
            patch.object(merge_gitops, "verify_change"),
            patch.object(merge_gitops, "checked", return_value=True),
            patch.object(merge_gitops.time, "sleep"),
        ):
            merge_gitops.merge(api, config())
        self.assertEqual(attempts, [HEAD, new_head])

    def test_http_error_detail_includes_safe_message_and_request_id(self):
        error = urllib.error.HTTPError(
            "https://api.github.com/endpoint",
            405,
            "Method Not Allowed",
            {"X-GitHub-Request-Id": "ABCD:1234"},
            io.BytesIO(b'{"message":"Required check pending\\nRetry"}'),
        )
        self.assertEqual(
            merge_gitops.http_error_detail(error),
            "GitHub HTTP 405: Required check pending Retry (request ABCD:1234)",
        )

    def test_permission_error_is_reported_without_retry(self):
        api = FakeAPI()
        previous = api.request
        attempts = []

        def request(path, *, method="GET", body=None):
            if path.endswith("/pulls/123/merge"):
                attempts.append(body["sha"])
                raise urllib.error.HTTPError(
                    path,
                    403,
                    "Forbidden",
                    {},
                    io.BytesIO(b'{"message":"Resource not accessible by integration"}'),
                )
            return previous(path, method=method, body=body)

        with (
            patch.object(api, "request", side_effect=request),
            patch.object(merge_gitops, "verify_change"),
            patch.object(merge_gitops.time, "sleep") as sleep,
            self.assertRaisesRegex(ValueError, "not accessible by integration"),
        ):
            merge_gitops.merge(api, config())
        self.assertEqual(attempts, [HEAD])
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
