# ctlphone

ctlphone is a Linux-side Android automation toolkit built on ADB. It provides a single-owner Unix-socket gateway, CLI commands, MCP tools, UI inspection, bounded/redacted app-log capture, optional pattern unlock with hidden input, and declarative test macros.

AI coding agents should read [`AGENTS.md`](AGENTS.md) first. Module boundaries, tests, and FoundF integration are described in [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md).

## Public snapshot boundary

This repository intentionally excludes every real-broker login/action component, including the broker service, broker clients, order approval, secure credential injection, and all China Merchants Securities or Eastmoney login/action code. The generic gateway still supports an inter-process device-claim boundary, but contains no broker UI logic. The snapshot also contains no device serial, password, account identifier, credential, screenshot, UI dump, or local runtime state.

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

Connect an authorized Android device and optionally pin its serial locally:

```bash
export PHONE_SERIAL=your-adb-device-serial
python -m phone_ctl.cli gateway start
python -m phone_ctl.cli gateway doctor
```

## CLI examples

```bash
python -m phone_ctl.cli screenshot shot.png --scale 0.5
python -m phone_ctl.cli dump
python -m phone_ctl.cli tap 500 1200
python -m phone_ctl.cli tap-text "Button text"
python -m phone_ctl.cli swipe 500 1600 500 600
python -m phone_ctl.cli text hello123
python -m phone_ctl.cli key BACK
python -m phone_ctl.cli launch com.example.app
python -m phone_ctl.cli current
```

The gateway grants one project a renewable lease and returns `DEVICE_BUSY` to competing callers. Audits omit operation parameters. App-log collection filters by package and redacts common credentials and identifiers.

Pattern unlock is only for a device whose owner has explicitly authorized it. The CLI reads the pattern through hidden input, does not accept it as an argument, does not store it, and makes one attempt only.

## Explicit exclusion

No broker login or trading workflow is present in this snapshot. Do not treat this general device-automation project as authorization to access accounts or submit transactions.
