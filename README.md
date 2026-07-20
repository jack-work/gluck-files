# gluck-files

Static file host at **files.kelliher.info**, gated by Authelia 2FA + the
`gluck-files-admin` lldap group.

No backend process. Caddy's `file_server` serves a directory managed by
the [`kelliher-web`](https://github.com/jack-work/kelliher-web) platform
storage abstraction; auth happens at the platform edge before any byte
of a file leaves the box.

## What you get

- `https://files.kelliher.info/` → directory index (browsable) of
  `/var/lib/gluck-files` on the host.
- `https://files.kelliher.info/<any-relative-path>` → that file.
- Any request without a valid Authelia session (or without membership
  in the `gluck-files-admin` lldap group) is bounced to the portal for
  password + TOTP.

## Uploading files

The service does not expose a write API. Upload out-of-band, straight
into the volume:

```bash
# Single file
scp report.pdf spain:/var/lib/gluck-files/

# A whole tree
rsync -avh --delete ./release-artifacts/ spain:/var/lib/gluck-files/release-artifacts/
```

Files land visible immediately — Caddy reads the live path, not a
Nix-store snapshot. `Cache-Control: no-store` is set so browsers won't
cache stale copies while you're iterating.

Ownership on the mount point is `caddy:caddy` (see below). If you `scp`
as your login user, you'll get your uid — fine, since the tree is
world-readable (`0755` + `umask 022` by default). If you'd rather keep
everything owned by caddy:

```bash
ssh spain "sudo chown -R caddy:caddy /var/lib/gluck-files"
```

## Auth flow

```
browser ──► https://files.kelliher.info/foo.pdf
   │
   └─► Cloudflare tunnel ─► Caddy (:8780)
                              │
                              ├─► forward_auth → Authelia
                              │     │
                              │     ├─ has session? gluck-files-admin? ✓ → allow
                              │     └─ otherwise → 302 to portal (password + TOTP)
                              │
                              └─► file_server /var/lib/gluck-files/foo.pdf
```

Only members of the `gluck-files-admin` group in lldap can read
anything. The group is bootstrapped automatically by the platform's
identity layer from `requiredGroups`.

## Storage

The volume is declared through the platform's storage contract:

```nix
services.kelliher-web.storage.volumes.gluck-files = {
  mountPoint = "/var/lib/gluck-files";
  owner = "caddy";
  group = "caddy";
  mode = "0755";
  quota = "50G";
  recordsize = "1M";      # tuned for large-file streaming (zfs only)
  snapshotProfile = "media";
};
```

- On `services.kelliher-web.storage.backend = "plain"` (default) this
  is just a directory under `/var/lib/`.
- On `services.kelliher-web.storage.backend = "zfs"` it becomes a
  dedicated dataset with hard `refquota`, `zstd` compression,
  `recordsize=1M`, and sanoid retention per the `media` profile.

The interface downstream (this module) sees is identical either way.
Flip the backend at the platform level to migrate.

## Deploying

This flake exposes `nixosModules.default`. Consume it from the host's
system flake alongside `kelliher-web`:

```nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    kelliher-web.url = "github:jack-work/kelliher-web";
    gluck-files.url  = "github:jack-work/gluck-files";
  };

  outputs = { self, nixpkgs, kelliher-web, gluck-files, ... }: {
    nixosConfigurations.spain = nixpkgs.lib.nixosSystem {
      modules = [
        kelliher-web.nixosModules.default
        gluck-files.nixosModules.default
        {
          services.kelliher-web = {
            enable = true;
            baseDomains = [ "kelliher.info" ];
            # …tunnelTokenFile, storage backend, etc.
          };
          services.gluck-files.enable = true;
        }
      ];
    };
  };
}
```

Then, on spain:

```bash
sudo nixos-rebuild switch --flake .
```

## Options

| Option | Type | Default | Notes |
|---|---|---|---|
| `services.gluck-files.enable` | bool | `false` | Turn it all on. |

That's the whole surface. There is no port to configure — nothing
listens. There's no auth to configure — the platform does it. There's
no path to configure — it's always `/var/lib/gluck-files`.
