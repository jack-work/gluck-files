---
name: MEDIA
description: Object authorization, same-origin streaming and the media viewer. Read before changing the files Caddy route, the authorizer, the viewer or any response header policy.
---

# Media delivery

Status: deployed 2026-10-14, spain 79fb26b.

| route | enforcement | bytes |
|---|---|---|
| browser directory | Authelia, then lister `list` | lister HTML |
| `/_files/view?uri=` | Authelia, then lister `read` | lister HTML embedding the stable object path |
| browser object, GET or HEAD | Authelia, then `/_files/authz` checks `read` on every request | Caddy proxies Garage web |

The browser never receives a capability URL. Every Range a player sends after a
seek is re-authorised. Revocation stops the next request; bytes already sent are
not recalled. Authelia's cached groups keep their normal refresh interval;
path-grant revocation is read from sqlite on the next request.

What the old design bought and what replaces it: the 302 to a presigned
`s3.kelliher.info` URL gave accidental origin isolation, because bucket bytes
came from a different origin. That isolation is now provided by response policy:
bucket bytes are never a document.

## URI contract

Caddy supplies `X-Forwarded-Uri`, `X-Forwarded-Method`, and authenticated
`Remote-*`. The site declares `trustsRemoteHeaders`; the build checks the strip
and unconditional `forward_auth` in the rendered Caddyfile. The lister trusts
those headers on loopback.

That answers identity provenance. Key binding is separate: the authorizer
decodes the path once, checks the ACL, and returns `X-Files-URI`, escaped with
`quote('/' + key, safe='/')`. Caddy removes the client's copy, requires a
nonempty valid echo from this response, and rewrites the Garage request to it.
Query parameters are discarded. Nothing else selects a key.

| input | rule |
|---|---|
| empty segments, dot segments, trailing slash/dot/space | refuse |
| encoded separators, backslashes, controls, invalid escapes/UTF-8 | refuse |
| an encoding changed by a second decode | refuse |
| spaces, plus, literal percent, hash, question mark, semicolon | decode once, escape in the echo |
| case and Unicode code points | preserve; NFC and NFD remain distinct |
| missing, empty or malformed echo; authorizer error/timeout | refuse; never fall back to the original URI |

Every authorizer response is no-store. It neither looks up nor reads an object.
GET and HEAD make the same decision; Range is not a separate permission.

## Garage is a raw capability

Garage's web endpoint serves the whole website bucket without authentication.
It listens on a unix socket in `/run/kweb-gluck-files-garage`, which kelliher-web
makes 0750, owner garage, group kweb-front, before every Garage start. Garage
makes the socket itself 0222, so the directory is the only control: no process
but Garage and Caddy can reach it. Caddy dials it through the declared link
`/run/kweb/gluck-files-garage.sock` and never without a successful authorizer
response.

Garage 1.3.1 decodes once and preserves case, slashes and Unicode. Website mode
substitutes other keys: a trailing slash selects an index, missing keys can
redirect, and a configured error document replaces the body of any GET 4xx,
including 400 and 416. The canonical echo excludes index requests; Caddy turns
redirects into 404 and strips every 4xx/5xx body, keeping status and
Content-Range, so a 416 still reports the total size.

## Response policy

| response | policy |
|---|---|
| every bucket byte, success, partial, 304, error | `Content-Disposition: attachment`, `private, no-store`, nosniff, CSP `sandbox` with `default-src 'none'` and same-origin media/img |
| viewer and listing HTML | app-owned templates, autoescaped; CSP with `script-src 'none'`, `object-src 'none'`, no sandbox |
| Caddy transport errors | same policy, generated from the same map by `handle_errors` |
| raw HTTP parser rejects (400, 431) | fixed text before routing. Route headers cannot exist there; tests require no request echo and no server version |

The media element loads the object as a same-origin subresource of the trusted
viewer, so its Range requests carry the Lax session cookie. Attachment and the
sandbox directive govern navigation, not subresource loads; both were verified
in Chromium rather than assumed. Unsupported types get the viewer page with a
plain download link. A direct link to any object downloads rather than renders,
deliberately.

A sandboxed media document was tried first. Its opaque origin dropped the
session cookie on later Range requests, which returned 401. Adding
`allow-same-origin` without `allow-scripts` was attempted and NOT established
either way; it is neither proven nor a dead end.

## Edge cache

The zone has default `browser_cache_ttl 14400` and `cache_level aggressive`,
never modified, and no cache rule. Media extensions are cached by default and
the cache key includes the query string. A stable files URL cached at the edge
would be served to a later request that never passed authz, because Cloudflare
answers before Caddy.

Origin `private, no-store` on every object response is therefore load-bearing.
Live gate, 2026-10-14, colo ATL throughout, with a scratch account granted one
object: authorised GET 200 and Range 206 served; the same session on an object
outside its grant 403; the identical URL with no session 401 to Authelia, no
bytes; every response `BYPASS`, never `HIT`. One finding: Cloudflare rewrote the
authorised 206 to `private, max-age=14400` while origin sent `private,
no-store`. The zone's Browser Cache TTL lands on edge-served partial responses.
The edge held nothing, but the authorised user's own browser could keep a slice.
A Cloudflare cache rule for files and s3 (cache off, browser TTL respecting
origin) fixed it: the rerun the same night served the 206 as `private, no-store`,
every response `DYNAMIC`, never `HIT`. Origin headers alone were not enough;
the rule is part of this route.

Two traps, both measured on this zone 2026-10-13:

| trap | so |
|---|---|
| Garage 1.3.1 drops `response-cache-control` on 206 responses | Caddy stamps no-store on partial responses itself. Never ask Garage to |
| Cloudflare fills an ABSENT Cache-Control with `max-age=14400`, and relays a present `private, no-store` | every response path must carry the header, errors included. One path without it is a public edge copy for an unmeasured period |

## Recorded negatives

All ten `cs7641/audio/*.mp3` objects were `audio/mpeg` with byte ranges and the
listed sizes, audited 2026-10-12 with legitimate SigV4 credentials. Metadata is
not a contributing cause.

The old delivery used the S3 API on 3900, not website mode on 3902, so website
error-document substitution could not contribute to it.

Compatibility survey, 2026-10-12: 290 keys, zero literal percent, trailing
dot/space or empty/dot segments, one interior-space key. That exact key is a
regression fixture. Re-run the survey before tightening the predicate.

## Acceptance

Verified in Chromium against Caddy 2.11.2 and Garage 1.3.1 on scratch, using the
real audio bytes at 16 KB/s with the cache disabled: played past 125 seconds,
seeked to about 2156 seconds, and the fresh Range at 127 seconds carried the
cookie and returned 206.
