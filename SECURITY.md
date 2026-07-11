# Security Policy

Nawa Edge is designed for sovereign, local, air-gapped evaluation. Please help keep it that way.

## Supported versions

| Version | Supported |
| --- | --- |
| 1.x | Yes |

## Reporting a vulnerability

Please do **not** open a public GitHub issue for security vulnerabilities.
Email **info@nawacore.ai** with:

- affected version or commit
- operating system and Python version
- minimal reproduction steps
- whether the issue could cause data exposure, code execution, incorrect seal verification, or unsafe output

We aim to acknowledge within 3 business days and provide a remediation plan or clarification as quickly as practical.

## Data handling

Do not send real plant, SCADA, historian, customer, personal, or regulated data in public issues or pull requests. Use synthetic or anonymized CSV samples only.

## Security design constraints

- No outbound network calls.
- Dashboard binds to `127.0.0.1` only.
- No third-party dependencies in `nawa_edge.py`.
- No telemetry, accounts, cloud services, or hidden update mechanisms.
