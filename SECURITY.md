# Security policy

## Sensitive data

Do not attach any of the following to a public issue:

- packet captures;
- Wopet account credentials or password-derived values;
- TUTK device UID, device username, or device password;
- app option exports, Home Assistant backups, or unredacted logs;
- public IP addresses or personally identifying pet/account metadata.

The bridge is designed not to print credentials. Network addresses may still
appear in debug output, so review logs before sharing them.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting feature for this repository. Do
not open a public issue for credential exposure, authentication bypasses, or
unsafe camera controls.

## Scope

Only test devices and accounts you own or are explicitly authorized to test.
Feeding is a physical action. The bridge sends each feed request exactly once
and never retries it; automations that press Feed should guard against repeats.
The P2P responder answers only the configured camera IP and UID.

