# Publishing Docket

This file is the maintainer runbook for shipping a release. It exists because two of the
steps cannot be automated, and a future maintainer should not have to rediscover why.

## What CI does, and what it deliberately does not

`.github/workflows/release.yml` runs on a pushed tag and does the whole gate:

1. Checks out the tag.
2. Installs the test dependencies.
3. Runs the full suite. The release log is the evidence that the tag was tested.
4. Builds the sdist and the wheel with `python -m build`.
5. Writes `SHA256SUMS` over `dist/*` and prints it.
6. Extracts this version's section from `CHANGELOG.md` and fails the job if that extraction
   is empty.
7. Creates the GitHub release with those artefacts attached.

The workflow does **not** upload to PyPI. That is deliberate, and the reason is blast radius
rather than missing plumbing: a PyPI token can publish under the project's name forever and
cannot be scoped to a branch, so storing one as a repository secret hands it to everyone who
ever gains admin on this repository. `github.token` is scoped to a single run and expires
with it, which is why the GitHub release is automated and the PyPI upload is not.

To ship to PyPI:

```bash
# Requires: Python 3.11+, build, twine, and a PyPI token for the docket-ai project.
python3 -m build
twine check dist/*
twine upload dist/*
```

Do this from a machine whose key never touches CI.

## The distribution name is `docket-ai`, not `docket`

PyPI administratively prohibits the bare name `docket`. It is not taken:

```
$ curl -s -o /dev/null -w '%{http_code}\n' https://pypi.org/pypi/docket/json
404
```

and uploading to it still fails:

```
$ twine upload dist/*
HTTPError: 400 Bad Request from https://upload.pypi.org/legacy/
     The name 'docket' isn't allowed. See https://pypi.org/help/#project-name
     for more information.
```

PyPI keeps a prohibited-project-names list to prevent dependency-confusion attacks. Names
land on it by administrator decision, not because another project owns them, so the JSON API
answering 404 and the upload being refused are consistent rather than contradictory.

Everything except the distribution name is `docket`:

| thing | value |
| --- | --- |
| distribution name (`pip install` takes this) | `docket-ai` |
| import name | `docket` |
| console script | `docket` |
| the command a user types | `docket` |

The distribution name is written in exactly one place, `pyproject.toml`, and
`tests/test_doc_links.py` reads it from there rather than repeating it, so a rename cannot
leave the guard checking one name while the guide tells a reader another.

### Getting the bare name, if you want it

The only path is a PEP 541 request on <https://github.com/pypi/support>, reviewed by the PyPI
administrators. It is not an API call and cannot be filed by CI.

1. Go to <https://github.com/pypi/support/issues/new/choose> and pick the PEP 541 template.
2. Set **Project to be claimed** to `docket`, and **Your PyPI username** to the account that
   owns the PyPI project.
3. Say plainly that the name is administratively prohibited rather than in use, that
   `https://pypi.org/pypi/docket/json` answers 404, and that there is no existing project or
   owner to contact. Give the source repository URL.
4. Wait for the admin queue.

Until that is granted, `pip install docket` will keep answering "no matching distribution",
which is why the README teaches `pip install docket-ai`. If the bare name is later released,
publishing under it means a new project on PyPI: PyPI project names cannot be renamed, and
the `docket-ai` releases stay where they are.

## Cutting a release

```bash
# Requires: git, and push access to the repository.
git tag -a vX.Y.Z -m "Docket X.Y.Z"
git push origin vX.Y.Z
```

The tag gate runs the suite before it ships anything, so a red suite on the tag is a real
failure rather than a flake. If the release already exists for that tag, delete both first:

```bash
gh release delete vX.Y.Z --cleanup-tag -y
```

and re-push the tag. Do not create the release by hand while `release.yml` exists: the
workflow's own `gh release create` will then fail with *"a release with the same tag name
already exists"* after its tests and build have already passed.
