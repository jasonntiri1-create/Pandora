# Security Policy

Pandora's public action-gateway core is experimental. Do not use it to authorize real-world consequential actions without a separately reviewed implementation of authentication, policy evaluation, approval, auditing, and recovery.

## Safe use
- Test with synthetic data and no production credentials.
- Treat all handler implementations and policy adapters as trusted, security-critical code.
- Do not assume that a context flag, risk label, or unit test alone establishes authorization.
- Do not connect this component directly to real devices, vehicles, financial accounts, browser profiles, or public network services.
- Keep credentials and runtime state outside source control.

## Reporting a vulnerability
Avoid publishing secrets, personal data, or exploit details in a public issue. Contact the repository owner through their GitHub profile to coordinate a private report.
