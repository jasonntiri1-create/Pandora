# Action Gateway Architecture

```text
ActionEnvelope
    |
    v
Risk classification
    |
    v
Policy decision ---- deny ----> terminal result
    |
    v
Budget check -------- block ---> terminal result
    |
    v
Approval gate ------ reject ---> terminal result
    |
    v
Registered async handler
    |
    v
Audit callback + event callback
```

The gateway injects risk, policy, budget, approval, audit, event, and handler behavior. The core does not provide privileged device, filesystem, browser, financial, or network adapters in this release.

The default risk model recognizes a small list of low-risk read-only/no-op intent prefixes. High-impact prefixes become high risk. Everything else is medium risk and needs approval. Default approval rejects, which prevents unclassified requests from executing silently.

A successful call means only that the registered handler returned successfully. It is not proof that the handler is safe, that the user was authenticated, or that external side effects are reversible.
