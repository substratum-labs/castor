# Security Policy

## Scope

Castor provides **application-layer control** for AI agents: tool-call validation, budget enforcement, and human-in-the-loop gating. It does not sandbox the host process (filesystem, network). For infrastructure isolation, use a container or [Roche](https://github.com/substratum-labs/roche).

## Reporting a Vulnerability

If you discover a security vulnerability, please report it responsibly.

**Email:** security@substratum-labs.com

Please include:
- Description of the vulnerability
- Steps to reproduce
- Impact assessment

We will acknowledge your report within 48 hours and aim to provide a fix or mitigation within 7 days for critical issues.

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.4.x   | Yes       |
| < 0.4   | No        |
