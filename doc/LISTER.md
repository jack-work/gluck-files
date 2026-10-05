---
name: LISTER
description: How files.kelliher.info gets a directory listing on top of Garage, which has none, and how the bucket is resolved from the Host header. Read before changing the lister's key or reaching for a generated index.html. Access control itself lives in doc/ACL.md.
---

# The lister

Garage's web endpoint serves an index document or a 404. `garage bucket
website` takes `--index-document` and `--error-document` and nothing else, so
there is **no directory listing**. That capability was lost when files moved off
Caddy's `file_server browse`.

## Why not a generated index.html

It is accurate when written and wrong the next time anything is uploaded, and
the failure is silent: a page that omits a new object or links to a deleted one,
with no error anywhere. Listing at request time cannot drift.

## There is no longer a split

This document used to describe Caddy routing trailing-slash requests to the
lister and everything else straight to Garage. **That split is gone.** Every
request to `files.kelliher.info` now goes to the gateway, because a policy
checked only on directory requests would govern the listing and nothing else.

See [`ACL.md`](./ACL.md) for what the gateway decides and how. What remains here
is listing mechanics and the bucket's own key.

Consequences worth keeping:

- Directory listings are computed at request time, so they cannot drift. A
  generated `index.html` is accurate when written and wrong the next time
  anything is uploaded, and the failure is silent.
- No object bytes pass through python. The gateway redirects to a short-lived
  presigned URL and Garage serves the data.

## Auth

Behind Authelia exactly as the rest of the site, `requireAuth = true`.

**`bearerBypass` stays off.** The lister does not verify JWTs, so switching the
bypass on would put an unauthenticated path in front of it. That is the
September 5 incident. The site's `respond @bearer 403` makes it ineffective
rather than merely inadvisable.

## Which bucket

The gateway serves every website hostname. It resolves the bucket from the `Host`
header exactly as Garage's web endpoint does, against the same `root_domain`,
then checks `LISTER_BUCKETS` as an **allowlist**.

The allowlist is the security property, not the resolution. Without it,
`graveyard.kelliher.info` names a bucket that is deliberately not a website, and
the browser plane acquires a door into the private one. Covered by
`lister/test_lister.py`, wired into `nix flake check`, with a negative control
run: deleting the allowlist check makes the graveyard case fail.

That Caddy passes the original `Host` upstream is proven rather than assumed. It
was demonstrated directly against the running system, back when this hostname
proxied to Garage's web endpoint and Garage resolved its own bucket from `Host`:

```
Host: files.kelliher.info  /wfh/Rental_Agreement.pdf -> 200
Host: localhost            /wfh/Rental_Agreement.pdf -> 404
```

The route now terminates at the gateway instead, but the `Host` behaviour it
relies on is the same and was measured, not inferred.

| `Host` | bucket |
|---|---|
| `files.kelliher.info` | `files` |
| `files.kelliher.info:8780` | `files` |
| `graveyard.kelliher.info` | none, 404 |
| `a.files.kelliher.info` | none, 404 |

An unresolvable host gets **404**, not 403: the same no-existence-leak rule
`kstack` states for unreadable items.

## The key

The bootstrap mints `files-lister` itself, the same way it already mints its own
key for lifecycle rules. No human, no sops entry, no secret in a transcript.

| property | value |
|---|---|
| grant | `--read` on `files` |
| graveyard | never granted |
| file | `/var/lib/gluck-files-lister/credentials`, mode 0440 |
| owner | `garage`, group `gluck-files-lister` |

The shared group is why neither process needs root: bootstrap runs as `garage`,
which is a supplementary member of the lister's group, so it can `chgrp` the
file it just wrote. The lister reads it as an `EnvironmentFile`.

Written through `mktemp` and `mv`, so a reader never sees a half-written file.

## Limits

`LISTER_MAX_KEYS` caps a page at 2000 entries and the page says so when it
truncates. Garage pages at 1000 per call; the lister follows continuation
tokens up to the cap.

A prefix with tens of thousands of objects will be slow and truncated. If that
ever happens, paginate the UI rather than raising the cap.
