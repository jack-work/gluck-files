---
name: CLI
description: The `files` hush command, why it ships from this repo, and the checksum flags Garage needs. Read before hand-installing a command.toml or debugging an XAmzContentSHA256Mismatch.
---

# The `files` CLI

A hush command wrapping `aws` against `s3.kelliher.info`.

```sh
nix run github:jack-work/gluck-files#files-cli-install
hush files s3 ls s3://files/
hush files s3 cp big.mp3 s3://files/music/
```

## Why it lives here

It used to live only in `~/.config/hush/commands/files/`, which is per-machine
state that has to be recreated on every box and is versioned nowhere. The repo
already owns the bucket, the lifecycle rules and the lister; the client belongs
with them.

`files-cli-install` writes `command.toml` into the hush command directory.
`secrets.toml` is **not** in the repo and must not be: it holds the access key.

## The checksum flags

awscli 2.23 sends CRC checksums Garage rejects, surfacing as
`XAmzContentSHA256Mismatch` or a checksum error on upload.

```toml
[env]
AWS_REQUEST_CHECKSUM_CALCULATION = "WHEN_REQUIRED"
AWS_RESPONSE_CHECKSUM_VALIDATION = "WHEN_REQUIRED"
```

These live in the command spec's `env` table. They were previously a
per-machine `~/.aws/config` workaround, which is the same disease as the
unversioned `command.toml`.

## Two doors, one bucket

| door | auth | for |
|---|---|---|
| `s3.kelliher.info` | SigV4, no Authelia | this CLI and any S3 client |
| `files.kelliher.info` | Authelia | a browser |

A client that can sign a request cannot follow a login redirect, which is why
the S3 door has `requireAuth = false`. See `AUTH.md`.
