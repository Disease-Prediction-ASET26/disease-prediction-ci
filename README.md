# Disease Prediction CI

This repository contains the shared GitHub Actions workflows for the AI,
backend, and frontend repositories. Each application keeps its own commands in
`.github/workflows/ci.yml`.

See the [cross-repository DevOps overview](https://github.com/Disease-Prediction-ASET26/disease-prediction-docs/blob/main/devops/README.md)
for the system map. This repository remains the source for shared workflow
behavior and pinned versions.

## Application pipelines

The pipelines provide these stages: `workflow-lint`, `secret-scan`, `lint`,
`build`, `unit-test`, `integration-test`, `owasp`, `containerize`, and `scan`.
Containerization and image scanning run in the same job.

| Repository | Enabled checks |
| ---------- | -------------- |
| AI | Workflow lint, Gitleaks, Ruff, Python compile check, pytest with coverage, Dependency-Check, pip-audit, container build and Trivy scan |
| Backend | Workflow lint, Gitleaks, Spotless, Maven package and tests, Dependency-Check, container build and Trivy scan |
| Frontend | Workflow lint, Gitleaks, ESLint, TypeScript/Vite build, Vitest unit and integration tests, npm audit, container build and Trivy scan |

Image builds use `Dockerfile.dev` for `develop` and pull requests targeting
`develop`. They use `Dockerfile.prod` for `main` and pull requests targeting
`main`. Builds run only after the configured checks pass.

## Use the pipelines

1. Create a feature branch in the application repository.
2. Open an internal pull request to `develop`.
3. Open the **Actions** tab or the pull request checks.
4. Check the `ci` job summary for results and disabled stages.
5. Merge the pull request after the required checks pass. Promote `develop` to
   `main` with a pull request when a production release is ready.

The `ci` check fails if a required stage fails. Reports are uploaded as workflow
artifacts for three days. Workflows run after pushes to `develop` and `main`.
Fork pull request workflows are disabled.

## Dependency scanning

The AI and backend pipelines use OWASP Dependency-Check. The AI pipeline also
uses `pip-audit` to check resolved Python dependencies. The frontend pipeline
uses `npm audit` with a HIGH severity threshold. The shared scan workflow
currently supports Python and Maven.

Dependency-Check fails at CVSS 7 or higher. `pip-audit` fails on any advisory.
Scanner errors fail the job. Maven scans use Sonatype's current Central Search
Solr API endpoint. The credential-dependent OSS Index analyzer is disabled. A
consumer may provide a root-level `owasp-suppression.xml` for
documented false positives. The shared workflow forwards it beside the Python
dependency list or Maven build artifact before scanning. Keep exceptions
narrow by matching the affected package and vulnerability, and explain why each
exception is safe.

The shared updater refreshes the OWASP database every day at `03:17 UTC`. It
publishes a private GHCR snapshot only after the smoke test passes. A failed
update leaves the previous snapshot available. Each application scan uses its
own copy. A missing or more-than-48-hour-old snapshot fails the scan.

## Secret scanning

Each application and GitOps pipeline calls the shared Gitleaks workflow on pull
requests and pushes. It scans the commits introduced by that change, redacts
detected values from logs, and fails the pipeline when it finds a secret. The
scanner does not validate candidate credentials against provider APIs. GitHub
secret scanning remains responsible for scanning repository history. The
Gitleaks binary is pinned by version and SHA-256 checksum.

To run a normal refresh manually, open **Actions > Refresh OWASP snapshot** in
this repository. Select **Run workflow** on `main`. Leave `bootstrap` disabled
to reuse the existing snapshot.

## Container images

The shared workflow selects a dev or production Dockerfile from the target
branch, builds for Linux ARM64, scans with Trivy, and publishes only after the
application checks pass. The full HIGH and CRITICAL report is retained as a
workflow artifact. Publishing fails when a finding has an available fix;
unfixed findings remain visible in the report for review.

The backend and frontend pipelines pass their Oracle dev Deployment names to
the shared workflow. After a successful push to `develop`, CI opens or updates a
GitOps pull request with the published image digest. Merge that pull request to
let Argo CD sync the new image. The workflow needs a GitHub App installed only
on `disease-prediction-gitops`, with Contents and Pull requests write access;
the calling repositories must provide `GITOPS_CLIENT_ID` (the app Client ID) and
`GITOPS_APP_PRIVATE_KEY` (the complete PEM key) as Actions secrets. CI does not push image changes
directly to the GitOps `main` branch. The AI model-runtime release has a
separate digest update because its deployable image is produced by the reviewed
model release workflow, not the normal AI base-image build.

| Build | Private GHCR package | Tags |
| ----- | -------------------- | ---- |
| Internal PR | `ghcr.io/disease-prediction-aset26/<repo>-pr` | Unique run tag and `pr-<number>` |
| `develop` | `ghcr.io/disease-prediction-aset26/<repo>-dev` | Unique run tag and `develop` |
| `main` | `ghcr.io/disease-prediction-aset26/<repo>` | Unique run tag and `main` |

Use the image digest from the job summary to identify an exact image. Preview
cleanup runs when an internal PR closes and each day at `03:47 UTC`. The dev
package keeps a moving `develop` tag and unique run tags; the production package
keeps a moving `main` tag and unique run tags. The preview package needs
**Admin** access for its application repository to delete image versions.
The publisher retries a push twice (three attempts total) when GHCR reports
`unknown blob`.
Production and dev packages are not cleaned automatically.

The local, dev, and production Compose environments are documented in the
[`disease-prediction-docker` repository](https://github.com/Disease-Prediction-ASET26/disease-prediction-docker).

## Shared workflows

| Workflow                                                     | Purpose                                         |
| ------------------------------------------------------------ | ----------------------------------------------- |
| [secret-scan.yml](.github/workflows/secret-scan.yml)          | Find secrets in commits introduced by a change. |
| [dependency-scan.yml](.github/workflows/dependency-scan.yml) | Scan Python and Maven dependencies.             |
| [owasp-refresh.yml](.github/workflows/owasp-refresh.yml)     | Update and publish the OWASP database snapshot. |
| [container.yml](.github/workflows/container.yml)             | Select, build, scan, and publish a dev or production image. |
| [preview-cleanup.yml](.github/workflows/preview-cleanup.yml) | Remove outdated PR preview images.              |
| [validate.yml](.github/workflows/validate.yml)               | Check shared workflows and Python tools.        |

Actions and reusable workflows use full commit SHAs. Dependabot proposes weekly
updates. See [TOOL-VERSIONS.md](TOOL-VERSIONS.md) for the pinned versions.

## Check changes to this repository

Use Python 3.13 and the tool versions in [TOOL-VERSIONS.md](TOOL-VERSIONS.md).
Run these commands before opening a PR:

```sh
ruff check --isolated actions tests
ruff format --check --isolated actions tests
python -m unittest discover -s tests -v
actionlint .github/workflows/*.yml
```
