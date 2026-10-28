# Disease Prediction CI

This repository contains shared security workflows for AI, backend, and
frontend. Each application keeps its commands in
`.github/workflows/ci.yml`.

## Current checks

| Repository | Enabled checks                                                            | Missing prerequisites                                             |
| ---------- | ------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| AI         | Workflow lint, Ruff, Python compilation, Dependency-Check, pip-audit      | Unit tests, integration tests, service packaging, Dockerfile      |
| Backend    | Workflow lint, Maven package, Spring context smoke test, Dependency-Check | Java lint, unit tests, Dockerfile                                 |
| Frontend   | Workflow lint and readiness summary                                       | Runtime, package manager, application commands, tests, Dockerfile |

SonarQube and container publication are disabled by default. An unset enable
variable means `false`. A failed enabled check makes the `ci` job fail. Its
summary lists disabled checks and their prerequisites. Disabled tests provide
no coverage. The AI build checks Python compilation only. The backend smoke
test runs the existing application test. Application failures remain visible.

## Set up shared scanning

Use a GitHub account with administration access to these repositories. The CI
repository must permit reuse from repositories in this organization:
**Settings > Actions > General > Access > Accessible from repositories in the
organization**.

1. Add the free NVD API key as a CI repository secret. Keep the key out of
   application repositories.

   ```sh
   gh secret set NVD_API_KEY --repo Disease-Prediction-ASET26/disease-prediction-ci
   ```

2. Create the first snapshot. The updater copies a pinned OWASP database and
   refreshes it before publication.

   ```sh
   gh workflow run owasp-refresh.yml --repo Disease-Prediction-ASET26/disease-prediction-ci --ref main -f bootstrap=true
   gh run list --repo Disease-Prediction-ASET26/disease-prediction-ci --workflow owasp-refresh.yml
   ```

3. Wait for the database update, smoke test, and publication to pass.
4. Open the private `owasp-dependency-check` GHCR package. Under **Manage
   Actions access**, give `disease-prediction-ci` **Write** access. Give each
   application repository **Read** access.
   If inherited permissions prevent changes, remove inheritance. Restore any
   team access that the change removes.
5. Enable daily updates after the first successful snapshot:

   ```sh
   gh variable set OWASP_UPDATER_ENABLED --body true --repo Disease-Prediction-ASET26/disease-prediction-ci
   ```

6. Submit the application `.github/` changes through internal branches. Keep
   fork PR workflows disabled in every repository.

The updater runs at `03:17 UTC`. It restores `dc-13.0.0` and updates NVD records.
It publishes a snapshot only after the smoke test passes. A failed update
preserves the previous snapshot.

Use `bootstrap=true` for first use or a reviewed database upgrade. The seed is
pinned to an image digest. Before a later bootstrap, check its age and
compatibility. If necessary, review and update the digest.

The updater caches refreshed data before packaging and smoke checks. If a later
step fails, retry with `bootstrap=false` to reuse that data. A cache miss restores
the published compatible snapshot. Failed NVD updates never replace cached data.

Each scan resolves the channel to a digest and records it in the summary. Each
scan uses its own writable database copy with `--noupdate`. Missing,
incompatible, or older-than-48-hour snapshots fail the scan. The NVD key stays
outside image layers, artifacts, and caches.

## Enable application stages

Add the missing commands and tests before changing a `false` job condition.
When a stage becomes active, update its `ci` prerequisite message. Keep
`unit-test` and `integration-test` separate. A run with zero tests must fail.

The backend build artifact contains the JAR and compiled classes. OWASP scans
that artifact. AI CI resolves application dependencies in a separate virtual
environment. It audits the complete resolved set. Audit tools stay outside
that environment. The Python Dependency-Check analyzers are experimental.
`pip-audit` provides an additional check. OSS Index is disabled because it
requires separate credentials.

Dependency-Check fails at CVSS 7 or higher. Trivy fails on HIGH or CRITICAL
findings, including unfixed findings. `pip-audit` fails on any advisory. Scanner
errors fail the job. Reports are retained for three days.

## Containers and PR previews

Supply a root `Dockerfile` before setting the application's `CONTAINER_ENABLED`
variable to `true`. For the backend, use the downloaded `target/` artifact in
the Dockerfile. Provide `.dockerignore` with the application Dockerfile. Exclude
secrets, local environments, test data, and Git metadata.

The container workflow builds and scans one `linux/amd64` image. It publishes
that same image. It compares the remote image configuration digest with the
image ID recorded before Trivy. BuildKit caches layers through GitHub Actions.

| Build       | Private package                               | Tags                                                              |
| ----------- | --------------------------------------------- | ----------------------------------------------------------------- |
| Internal PR | `ghcr.io/disease-prediction-aset26/<repo>-pr` | Unique `pr-<number>-<head-sha>-<run>-<attempt>` and `pr-<number>` |
| Main        | `ghcr.io/disease-prediction-aset26/<repo>`    | Unique `sha-<commit>-<run>-<attempt>` and `main`                  |

The summary contains the image digest. Use the digest to select an exact image.
Private pulls outside Actions need a GHCR read credential. Do not put application
secrets in image layers.

Cleanup retains the current successful preview until its replacement passes and
publishes. A closed internal PR triggers cleanup immediately. Nightly
reconciliation runs at `03:47 UTC`. Cleanup preserves other PRs, unknown tags,
and all production packages. Superseded builds cannot move the preview alias.
Publication and cleanup share a repository concurrency group. GitHub can replace
pending jobs in a concurrency group. If a pending build is cancelled and its
preview is still needed, rerun the build. The preview package must grant its
application repository **Admin** access for version deletion. Workflow-created
packages normally grant this automatically.

## SonarQube

Before enabling SonarQube, create the project and configure these values in each
application repository:

| Type     | Name                | Value                                           |
| -------- | ------------------- | ----------------------------------------------- |
| Variable | `SONAR_HOST_URL`    | Server URL reachable from GitHub-hosted runners |
| Variable | `SONAR_PROJECT_KEY` | Application project key                         |
| Secret   | `SONAR_TOKEN`       | Project analysis token                          |
| Variable | `SONAR_ENABLED`     | `true` after the server and project are ready   |

Analysis runs on main only and waits for the quality gate. PR analysis requires
a separate product/license decision. Expected coverage paths are AI
`coverage.xml`, backend `target/site/jacoco/jacoco.xml`, and frontend
`coverage/lcov.info`. Application teams must supply coverage generation and
artifact transfer before claiming coverage in SonarQube.

## Check and release changes

Create a feature branch before changing a repository. Link its PR to the
tracking issue. Merge the PR after its required checks pass. Do not push
implementation changes directly to `main`.

Use Python 3.13 and the versions recorded in
[TOOL-VERSIONS.md](TOOL-VERSIONS.md).

```sh
python -m pip install PyYAML==6.0.3 ruff==0.17.3
ruff check --isolated actions tests
ruff format --check --isolated actions tests
python -m unittest discover -s tests -v
actionlint .github/workflows/*.yml
```

The tests cover snapshot freshness, failed updates, secret exclusion,
disabled-stage gates, scan-before-publish behavior, and preview cleanup. The
live updater also proves that Dependency-Check rejects a checksum-verified Log4j
2.14.1 JAR. That live test needs a populated NVD database. Unit tests do not
replace it.

Actions and reusable workflows use full commit SHAs. Dependabot proposes weekly
action and scanner base updates. After tool changes, commit the tool files
first. Update internal action references to that commit. Then release the shared
workflows and update application references to the reviewed workflow commit.
Engine upgrades also require matching scanner constants, channel names, a fresh
database, and new consumer references. Do not change the engine or database
format through an unreviewed tag update.

GHCR container storage and bandwidth are currently free. Private Actions minutes
and artifact storage have separate quotas. If the organization plan supports
private branch protection, configure the final `ci` check as required. This
repository does not deploy applications or configure a cloud provider.

References: [OWASP caching](https://dependency-check.github.io/DependencyCheck/data/cacheh2.html),
[GHCR billing](https://docs.github.com/en/billing/concepts/product-billing/github-packages),
[Actions allowances](https://docs.github.com/en/billing/concepts/product-billing/github-actions).
