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
# Requires: Python 3.11+, git, build, twine, and a PyPI token for the docket-ai project.
export TWINE_USERNAME="__token__"
export TWINE_PASSWORD="$(cat /path/to/your/pypi-token)"
python3 tools/build_dist.py --ref vX.Y.Z
python3 tools/build_dist.py --check --ref vX.Y.Z
twine check dist/*.whl dist/*.tar.gz
twine upload dist/*.whl dist/*.tar.gz
```

Build from the ref, never from the working tree. This is not a style preference: the 0.1.4
wheel and sdist were built from a working tree, two commits landed before the upload, and the
published long description lacked the README logo the repository already had. The 0.1.5 sdist
did it again one commit behind its `.gitignore`. Neither could be fixed, because PyPI reserves
a filename once it is published.

`tools/build_dist.py` exports the ref with `git archive` and builds from that export, so an
untracked file cannot leak in and a dirty tracked file exports at the committed version. It
writes `dist/PROVENANCE.json` recording the ref, the commit and each artifact's digest, and
`--check` refuses to pass when `dist/` does not match the ref you are about to release. Run it
after the build and before `twine upload`, every time.

Both `twine` commands name the two extensions rather than `dist/*`, because `dist/` also holds
`PROVENANCE.json` and `twine check dist/*` refuses a file it does not recognise.

`TWINE_USERNAME` must be the literal string `__token__`, not your account name. PyPI has
withdrawn username and password authentication from the upload endpoint entirely, so a token
sent under any other username is refused with `403 Username/Password authentication is no longer
supported`. This is easy to get wrong, because the token itself is correct and the error names
authentication rather than the username.

Do this from a machine whose key never touches CI.

## The distribution name is `docket-ai`, not `docket`

PyPI administratively prohibits the bare name `docket`. It is not taken:

```
$ curl -s -o /dev/null -w '%{http_code}\n' https://pypi.org/pypi/docket/json
404
```

and uploading to it still fails:

```
$ twine upload dist/*.whl dist/*.tar.gz
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

### Upload to PyPI before you push the tag

`tests/test_packaging.py` installs the *published* distribution, so the tag gate exercises
whatever PyPI is serving for that version. Push the tag first and those three tests skip with
that reason, which is honest and green; upload first and they run for real. The order that
produces a gate measuring nothing is the first one, so prefer the second:

```bash
python3 tools/build_dist.py --ref vX.Y.Z      # export, build, record provenance
twine check dist/*.whl dist/*.tar.gz
twine upload dist/*.whl dist/*.tar.gz        # PyPI only: see "why this is manual" above
# wait for the index to serve it, then:
git tag -a vX.Y.Z -m "Docket X.Y.Z"
git push origin vX.Y.Z
```

The wait is real and it is not a formality. The JSON API and the simple index are different
caches, and `pip` resolves against the simple index: a tag pushed in the minute after an upload
finds a guard that says the version exists and an installer that says it does not. That happened
once, on 0.2.1, and the release gate went red on a propagation delay. The guard now reads the
simple index for exactly this reason, so it skips rather than failing — but a skipped install
check is not a performed one, and the release record should say which it was.

The tag gate runs the suite before it ships anything, so a red suite on the tag is a real
failure rather than a flake. If the release already exists for that tag, delete both first:

```bash
gh release delete vX.Y.Z --cleanup-tag -y
```

and re-push the tag. Do not create the release by hand while `release.yml` exists: the
workflow's own `gh release create` will then fail with *"a release with the same tag name
already exists"* after its tests and build have already passed.
