# Releasing

A published GitHub release builds the sdist and wheel and uploads them to
[PyPI](https://pypi.org/project/tapo-monitor/) through
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/). No API token is stored
in the repository or in GitHub secrets; PyPI trusts one workflow in one environment.

The same published release also triggers the Docker workflow, which publishes monitor
and scorer images for amd64 and arm64 to GHCR. Publication changes both distribution
channels; a local build does not publish either one.

## One-time setup

On pypi.org, logged in as the account that will own the project:

1. For a new PyPI project, open *Your account → Publishing → Add a new pending
   publisher*. For an existing project, configure its trusted publisher in the
   project's publishing settings. This setup is performed once, not for each release.
2. Choose **GitHub** and fill in exactly:

   | Field | Value |
   | --- | --- |
   | PyPI project name | `tapo-monitor` |
   | Owner | `PeterkoCZ91` |
   | Repository name | `tapo-monitoring` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

On GitHub, in the repository's *Settings → Environments*:

3. Create an environment named `pypi`. Optionally add a required reviewer, so an upload
   waits for a click, and restrict deployments to tags matching `v*`.

A pending publisher does not reserve a new project's name before its first upload.

## Each release

1. Bump `version` in `pyproject.toml` and `__version__` in `tapo_monitor/__init__.py`,
   and move the changelog's *Unreleased* section under the new version. A prepared
   candidate can retain an `Unreleased` date; set the actual date before publication.
   Check the [upgrade notes](upgrading.md), configuration and capability documentation.
2. Check the complete outgoing history for private data, not just the final tree.
   Run the CI gates: Python 3.12/3.13 pytest with coverage and scorer tests collected,
   ruff, mypy, shellcheck, Bats and the three-seed two-day soak. Build the distributions
   with `python -m build`, run `twine check --strict dist/*`, and check a wheel install
   in a clean virtualenv. Commit, push, and wait for CI to pass.
3. Tag `vX.Y.Z` (the workflow refuses a tag that does not match the package version)
   and publish a GitHub release from it.
4. Watch the *Release* workflow; when it is green, `pip install tapo-monitor==X.Y.Z`
   into a fresh virtualenv and run `tapo-monitor version`.

PyPI never accepts the same version twice. A broken upload is fixed by a new version,
optionally yanking the bad one on pypi.org.

To try the build without publishing, run the *Release* workflow by hand
(*Actions → Release → Run workflow*): it builds and checks the distributions and skips
the upload.

The manually dispatched *Docker images* workflow publishes images; it is not the
distribution-build dry run described above.
