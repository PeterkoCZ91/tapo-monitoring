# Releasing

A published GitHub release builds the sdist and wheel and uploads them to
[PyPI](https://pypi.org/project/tapo-monitor/) through
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/). No API token is stored
in the repository or in GitHub secrets; PyPI trusts one workflow in one environment.

## One-time setup

On pypi.org, logged in as the account that will own the project:

1. Open *Your account → Publishing → Add a new pending publisher* (the project does not
   exist yet, so it is a *pending* publisher; after the first upload it becomes an
   ordinary trusted publisher of `tapo-monitor`).
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

A pending publisher does not reserve the name: until the first upload, anyone can
register `tapo-monitor` first. Publish the first release soon after adding it.

## Each release

1. Bump `version` in `pyproject.toml` and `__version__` in `tapo_monitor/__init__.py`,
   and move the changelog's *Unreleased* section under the new version.
2. Commit, push, and wait for CI to pass.
3. Tag `vX.Y.Z` (the workflow refuses a tag that does not match the package version)
   and publish a GitHub release from it.
4. Watch the *Release* workflow; when it is green, `pip install tapo-monitor==X.Y.Z`
   into a fresh virtualenv and run `tapo-monitor version`.

PyPI never accepts the same version twice. A broken upload is fixed by a new version,
optionally yanking the bad one on pypi.org.

To try the build without publishing, run the *Release* workflow by hand
(*Actions → Release → Run workflow*): it builds and checks the distributions and skips
the upload.
