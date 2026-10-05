---
name: ACL
description: Per-path access control on the files bucket — why the gateway exists, where the single enforcement point is, how inheritance and the trailing slash work, and the admin CLI. Read before changing the Caddy route, the ACL table, or anything about who can read what.
---

# Per-path ACLs

`files.kelliher.info` decides read, list and write per path, with inheritance
down a tree. Garage decides nothing about paths, because it cannot.

## Why this exists rather than a Garage grant

`garage bucket allow` takes `--read`, `--write`, `--owner` and a bucket. There is
no prefix, no path, no key pattern. Verified against garage 1.3.1:

```
garage bucket allow [FLAGS] <bucket> --key <key-pattern>
```

So the bucket is the smallest unit of authorization Garage has. "Read-only on one
directory" is not a thing it can be asked for, and the bucket holds 157 objects
including a rental agreement and documents naming individuals. Anything finer has
to be decided in front of it.

### The shape that was tried first, and why it was wrong

A second bucket on a second hostname, `share.kelliher.info`, with its own access
group. It was correct by construction, cheap, and needed no policy engine.

It was also **not what was asked for**, and it is gone. The request was
filesystem ACLs on the store that already exists. A second bucket was the safer
way to hand one person one file; it was a different feature. Substituting a safer
thing for the requested thing, without the requester agreeing, is scope
substitution even when the reasoning is sound — especially when it is sound,
because sound reasoning is what stops anyone from asking.

## The single enforcement point

**Every** request to `files.kelliher.info` goes to the gateway on `9099`.

```
Cloudflare -> Caddy -> strip Remote-* -> forward_auth (Authelia) -> gateway :9099
```

Garage's web endpoint on `3902` is no longer reachable from any hostname.

That matters more than it looks. Before this, Caddy routed trailing-slash
requests to the lister and **everything else straight to Garage**, so no code
saw an object request. An ACL table under that arrangement would have governed
the listing and nothing else: a control that looks real and does nothing.

| request | needs | result |
|---|---|---|
| `/` or any path ending `/` | `list` on the prefix | a listing, or 404 |
| any other path | `read` on the key | **302** to a short-lived presigned URL |
| no `Remote-User` | — | **401** |
| anything denied | — | **404**, never 403 |

404 rather than 403 because 403 confirms a path exists. The bucket's shape is not
an oracle. That is the same rule `kstack` states for unreadable items.

## Identity

`Remote-User`, stamped by Caddy after `forward_auth`. Caddy strips any
client-supplied `Remote-*` first, and that strip is unconditional and emitted
before `forward_auth` in route order.

**A request with no `Remote-User` is refused, not treated as anonymous.** An
absent header means the request did not arrive through the authenticated path, so
the safe reading is "something is wrong", not "nobody".

`bearerBypass` stays **off**. The gateway does not verify JWTs, so switching it
on would put an unauthenticated path in front of the only thing deciding access.
The site also closes it by hand with `respond @bearer 403`, which makes the
switch ineffective rather than merely inadvisable. That is the September 5
incident, and the lines are belt and braces on purpose.

## The model

```
a grant is (subject, path, perm)
subject is user:<name> or group:<name>
a grant on a PREFIX covers everything beneath it
nothing is permitted that was not granted
```

There is **no negative grant**. A deny row that loses a precedence race is worse
than not having one, so deny is the absence of a grant and precedence never
arises.

### The trailing slash is load-bearing

`dad/` and `dad` are different paths. A grant on `dad/` must not cover a sibling
object called `dadtaxes`, and prefix matching on the bare string would do exactly
that. So directories keep their slash, and the test for it is
`test_a_grant_does_not_leak_to_a_sibling_with_a_shared_prefix`.

### Traversal is refused, not resolved

`dad/../wfh/Rental_Agreement.pdf` is a `BadPath`, not a request that gets
normalised and then checked. A request that needs to climb is one somebody built
by hand. Resolving it first and checking second is how a path check gets bypassed.

### Admin is a grant, not a branch

`group:files-admin` holds `read`, `list` and `write` on the root, seeded when the
database is created. There is no `if user is admin` anywhere in the request path,
so the admin case is exercised by exactly the checks everyone else gets.

### A caller sees their own subtrees at the root

Without `list` on `/`, a caller who holds grants beneath it gets a listing of
just those subtrees rather than a 404. It reveals only paths already granted to
them, and it keeps a legitimate user off a dead end.

## Delivery: presign and redirect

The gateway decides access; the presigned URL only delivers bytes.

| | |
|---|---|
| expiry | **120 seconds** by default, not the seven days SigV4 permits |
| signed against | `https://s3.kelliher.info`, so a browser can follow the redirect |
| bytes through python | none |

A presigned URL is a bearer capability until it expires. That is a real cost, and
the mitigation is that it is short and single-purpose rather than that it is
secret.

**Streaming was considered and rejected.** Pushing 60 MB through a Python process
on the house router is work the router should not do, and it walks into
Cloudflare's ~100 s origin limit, which nobody on this estate has measured. The
procedure is filed and unrun at `~/dev/rou2/docs/cloudflare-streaming.md`. An
unmeasured limit is not a design input.

## The admin CLI

Authority is **ssh to spain**, which is already the strongest credential on the
estate. There is no network-facing way to change a grant, no web portal, and no
self-service: the admin brokers every share.

```bash
ssh spain@spain 'sudo gluck-files-acl grant dad dad/ --read --list'
ssh spain@spain 'sudo gluck-files-acl ls dad'
ssh spain@spain 'sudo gluck-files-acl check dad wfh/Rental_Agreement.pdf read'
ssh spain@spain 'sudo gluck-files-acl revoke dad dad/ --list'
ssh spain@spain 'sudo gluck-files-acl drop dad'
```

`check` asks the gateway's own question and exits 0 for allow, 1 for deny, so it
is usable in a script and in an acceptance test.

The alternative was an authenticated admin API on a public hostname. That would
have meant a new authorization surface, a bearer path in front of a service that
does not verify JWTs, and a second way to grant access. One way in is worth more
than convenience here.

## Granting site entry is separate from granting a path

Two different gates, and both are needed:

| gate | what it decides | where |
|---|---|---|
| `site-files-access` | may this account reach the hostname at all | Authelia, one rule per site |
| the ACL table | which paths, once inside | here |

**Order matters.** Until the gateway existed, `site-files-access` meant "reach
the whole bucket". Now it means only "may knock", and the ACL table decides each
path. Granting site access before the gateway is live hands over all 157 objects.

## Write

`write` exists in the table and is not enforced by the gateway, because uploads
do not come through it: they are SigV4 requests to `s3.kelliher.info`, verified
by Garage against keys it minted. The column is there so a future upload path has
somewhere to ask, and so the vocabulary matches what was asked for.

## Limits

`LISTER_MAX_KEYS` caps a listing at 2000 entries and the page says so when it
truncates. A prefix with tens of thousands of objects will be slow and truncated;
paginate the UI rather than raising the cap.
