---
name: MEDIA
description: Object authorization and same-origin streaming. Read before changing the files Caddy route or the authorizer.
---

# Media delivery

Status: implementation under review, not deployed. Acceptance requires a real
browser to play beyond 120 seconds and seek through `files.kelliher.info`.

| route | enforcement | bytes |
|---|---|---|
| browser directory | Authelia, then lister `list` | lister HTML |
| browser object, GET or HEAD | Authelia, then `/_files/authz` checks `read` on every request | Caddy proxies Garage web |
| direct legacy lister object | lister `read` | 120-second presigned redirect |

The browser keeps the files URL. No capability URL expires beneath a player.
Legacy presigns remain bearer capabilities until expiry, including after an ACL
revocation. Existing in-flight transfers also complete; revocation stops the next
request, not bytes already authorized. Authelia's cached groups retain its normal
refresh interval. Path-grant revocation is read from sqlite on the next request.

## URI contract

Caddy supplies `X-Forwarded-Uri`, `X-Forwarded-Method`, and authenticated
`Remote-*`. The lister trusts those headers on loopback; loopback callers are
trusted, not independently authenticated. Public callers cannot supply identity:
Caddy removes their `Remote-*` before Authelia stamps it.

The authorizer decodes the path once, checks the same ACL engine, and returns
`X-Files-URI`, escaped with `quote('/' + key, safe='/')`. Caddy removes the
client's copy, requires a nonempty valid echo from this response, and rewrites the
Garage request to it. Query parameters are discarded. No later handler selects
a different key.

| input | rule |
|---|---|
| empty segments, dot segments, trailing slash/dot/space | refuse |
| encoded separators, backslashes, controls, invalid escapes/UTF-8 | refuse |
| an encoding changed by a second decode | refuse |
| spaces inside names, plus, literal percent, hash, question mark, semicolon | decode once, escape in the echo |
| case and Unicode code points | preserve; NFC and NFD remain distinct |
| missing, empty or malformed echo; authorizer error/timeout | refuse; never use the original URI as fallback |

The endpoint returns 200 with an empty body and canonical echo, 401 for missing
identity, or 403 for refusal. Every response is no-store. It neither looks up nor
reads an object. GET and HEAD make the same decision; Range is not a separate
permission.

## Garage is a raw capability

`127.0.0.1:3902` serves the whole website bucket without authentication. The
boundary is: Caddy never reaches it without a successful authorizer response.

Garage 1.3.1 decodes once and preserves case, slashes and Unicode. Website
substitutions are unsafe: trailing slash selects an index, missing keys can
redirect, and configured error documents replace bodies on any GET 4xx,
including 400 and 416. The canonical echo excludes index requests. Caddy
suppresses redirects and every 4xx/5xx body, retaining error status and native
Content-Range where available. Tests configure a private index and error document
on real Garage and prove neither body crosses the proxy.

## Browser origin

The old S3 redirect provided accidental origin isolation. Streaming makes bucket
bytes same-origin with the file listing. Replacement controls apply before the
first auth hop, including errors: private/no-store, nosniff, and sandbox CSP with
no scripts, forms, base URL or framing.

Only the enumerated audio/video MIME types and JPEG/PNG/GIF/WebP display inline.
Everything else, including HTML, SVG and PDF, is an attachment. Upstream
Content-Disposition and cache/CSP headers cannot loosen this policy. Python
never carries object bytes. The public S3 hostname remains unchanged; removing
it would break signed clients and is a separate decision.

## Recorded negative

Content-Type audited 2026-10-12 with legitimate SigV4 credentials over an SSH
tunnel: all ten `cs7641/audio/*.mp3` objects were `audio/mpeg`, supported byte
ranges, and matched listed sizes. Metadata is not a contributing cause. Public
HEAD 403 responses were not evidence about stored metadata.

The old failing delivery used the signed S3 API on 3900, not website mode on
3902. Website error-document substitution could not contribute to that observed
presign-expiry failure; its 400 XML response was independent of website config.

Compatibility survey, 2026-10-12: 290 existing keys, zero literal percent,
trailing dot/space or empty/dot segments; one interior-space key. The exact key
`cs7641/quizzes/sl-quiz/SL Unit Quiz_ CS7641_ Machine Learning - Fall 2026.pdf`
is a regression fixture. Re-run the survey before tightening the predicate.

Headers are generated from one response policy for both normal and Caddy error
handlers. HTTP-parser rejections before routing have only fixed server errors,
never bucket bytes; they cannot inherit route headers.
