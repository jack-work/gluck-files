---
name: SHARING
description: Why there is a second website bucket on its own hostname, what it is not, and the one line that must change when zero-permissions-by-default lands. Read before granting anyone outside the household access to anything in Garage.
---

# Sharing something with someone outside the household

`share.kelliher.info` serves the `share` bucket, behind Authelia, exactly as
`files.kelliher.info` serves `files`.

That is the whole mechanism. The reason it exists is the interesting part.

## Why not a permission on the `files` bucket

Two reasons, and the first one is sufficient.

**`files` holds things nobody outside the household should see.** A rental
agreement, a filled contact sheet, and thirteen PDFs naming individuals. That is
recorded in `doc/AUTH.md` because those files were briefly reachable through the
bearer-bypass hole in September. Granting a guest read on `files` puts them one
policy bug away from all of it.

**Garage cannot express a narrower grant.** `garage bucket allow` takes
`--read`, `--write`, `--owner`, and a bucket. There is no prefix, no path, no
key pattern. Verified against garage 1.3.1:

```
garage bucket allow [FLAGS] <bucket> --key <key-pattern>
```

So "read-only on one directory" is not a thing Garage can be asked for. The
bucket is the smallest unit of authorization it has. If the bucket is the unit,
then a separate audience needs a separate bucket.

## What this buys, and what it does not

| | |
|---|---|
| gives | a grant to `share` is not a grant to `files`, by construction rather than by a rule anyone maintains |
| gives | the same argument `doc/AUTH.md` makes for the graveyard: the boundary is Garage's own, not ours |
| does **not** give | per-path ACLs. Everyone who reaches `share.kelliher.info` sees all of `share` |
| does **not** give | inheritance, or file-level grants |

So two guests who should not see each other's files cannot both live in `share`.
Today there is one guest, so this is correct and cheap. The general answer is
below.

## Expiry comes free

The lifecycle rules apply to every bucket, and they read their day counts from
the prefix names, so the retention vocabulary is the same here as in the
graveyard and as in `/var/tmp/graveyard`:

| put it at | it lives |
|---|---|
| `share/dad/thing.pdf` | forever, until deleted |
| `share/7d/thing.pdf` | seven days |
| `share/30d/thing.pdf` | thirty days |

A share that should end on a schedule should be uploaded to a dated prefix
rather than remembered about.

## The one line that must change

This site is declared with `requireAuth = true` and **no group**. Today that
means "any authenticated directory user", because Authelia's access control
carries a single rule over nine hostnames with no `subject`. With only Jack's
two accounts in the directory, that is not an exposure.

**It becomes one the moment a second human exists.** When zero-permissions-by-
default lands, this site must declare its access group:

```nix
services.kelliher-web.sites.gluck-share = {
  siteAccessGroups = [ "site-share-access" ];   # <- add this
  ...
};
```

Until that option exists, do not create a guest account. The ordering is the
whole point: a guest who exists before the gate does reaches every gated
hostname on the estate, including the finance cache and the calendar.

`requiredGroups` is deliberately **not** used for this. It means *capability*,
it is enforced by the app's own handlers via `Remote-Groups`, and
`kelliher-web/modules/caddy.nix` says so in the option's own description. Site
entry and capability are different questions and conflating them locks legitimate
readers out: the calendar permits reading via object ACLs and gates only creation
on `calendar-create`.

## Host to bucket

One lister serves both hostnames. It resolves the bucket from the `Host` header
the same way Garage's web endpoint does, against the same `root_domain`, and
then checks an **allowlist**.

The allowlist is the security property. Without it,
`graveyard.kelliher.info` would name a bucket that is deliberately not a
website, and the browser plane would have a door into the private one. It is
covered by `lister/test_lister.py` and wired into `nix flake check`, including
the case that matters:

```
graveyard.kelliher.info -> None
a.share.kelliher.info   -> None
share.kelliher.info:8780 -> share
```

That Caddy preserves the `Host` header upstream is not an assumption. It is
already proven by the running system: `files.kelliher.info` serves objects
through `reverse_proxy localhost:3902`, and Garage resolves the bucket from
`Host`, so a rewritten `Host` would already 404. Confirmed directly:

```
Host: files.kelliher.info  /wfh/Rental_Agreement.pdf -> 200
Host: localhost            /wfh/Rental_Agreement.pdf -> 404
```

## When to replace this with real ACLs

A second bucket is the right answer for one audience. It stops being the right
answer at the second simultaneous guest, or the first time one guest should see
a subtree and not its sibling.

The general answer is to make the lister the gateway for **all** requests rather
than only directory requests, so there is one place where a policy is checked on
the object path. Today there is none: Caddy routes trailing slashes to the
lister and everything else straight to Garage, so no code sees an object request.

The lister is one route from being that gateway. It already holds a read key for
every website bucket, so the change is Caddy's terminal `reverse_proxy` pointing
at the lister instead of at Garage, plus an ACL table and a decision about how
bytes get delivered.

That decision, when it comes: **presign with a short expiry, not streaming.**
Streaming 60 MB through a Python process on the house router is work the router
should not be doing, and it walks into Cloudflare's ~100 s origin limit, which
nobody here has measured. The procedure is filed and unrun at
`~/dev/rou2/docs/cloudflare-streaming.md`. A presigned URL is a bearer
capability until it expires, which is a real cost, but it is bounded and it is
the posture of every other link on this estate. An unmeasured limit is not a
design input.

## Operating

```bash
hush files s3 cp thing.pdf s3://share/dad/
ssh spain@spain 'sudo garage bucket info share'
```

### Human keys are granted by hand, and the bootstrap will not do it for you

The bootstrap unit grants only the two keys it mints itself: `gluck-files-bootstrap`
(RWO, for lifecycle) and `files-lister` (R). **It does not grant any human key**,
and it should not: hardcoding a person's key name into the module would put
identity in the wrong repo.

So a new bucket starts unreachable from your laptop, and the first upload fails
with a message that reads like a bug:

```
AccessDenied ... CreateMultipartUpload: Forbidden: Operation is not allowed for this key.
```

That is the correct response to an ungranted key. The fix is one deliberate
command:

```bash
ssh spain@spain 'sudo garage bucket allow --read --write share --key <your key name>'
```

**The key `hush files` actually uses is named `cheroot-laptop`, not
`jack-laptop`.** Two keys named `jack-laptop` exist in Garage with no grants on
any bucket; they are orphans. Check before granting, because granting the wrong
one produces the same `AccessDenied` and looks like the grant failed:

```bash
ssh spain@spain 'sudo garage bucket info files'   # read the key that already works
```

This section exists because the first draft of this document asserted that the
bootstrap granted human keys. It does not. The claim survived review and died on
the first upload.
