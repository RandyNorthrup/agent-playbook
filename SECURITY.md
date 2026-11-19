# Security policy

## Reporting a vulnerability

Please report security problems privately through GitHub's
[private vulnerability reporting](https://github.com/RandyNorthrup/agent-playbook/security/advisories/new)
rather than a public issue. Include what you found, how to reproduce it, and what it affects. You'll get a
reply within a week, and a fix or a plan once the problem is confirmed.

Supported: the latest commit on `main`.

## How the canvas server is protected

The canvas server (`agent-playbook/playbook/server.py`) is meant for one person on one machine:

- It binds to `127.0.0.1` only and checks the `Host` header, so other machines and DNS-rebinding pages can't
  reach it.
- Every write and every "open folder" request needs a per-run token embedded in the page the server serves,
  so other websites open in the same browser can't drive it.
- It sends headers that refuse framing.
- "Open folder" only shows a folder, or selects a file, that exists on this machine, in the file manager. It
  reads the path as written, with `~` as the home folder and a relative path from the skill folder; network
  paths are refused. It never runs anything: a macOS app bundle is revealed, not launched.
- A request that stalls is dropped after 20 seconds, and every error comes back as JSON.
- Store ids are checked against one rule and can't leave their collection folder.

## What never goes in a playbook

The accounts registry holds handles only. Passwords, recovery codes, tokens and email addresses never go in
the store, and `pb.py check` warns when an account contains an email address. Your own playbook will hold your
accounts and an audit log of everything you've posted: keep it in a private repository or out of git.
