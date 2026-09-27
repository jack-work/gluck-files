---
name: LISTER
description: How files.kelliher.info gets a directory listing on top of Garage, which has none. Read before changing the Caddy split, the lister's key, or reaching for a generated index.html.
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

## The split

Caddy routes on the trailing slash, inside the site's existing `route` block.

| request | handled by |
|---|---|
| `/` or any path ending `/` | lister, port 9099 |
| everything else | Garage web, port 3902 |

```
@dir path_regexp dir (/|^)$
handle @dir {
  reverse_proxy localhost:9099
}
```

`extraConfig` is emitted **before** the terminal `reverse_proxy`, so a
non-matching request falls through to Garage untouched.

Consequences worth keeping:

- No bytes pass through the lister. Large objects stream from Garage.
- File URLs stay `files.kelliher.info/<key>`, unchanged.
- The lister never proxies, never redirects to storage, holds no object data.

## Auth

Behind Authelia exactly as the rest of the site, `requireAuth = true`.

**`bearerBypass` stays off.** The lister does not verify JWTs, so switching the
bypass on would put an unauthenticated path in front of it. That is the
September 5 incident. The site's `respond @bearer 403` makes it ineffective
rather than merely inadvisable.

## Which bucket

One lister serves every website hostname. It resolves the bucket from the `Host`
header exactly as Garage's web endpoint does, against the same `root_domain`,
then checks `LISTER_BUCKETS` as an **allowlist**.

The allowlist is the security property, not the resolution. Without it,
`graveyard.kelliher.info` names a bucket that is deliberately not a website, and
the browser plane acquires a door into the private one. Covered by
`lister/test_lister.py`, wired into `nix flake check`, with a negative control
run: deleting the allowlist check makes the graveyard case fail.

That Caddy passes the original `Host` upstream is proven rather than assumed, by
the running system: `files.kelliher.info` already serves objects through
`reverse_proxy localhost:3902`, and Garage resolves its bucket from `Host`.

| `Host` | bucket |
|---|---|
| `files.kelliher.info` | `files` |
| `share.kelliher.info` | `share` |
| `share.kelliher.info:8780` | `share` |
| `graveyard.kelliher.info` | none, 404 |
| `a.share.kelliher.info` | none, 404 |

An unresolvable host gets **404**, not 403: the same no-existence-leak rule
`kstack` states for unreadable items.

## The key

The bootstrap mints `files-lister` itself, the same way it already mints its own
key for lifecycle rules. No human, no sops entry, no secret in a transcript.

| property | value |
|---|---|
| grant | `--read` on every website bucket (`files`, `share`) |
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
