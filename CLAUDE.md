# Working on this repository

An unofficial Home Assistant custom integration for Jaguar Land Rover
InControl. Read-only: it reads the car, it never commands it. One domain,
`jlr_incontrol`, living in `custom_components/jlr_incontrol/`.

## Non-negotiables

**Never commit secrets.** No credentials, tokens, VINs, PINs, the codes JLR
emails during sign-in, encrypted vehicle ids, or coordinates — not in code,
tests, fixtures, commit messages, issue replies or docs. All of it lives only
in the Home Assistant config entry at runtime. `redact.py` exists for
diagnostics; extend it rather than working around it.

**Be a polite client.** JLR's backend is not ours and the users are signed in
with their own accounts. Every poll interval, retry and reconnect in here was
chosen to stay quiet, and a change that talks to JLR more often needs a reason
better than "it would feel more responsive". Floors like `PORTAL_FORCE_FLOOR`
and `RESUBSCRIBE_FLOOR` are there to stop a button press becoming a hammer.

**Stay read-only.** Remote control (lock, climate, charging, honk) was removed
in v1.4.0 and is not coming back by accident.

**Releases happen only when the maintainer asks.** Merging is not releasing.

## Local development

Four virtualenvs, one per CI lane. They are gitignored and may not exist in a
fresh container — rebuild them with `uv` if they are missing.

| venv | Python | What it is for |
| --- | --- | --- |
| `.venv-ha` | 3.14 | Tests against the latest core |
| `.venv-ha-min` | 3.13 | Tests against the oldest supported core (2025.8.0) |
| `.venv-lint` | 3.14 | black, isort, ruff, flake8 |
| `.venv-types` | 3.14 | mypy plus an unpinned core |

**Upgrade `uv` before building them.** The container ships an old `uv` whose
bundled Python list ends at a 3.14 release candidate, so it silently builds
`.venv-ha` on an RC and resolves a core months out of date — the exact trap the
`MAX_CORE_AGE_MONTHS` check in CI exists to catch. Run
`pip install --user --upgrade uv` first and use `~/.local/bin/uv`.

```sh
UV=~/.local/bin/uv
$UV venv --python 3.14 .venv-ha
$UV venv --python 3.13 .venv-ha-min
$UV venv --python 3.14 .venv-lint
$UV venv --python 3.14 .venv-types

$UV pip install --python .venv-ha/bin/python     -r requirements-test.txt
$UV pip install --python .venv-ha-min/bin/python -r requirements-test-min.txt
$UV pip install --python .venv-lint/bin/python   -r requirements-lint.txt
$UV pip install --python .venv-types/bin/python  -r requirements-types.txt homeassistant
```

## The checks

CI has eight named checks and all eight gate a merge. Run the first four
locally before pushing; the last four need the runner.

```sh
.venv-lint/bin/black --check custom_components/ tests/
.venv-lint/bin/isort --check-only custom_components/ tests/
.venv-lint/bin/ruff check custom_components/ tests/
.venv-lint/bin/flake8 custom_components/ tests/

.venv-ha/bin/python -m pytest -q \
  --cov=custom_components/jlr_incontrol \
  --cov-report=term-missing --cov-report=json --cov-fail-under=95
.venv-ha/bin/python scripts/coverage_gate.py
.venv-ha-min/bin/python -m pytest -q

.venv-types/bin/mypy custom_components/jlr_incontrol/
```

Both test lanes matter. The minimum lane runs the behaviour against core
2025.8.0, and it is the one that catches a helper this integration is not
entitled to use yet — `OptionsFlowWithReload` shipped here once while the
manifest claimed a core a year older than the release that introduced it.

`scripts/coverage_gate.py` enforces **95% per module**, not 95% overall.
`--cov-fail-under` is the number that hides one module rotting to nothing, so
both run.

mypy is Home Assistant's Platinum setting, spelled out in `pyproject.toml`
rather than inherited from `strict` so that relaxing one is visible. Its
`python_version = "3.14"` is core's floor, not this integration's — it does not
licence 3.14-only syntax here, and the imports lane enforces the real 3.13
floor.

## Versions and releases

- `custom_components/jlr_incontrol/manifest.json` carries the version; HACS
  reads it, not the tag, and the release workflow fails on a mismatch. Bump it
  in the same commit as the change.
- `hacs.json` carries the minimum core, asserted against the minimum import
  lane, so it cannot drift from `MIN_HA_VERSION` in `ci.yaml`.
- Release by `workflow_dispatch` on `release.yaml` with `version` (`v1.7.4`)
  and optional `notes`. Anything containing `beta` or `alpha` is marked a
  prerelease automatically.
- A commit body line starting `BREAKING:` is lifted whole into a warning
  section at the top of the release notes.
- Two archives ship, and they are not interchangeable: `jlr_incontrol.zip`
  wraps the directory for manual installs, `jlr_incontrol-hacs.zip` has the
  files at its root because HACS extracts straight into the integration
  directory. Shipping one file for both broke every HACS install in 1.6.1.

## What is already settled about JLR's data

Re-deriving these has cost days. They are documented at length in
`docs/ENTITIES.md` and `docs/DATA_MODEL.md`.

- **There is no timestamp anywhere in what JLR sends.** Not in the status
  payload, not in the STOMP frame headers. The envelope's `t` is delivery
  time — measured to the millisecond against a payload three hours old. The
  odometer is the only ordering key, which is what `_is_older_by_odometer`
  uses.
- **`ENGINE_COOLANT_TEMP` is latched at shutdown**, not live. It is not a
  usable "engine is running" signal and was wrongly used as one; coolant now
  decays on age instead.
- **No car has been observed reporting a running value in
  `VEHICLE_STATE_TYPE`.** Do not infer a running engine from it.
- **`KEY_ON_ENGINE_OFF` is not transient.** One car sat in it for fifteen
  hours. Anything that withholds readings while unsettled has to be
  asymmetric — withhold the reading that claims the car is open or unlocked,
  show the reassuring one — or it hides a dozen correct sensors.
- The vehicle endpoints sit behind Approov attestation and answer 498 without
  it; position and vehicle names come from the owner web portal session
  instead.
- Tokens last about four minutes, which is what drives the socket reconnect
  and resubscribe — and the resubscribe is what makes JLR redeliver its
  retained snapshot.

## Code conventions

- Comments explain **why**, usually by naming the thing that went wrong. If a
  guard looks redundant, the comment should say which incident it is for.
  Match that density; do not add comments that restate the line below.
- Line length 88, black and isort (black profile), ruff with `E F W I UP B SIM
  C4 RET PTH`.
- Binary sensor polarity follows Home Assistant's device classes: for `LOCK`,
  on means unlocked; for `DOOR` and `WINDOW`, on means open. Note that the
  window helper tests `!= "CLOSED"`, not `== "OPEN"`.
- Behaviour that users can see belongs in `docs/` too — `ENTITIES.md` for what
  an entity does, `TROUBLESHOOTING.md` by symptom, `RECIPES.md` for automation
  examples. `quality_scale.yaml` claims should describe what the code actually
  does, not just say `done`.
- Tests live in `tests/` for the pure units and `tests/ha/` for anything
  needing a running Home Assistant. `tests/ha/doubles.py` holds the shared
  fakes; widen those rather than hand-rolling a payload per test. A test that
  re-implements the logic it is checking is not a test — drive it end to end.

## Dependencies

Dependabot watches GitHub Actions monthly and the pinned lint/test tooling
weekly. The pins are deliberate: a linter release should arrive as a pull
request that can be read and run, not fail an unrelated commit. Two pins are
explicitly held in `.github/dependabot.yml` with reasons —
`pytest-homeassistant-custom-component` in the minimum lane and `pycares` —
and bumping either breaks the lane on its own assertion.
