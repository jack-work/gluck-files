# gluck-files

Object storage on spain, backed by [Garage](https://garagehq.deuxfleurs.fr/)
(S3-compatible), fronted by kelliher-web.

Two hostnames, one daemon:

| | |
|---|---|
| `s3.kelliher.info` | The S3 API. Authenticated by **SigV4**, no Authelia. Uploads, presigning, lifecycle. |
| `files.kelliher.info` | The browser path. Authenticated by **Authelia** + the `files-admin` group. Read-only, links only. |

Why it is shaped this way, and the bug it replaces, is in
[`doc/AUTH.md`](./doc/AUTH.md). Read that before changing anything about auth.

## Buckets

| Bucket | Reachable from | Retention |
|---|---|---|
| `files` | both hostnames | forever, until you delete it |
| `graveyard` | `s3.` only (private, never a website) | `1d/` `7d/` `30d/` prefixes expire on their names |

`files` is not a free choice: Garage's web endpoint resolves the bucket from
the `Host` header, so `files.kelliher.info` **requires** a bucket called
`files`.

The graveyard mirrors `/var/tmp/graveyard` on the same box on purpose. One
vocabulary for expiry across the estate: `7d/` means seven days in the bucket
exactly as it does on the filesystem, and in both cases the *name is the
policy*: a lifecycle rule reads its day count from the same string that names
the prefix, so they cannot drift apart.

Retention is a property of the bucket, not a timer anybody maintains. Every
bucket also aborts incomplete multipart uploads after a day: orphaned parts
belong to no object, appear in no listing, and are noticed only when the disk
fills.

## Credentials

Your key is minted by Garage and lives in hush. It is never in the Nix store,
never in a unit file, never in argv, never in your shell history.

Minting it, once, in one pipeline so the secret is never displayed:

```bash
ssh spain@spain 'sudo garage key create jack-laptop' | hush secret set files
```

Then use it through hush, which puts it in the environment of one process and
nowhere else:

```bash
hush files aws --endpoint-url https://s3.kelliher.info s3 ls s3://files/
```

The bootstrap unit mints a **separate** key for itself to apply lifecycle
rules, so revoking a laptop never disarms retention.

## Uploading

```bash
hush files aws --endpoint-url https://s3.kelliher.info s3 cp report.pdf s3://files/
hush files aws --endpoint-url https://s3.kelliher.info s3 sync ./tree/ s3://files/tree/
```

**Cloudflare caps request bodies at 100 MB** on the free plan, and times out
origins at 100 s. Both are answered by multipart uploads with parts under the
cap, configured *into* the hush command rather than remembered:

```
multipart_threshold = 32MB
multipart_chunksize = 32MB
```

A limit worked around by discipline is a limit that will eventually bite. If
you are moving something genuinely large, skip the tunnel entirely:

```bash
ssh -L 3900:127.0.0.1:3900 spain@spain -N &
hush files aws --endpoint-url http://127.0.0.1:3900 s3 cp big.iso s3://files/
```

## Sharing a file with someone

Presign it. The link carries its own expiry, so the grant ends on a schedule
instead of living forever in a chat history:

```bash
hush files aws --endpoint-url https://s3.kelliher.info \
  s3 presign s3://files/report.pdf --expires-in 86400
```

SigV4 caps presigned URLs at 7 days. For something you want gone regardless of
who kept the link, put it in the graveyard instead. The object expires even if
the URL does not:

```bash
hush files aws --endpoint-url https://s3.kelliher.info s3 cp draft.pdf s3://graveyard/7d/
```

## Browsing

`files.kelliher.info/<path>` serves an object, behind Authelia.

**There is no directory listing.** Garage's web endpoint serves an index
document or 404; it has no autoindex, unlike the `file_server browse` this
service used to run. Links are the interface. If you want an index at a prefix,
put one there: an `index.html` uploaded to a prefix is served for `/`, which
is the supported way to get browsing back and costs nothing to add later.

## Operating

```bash
ssh spain@spain 'sudo garage status'                  # node, layout, capacity
ssh spain@spain 'sudo garage bucket info files'       # size, object count, keys
ssh spain@spain 'journalctl -u garage -f'
ssh spain@spain 'journalctl -u gluck-files-bootstrap' # layout/bucket/lifecycle setup
```

The bootstrap unit is idempotent and guards on observed state: `bucket create`
and `layout apply` both exit non-zero once their work is done, so it checks
before acting rather than re-running blindly.

## Durability, stated plainly

One node, one NVMe, `replication_factor = 1`. This buys **availability, not
durability**. No replication factor helps when there is one disk. Treat a
bucket as a convenience copy, not a backup.

## Ports

All loopback. Only Caddy reaches them; RPC never leaves the box.

| 3900 | S3 API | fronted at `s3.kelliher.info` |
| 3901 | RPC | single node, never through the firewall |
| 3902 | web | fronted at `files.kelliher.info` |
| 3903 | admin | never proxied |

## Rollback

`/var/lib/gluck-files` still holds the pre-migration tree, untouched, and
nothing serves it. The old `file_server` shape lives in git history. Reverting
is one commit and a deploy, with the data still on disk because we never
deleted it.

The old Caddy block could not be left in place, tempting as it sounds: it
matched `files.kelliher.info`, which the web endpoint now owns, and two blocks
cannot hold one hostname.

Delete the directory in a deliberate change once the bucket has carried real
traffic, not as a side effect of this one.
