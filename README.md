# Pandora

**A governed AI action-dispatch core.**

Pandora explores a personal AI systems architecture built around persistent context, task orchestration, tool routing, policy checks, approvals, and auditability.

## Public release scope

This repository is a **curated, history-free public release of the action-dispatch core**, not the complete private Pandora runtime. The original implementation and its historic development state are preserved separately in a private archive.

Included here:
- Typed action envelopes and result/status models
- Asynchronous intent routing through registered handlers
- Risk and policy adapters, budget checks, human-approval hooks, and audit/event callbacks
- Fail-closed defaults for high-risk and unclassified actions
- Unit tests for approval denial, policy denial, budget blocking, handler failures, and gateway bypass protection

Intentionally excluded from this public snapshot are private runtime state, credentials, deployment secrets, system startup/persistence agents, credential/browser-data extraction, unrestricted remote execution, cybersecurity exploitation, device/drone control adapters, biometric processing, financial trading integrations, and other high-impact integrations pending separate review.

This is an experimental engineering component, **not a production-ready autonomous agent or an end-user Pandora installer**. The default in-memory adapters are examples; deployments must supply audited policy, authorization, budget, approval, and persistence implementations. Do not connect real devices, accounts, or financial systems to this reference component.

## Quick start

Requires Python 3.11 or newer.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest
```

The action gateway uses the Python standard library. Development requirements are only for linting, tests, and dependency auditing.

## Safety model

The default risk classifier allows only explicitly recognized read-only/no-op prefixes as low risk. High-risk intents are classified as high, and unclassified intents are treated as medium risk and require explicit approval. Without an approval adapter, approval is rejected. Caller-controlled context flags cannot override the approval gate.

This is defense-in-depth, not a security guarantee: code that registers handlers remains trusted code and must be reviewed.

## Engineering checks

CI runs unit tests, selected Ruff checks, a Git-history secret scan, and dependency auditing. The public repository starts from a single clean root commit; its history does not include the private implementation's earlier commits.

## License

No open-source license is included in this release. Public visibility alone does not grant permission to reuse, redistribute, or make derivative works; please contact the author about licensing.
