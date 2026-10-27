---
name: AUTH
description: Why gluck-files is split across two hostnames, why s3.kelliher.info has no Authelia, and the September 5 bearer bypass hole that shaped both. Read before changing a site block or adding a service to this platform. The browser-side request path described here was superseded by the ACL gateway and then the per-request object authorizer; see doc/ACL.md and doc/MEDIA.md.
---

# Why two hostnames, and why one of them has no Authelia

> **Superseded in part.** Since 2026-10-04 every request to
> `files.kelliher.info` is decided by the ACL gateway, now on its Caddy-only
> socket, and since 2026-10-14 objects reach the browser only through Caddy after
> `/_files/authz` approves that exact request; Garage's web endpoint listens on a
> socket only Caddy can reach. `doc/ACL.md` and `doc/MEDIA.md` are authoritative
> for the request path. The reasoning below about the bearer bypass, the hostname
> split and the bucket boundary is still why this service is shaped as it is, and
> the September 5 incident it records has not changed.
>
> Proxying Garage's web endpoint without the authorizer in front would delete
> per-path access control silently: every holder of `site-files-access` would
> regain every object, and `gluck-files-acl check` would still pass, because it
> interrogates the ACL database rather than the live request path. The media
> stack test is the check that would fail.

This is the reasoning behind the auth shape of `gluck-files`. It is written
down because the mistake it corrects was invisible for months, and because
the same mistake is available to the next service on this platform.

## The bug this service exists to fix

`files.kelliher.info` used to be Caddy's `file_server` over
`/var/lib/gluck-files`, gated by Authelia. The site block carried the house
bearer bypass, which every authenticated site on kelliher-web gets:

```
@no_bearer not header Authorization Bearer*
forward_auth @no_bearer 127.0.0.1:9091 { … }
```

Read it carefully. `forward_auth` runs **only** for requests matching
`@no_bearer`. A request carrying an `Authorization: Bearer …` header does not
get authenticated. It gets *forwarded*, on the understanding that the backend
will verify the JWT itself. That understanding is written down in the platform
module, and for herald, calendar and kfin it is true: they check the token
against Authelia's JWKS before doing anything.

`gluck-files` had **no backend**. Nothing ran behind the `file_server`. So no
code anywhere in the request path ever looked at that JWT, and the bypass was
not a bypass to a stricter check. It was a hole:

```
curl -H 'Authorization: Bearer anything-at-all' https://files.kelliher.info/wfh/Rental_Agreement.pdf
```

The header does not have to be valid. It does not have to be a JWT. It has to
be *shaped like* a bearer token. Behind it sat a rental agreement, a filled
contact sheet, and thirteen PDFs naming individuals.

The lesson is not "someone forgot a check". It is that **an auth idiom whose
correctness depends on a promise the backend makes cannot be safely applied to
a site that has no backend to make it.**

## Why Garage fixes it in kind rather than in degree

We could have kept the file server and removed the bypass. That fixes this
site, once, until someone re-adds the idiom by copying a neighbouring block.

Garage changes the category of the guarantee. It verifies AWS SigV4 on every
request against keys it minted itself. The signature covers the method, the
path, the headers and a timestamp, so a request cannot be replayed, edited, or
guessed. There is no header you can *shape* your way past, because there is no
credential-shaped-thing being trusted. There is a signature being checked.

The bypass stops being dangerous on the S3 hostname not because we removed it
(we did remove it) but because the thing behind it finally does real work.

## The split

Two consumers exist and they cannot use each other's method. A browser cannot
sign SigV4. An S3 client cannot follow an Authelia login redirect. Forcing both
through one hostname breaks one of them.

| Hostname | Garage endpoint | Port | Auth |
|---|---|---|---|
| `s3.kelliher.info` | S3 API | 3900 | **SigV4 only.** No Authelia, deliberately. |
| `files.kelliher.info` | web, on `/run/kweb-gluck-files-garage/sock`, reached only by Caddy after `/_files/authz` | socket | **Authelia**, or a token verified at the edge for a listed client (see "Verified bearer tokens"); the unverified bearer bypass stays closed. Then the gateway decides per path, per request |

### `s3.kelliher.info`: `requireAuth = false` is the correct setting

This looks alarming in a diff and is not. `forward_auth` here would add no
security, since every request is already verified by the backend, and would break
every S3 client, since a client that can sign a request cannot follow a login
redirect. The site is not unauthenticated; it is authenticated by something
better than a session cookie.

Verified against garage 1.3.1:

```
unsigned GET /files/hello.txt                     -> 403
GET with 'Authorization: Bearer anything-at-all'  -> 400
presigned GET                                     -> 200
```

### `files.kelliher.info`: Authelia, with the bypass nailed shut

Garage's **web** endpoint is the static-website endpoint. It verifies nothing,
by design: serving a website bucket to unsigned requests is its entire job.
Authelia is the only gate in front of it.

**Since 2026-10-14 Caddy reaches that endpoint only after the gateway's
`/_files/authz` approves the exact request, and rewrites it to the canonical key
the gateway returns.** The paragraphs below still apply unchanged, because the
bypass must stay closed whatever sits behind the proxy: the gateway does not
verify JWTs either, so a bearer-shaped header would reach the only thing
deciding access without being authenticated.

So this site must **not** inherit the bearer bypass, or we would have rebuilt
the original bug with a bucket where the file server used to be. The platform
now defaults `bearerBypass` to false, and this site never opts in. The block
also closes the bypass by hand, ahead of the proxy in route order:

```
@bearer header Authorization Bearer*
respond @bearer 403
```

Signed callers are not turned away from the estate. They are sent to the door
built for them, `s3.kelliher.info`.

Those two lines are belt and braces rather than duplication. The platform
assertion that refuses `bearerBypass` on a static tree cannot fire here,
because this site proxies a backend like any API, so a future author could switch
the bypass on and reopen the hole. The lines make that switch ineffective
rather than merely inadvisable.

Proved, with a real Caddy, a stub Authelia returning 401, and a decoy file:

```
without those two lines, Authorization: Bearer anything-at-all  -> 200 + contents
with them,               Authorization: Bearer anything-at-all  -> 403
with them,               no session, no header                  -> 401 (Authelia)
```

## Verified bearer tokens, since 2026-10-27

The bypass above trusted a header nobody checked. What replaced it on the
platform is the opposite: with `trustsRemoteHeadersOrBearer`, kelliher-web sends
a request whose Authorization is exactly `Bearer <token>` to `kweb-bearer`,
which verifies the token against Authelia, admits only the listed clients, takes
the name and groups from userinfo, and stamps `Remote-Principal` and
`Remote-Client` after stripping any a client sent. A refused token never
reaches this site's routes.

`services.gluck-files.bearerClients` opts files in, one client at a time (today
`aguatv-tv`, the Fire TV, signed in by device code). Empty, the default, keeps
the hand-written `respond @bearer 403` exactly as above. Not empty,
`lib/media-route.nix` replaces it with a narrow allow, still ahead of every
route:

| caller | may |
|---|---|
| `Remote-Principal: human`, `Remote-Client` listed | GET and HEAD of an object (not a listing, nothing under `/_`) |
| the same | GET and HEAD of `/_media/`, `/_media/app/*`, `/_media/static/*`, `/_media/r/*`, `/_media/api/me[/*]` |
| the same | POST of exactly `/_media/api/me` and `/_media/api/me/state` |
| anything else with `Authorization: Bearer` | 403 |

Then Authorization is stripped, so the lister, Garage and juglar never see the
token. The lister ACL decides objects for the token's person exactly as for a
session; on this branch Authelia's access_control for the domain is not
consulted, so the ACL is the only gate, and it denies by default.
`test_media_stack.py` tests 21 to 23 hold all of this, including a site with no
bearer clients.

## The bucket boundary does work too

`files` is marked as a website; `graveyard` is not. Garage answers **404** on
the web endpoint for a bucket without website access, whatever `Host` header
the request carries. So the private bucket is unreachable from the browser
plane *by construction*, not by a rule anyone maintains. Forging the Host
header does not reach it.

**This control is still true and no longer the first line.** Browser traffic
reaches the web endpoint only after the gateway approves a key in the `files`
bucket, so the gateway is what keeps `graveyard` out of reach. The bucket flag
remains a second line of defence should the authorizer ever be bypassed.

## The generalisation, for whoever adds the next service

`requireAuth = true` currently means:

> Authelia gates this site, **unless** the caller sends a bearer-shaped header,
> in which case your backend had better be verifying it.

That second clause is invisible at the call site. Nothing in `requireAuth =
true` tells you that your backend just inherited an authentication obligation.

If you are adding a site: either your backend verifies JWTs against Authelia's
JWKS (see `gluck_calendar.py`'s `bearer_to_remote_headers`), or your site block
closes the bypass the way this one does. There is no third option that is
merely "gated by Authelia", however much the config looks like it.
