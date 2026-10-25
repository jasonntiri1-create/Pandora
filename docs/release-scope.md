# Public Release Scope

## Included
This initial public release contains the asynchronous ActionGateway, its action/result data types, and unit tests for the gateway's policy and approval behavior.

## Excluded
The release excludes the full private runtime and its private history, environment files, identity and world-model data, local operational logs, installer and auto-start code, persistence agents, remote command execution, credential/browser-data extraction, exploitation modules, physical device/drone adapters, biometric pipelines, and trading integrations.

## Known limitations
- The gateway is a policy-routing component, not an operating-system sandbox.
- Registered handler functions can have arbitrary effects and must be reviewed.
- The default budget adapter is permissive; production deployments need an enforceable budget policy.
- Default audit and event callbacks log but do not provide tamper-resistant persistence.
- The approval callback is fail-closed when not configured; a real approval service must independently authenticate the approver.
- This release has no end-user installer and does not claim production readiness.

## Validation
CI verifies only the source in this curated snapshot. It does not establish safety, correctness, or production readiness of the separate private runtime.
