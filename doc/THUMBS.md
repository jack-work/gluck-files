---
name: THUMBS
description: The small JPEG copies the files explorer shows as tiles, where they live, who makes them (upload hook, backfill), how /_thumb serves them, and what the process is allowed to do. Read before changing thumbs.py, thumbrender.py, thumbsctl.py or the /_thumb route.
---

# Thumbnails

The explorer lists a folder and shows `/_thumb/users/<u>/<rel>?v=<etag>` for
each photo or video. The lister never sees the original for a tile.

## Where a thumb lives

`.thumbs/<key>.jpg` in the same bucket as the original, `<key>` being the
object's key inside that bucket. In a per-user bucket that is
`.thumbs/camera/20240512_101010.jpg.jpg` for `camera/20240512_101010.jpg`; in
the shared bucket it is `.thumbs/users/admin/camera/...`. The lister finds
them with one listing of `.thumbs/<folder>/` and takes `?v=` from the thumb
object's own ETag, so a tile's URL changes exactly when its picture does.

Each thumb carries `src-etag` and `src-size` as object metadata: the etag and
size of the source it was made from. That pair, not a clock, decides whether
a thumb is current.

A photo that cannot be decoded gets a zero-byte object with the same metadata
plus `failed`. The lister ignores zero-byte thumbs (the tile stays a glyph),
`/_thumb` answers pending for them, and the backfill leaves them alone until
the source changes or `--retry-failed` is given. Timeouts and killed children
leave no marker: those are retried next pass.

## What a thumb is

320 px on the long edge, never enlarged, JPEG quality 70, EXIF orientation
applied, a non-sRGB profile converted to sRGB, then every metadata block
dropped (EXIF with GPS, ICC, XMP, comments). Video is the frame at 1 s, or the
first frame when the clip is shorter. JPEG, PNG, WebP, GIF and HEIC/HEIF are
photos; mp4, mov, m4v and 3gp are videos. This is the lister's own `kind_of`,
and a test keeps the two in step.

## Who makes them

- **Upload hook.** `upload.py` hands the key to one background thread when an
  upload completes and answers at once. The thread runs at nice 19 with idle
  I/O priority, the queue holds 512 keys, and a full queue or a failed job
  loses only the thumb. `UPLOAD_THUMBS=0` turns it off. If Pillow (or ffmpeg,
  for video) is missing the hook queues nothing.
- **Backfill.** `thumbsctl.py backfill --user NAME [--prefix camera/]`, or
  `--all` with `FILES_USER_KEYS` set. Environment as the upload service:
  `UPLOAD_ENDPOINT`, `UPLOAD_BUCKET`, `UPLOAD_REGION`, `FILES_USER_KEYS`, plus
  the upload key in `AWS_*`. Idempotent, resumable (`--limit N` then run
  again), newest first, one worker. It prints counts (made, current, marked,
  failed, seen, elapsed_s, cpu_s, hottest_c) on stdout and progress on stderr.
  `--dry-run` writes nothing; `--timings FILE` records wall and CPU seconds per
  file.

Both call `thumbs.ensure`. Decoding happens in a child process
(`thumbrender.py`, or ffmpeg for a poster) with a clean environment (no AWS
variables), a CPU limit and an address-space limit. A hostile file can crash
the child and nothing else.

ffmpeg is forced to the mov demuxer and a protocol whitelist: by default it
picks a demuxer by content, and an ffconcat or HLS script named `.mp4` reads
files and URLs of its choosing. Videos are read by range from a presigned
loopback URL, not downloaded.

## What the backfill will not do

- Connect to anything but loopback: it refuses a non-loopback endpoint and
  raises on any non-loopback `connect`.
- Take more than a share of a core: `--cpu-percent` (default 50) sleeps after
  each file in proportion to the CPU it used.
- Run hot: `--pause-above-c` (default 80) waits while a CPU sensor is above
  that, and reports the hottest reading it saw.
- Compete for the disk or the CPU: nice 19 and idle I/O class, set by the
  process itself. A unit should still set `CPUQuota=`, `MemoryMax=`,
  `IPAddressDeny=any` with `IPAddressAllow=localhost`, and `Nice=19`.

## GET /_thumb/users/<owner>/<rel>

Same origin, authenticated by the edge. Allowed when the ACL grants `read` on
`users/<owner>/<rel>` (under `users/` that is the owner, and any user with an
explicit row on that home, never a group or the root grant) and the bearer
client's prefix scope, if any, covers it.

| case | answer |
| --- | --- |
| allowed, thumb present | 200 `image/jpeg`, `Cache-Control: private, max-age=31536000, immutable`, ETag, `nosniff` |
| allowed, no thumb, marker, or not a JPEG | 404, `X-Thumb: pending`, `no-store` |
| anything else | 404, plain, `no-store`, no `X-Thumb` |

"Anything else" is not the owner or a named user, no such user, outside `users/`, a hidden or
malformed path, an unknown host. All of them are the same bytes and are sent
before the bucket is touched, so the route says nothing about which users or
originals exist. No Remote-User is 401.

A page of tiles arrives as a burst. `/_thumb` is shed under overload only at
eight times the queue depth that sheds a listing.

Caddy: `lib/media-route.nix` sends `/_thumb/*` to the lister. The site-wide
response policy (`Cache-Control: private, no-store`) is deferred and would
overwrite the lister's header, so `lib/thumb-policy.nix`, placed before it in
`beforeAuthConfig` like the CSP overrides, copies the lister's `X-Thumb-Cache`
into `Cache-Control` and removes it. A response with no `X-Thumb-Cache`
(a 401 from the edge, a 502 when the lister is down) falls back to
`private, no-store`. The lister sets `X-Thumb-Cache` equal to what it wants
cached: the immutable value on 200 and 304, `no-store` on every 404.
