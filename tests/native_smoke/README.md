# Actual native runtime smoke checks

These POSIX scripts exercise a compiled package in disposable state. They use a loopback synthetic Responses provider and MCP fixture, not a paid model. Install the UI dependency with `python3 -m pip install -r tests/native_smoke/requirements.txt`.

Each invocation requires a new `--work` directory and writes `report.json`. A pass requires exit code zero and `passed: true`; failure or missing evidence blocks release. Keep terminal text/raw captures for review.

```sh
python3 tests/native_smoke/resource_capacity.py --package /absolute/package --work /new/capacity-proof
python3 tests/native_smoke/helper.py --binary /absolute/package/bin/codex --work /new/helper-proof
python3 tests/native_smoke/question.py --package /absolute/package --work /new/question-proof
python3 tests/native_smoke/update_route.py --package /absolute/package --work /new/route-proof
python3 tests/native_smoke/update_prompt.py --package /absolute/package --work /new/prompt-proof
python3 tests/native_smoke/update_package.py --package /absolute/package --work /new/package-proof
```

- Capacity: actual CLI starts with a soft descriptor limit of 256; its visible status provider must open 700 files while preserving the inherited hard limit. A hard limit below 1024 cannot run this proof.
- Helper: real CLI to core to companion execution returns exactly `42`.
- Question: actual terminal choices and custom input share one question; keyboard input returns the custom value and native metadata acknowledgement.
- Route: real CLI calls its bundled manager; corrupt ownership metadata fails without invoking Brew.
- Prompt: a synthetic newer converter release with the same upstream version is offered and dismissed by release identity. The informational banner may remain; the modal must not recur.
- Package: real compiled CLI/helper/rg are updated in a scratch installation and rolled back. The sequence increment is synthetic. Candidate bytes and project configuration stay unchanged.

The package must contain its bundled current compatible-release descriptor, updater resources, CLI, matching companion and ripgrep. Update probes replace remote feed configuration only inside disposable package copies with local fixtures. Helper and question probes disable startup update checks in disposable configuration. The configured input package remains unchanged; these probes do not verify a published remote feed. These smokes complement native source tests; they do not replace the single/multiple/no-option/cancel/decline regression cases.

The scripts were exercised against the verified macOS arm64 0.153.4 custom runtime. `portable-baseline.json` records that evidence. A later upstream version requires its own run; baseline results do not establish later compatibility.
