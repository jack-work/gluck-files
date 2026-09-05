# Why two hostnames, and why one of them has no Authelia

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
| `files.kelliher.info` | web | 3902 | **Authelia only**, and the bearer bypass is explicitly closed. |

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
because this site is a `proxyTo` like any API, so a future author could switch
the bypass on and reopen the hole. The lines make that switch ineffective
rather than merely inadvisable.

Proved, with a real Caddy, a stub Authelia returning 401, and a decoy file:

```
without those two lines, Authorization: Bearer anything-at-all  -> 200 + contents
with them,               Authorization: Bearer anything-at-all  -> 403
with them,               no session, no header                  -> 401 (Authelia)
```

## The bucket boundary does work too

`files` is marked as a website; `graveyard` is not. Garage answers **404** on
the web endpoint for a bucket without website access, whatever `Host` header
the request carries. So the private bucket is unreachable from the browser
plane *by construction*, not by a rule anyone maintains. Forging the Host
header does not reach it.

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
