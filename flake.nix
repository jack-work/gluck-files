{
  description = "gluck-files — object storage on spain, backed by Garage (S3-compatible)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs, ... }:
    let
      nixosModule =
        { config, lib, pkgs, ... }:
        let
          cfg = config.services.gluck-files;

          garagePkg = pkgs.garage_1_x;
          garageCli = "${garagePkg}/bin/garage -c /etc/garage.toml";

          # The retention vocabulary, declared ONCE. These strings name the
          # prefixes in the graveyard bucket AND supply the day counts of the
          # lifecycle rules that expire them, exactly as /var/tmp/graveyard's
          # directory names supply their own tmpfiles ages. One fact, no
          # copies: `7d/` cannot come to mean anything other than seven days.
          windowDays = w: lib.toInt (lib.removeSuffix "d" w);

          lifecycleJson = builtins.toJSON {
            Rules =
              map (w: {
                ID = "expire-${w}";
                Status = "Enabled";
                Filter.Prefix = "${w}/";
                Expiration.Days = windowDays w;
              }) cfg.windows
              ++ [
                # Orphaned multipart parts are the classic silent disk leak:
                # an interrupted upload leaves blocks that belong to no object,
                # are billed by no listing, and are noticed only when the disk
                # fills. Every bucket gets this rule.
                {
                  ID = "abort-incomplete-multipart";
                  Status = "Enabled";
                  Filter.Prefix = "";
                  AbortIncompleteMultipartUpload.DaysAfterInitiation = 1;
                }
              ];
          };

          lifecycleFile = pkgs.writeText "gluck-files-lifecycle.json" lifecycleJson;

          bootstrap = pkgs.writeShellApplication {
            name = "gluck-files-bootstrap";
            runtimeInputs = [
              garagePkg
              pkgs.awscli2
              pkgs.gnugrep
              pkgs.gnused
              pkgs.coreutils
            ];
            text = ''
              # Bring a fresh Garage node to the shape this service expects:
              # a layout, two buckets, website access on exactly one of them,
              # and lifecycle rules. Every step guards on observed state, so
              # running this on an already-configured cluster is a no-op.
              # (`bucket create` and `layout apply` both exit non-zero when the
              # work is already done, so "just re-run it" is not idempotent.)

              # The daemon opens its RPC socket a moment after the unit starts.
              for _ in $(seq 1 60); do
                if ${garageCli} status >/dev/null 2>&1; then break; fi
                sleep 1
              done

              # ── layout ────────────────────────────────────────────────────
              # A node with no role stores nothing: Garage will accept writes
              # and then have nowhere to put them. Assign only when unassigned,
              # since re-applying a layout version is an error.
              if ${garageCli} status | grep -q "NO ROLE ASSIGNED"; then
                node=$(${garageCli} node id -q | cut -d@ -f1)
                ${garageCli} layout assign -z ${cfg.zone} -c ${cfg.capacity} "$node"
                version=$(${garageCli} layout show \
                  | sed -n 's/.*layout apply --version \([0-9]\+\).*/\1/p' | tail -1)
                ${garageCli} layout apply --version "$version"
              fi

              # ── buckets ───────────────────────────────────────────────────
              for bucket in ${cfg.bucket} ${cfg.graveyardBucket}; do
                if ! ${garageCli} bucket list | grep -qw "$bucket"; then
                  ${garageCli} bucket create "$bucket"
                fi
              done

              # ── website access ────────────────────────────────────────────
              # Only the durable bucket is reachable on the browser path. The
              # graveyard is deliberately NOT a website: with website access
              # denied, Garage answers 404 on the web endpoint no matter what
              # Host header a request carries, so the private bucket is
              # unreachable from the browser plane by construction rather than
              # by a rule someone maintains.
              ${garageCli} bucket website --allow -i index.html ${cfg.bucket}

              # ── lifecycle ─────────────────────────────────────────────────
              # Lifecycle is an S3-API operation, not a Garage admin one, so it
              # needs SigV4 credentials. This unit mints its OWN key rather than
              # borrowing the human's: the secret is read into a variable, used,
              # and dropped. It is never an argv (world-readable in /proc), never
              # echoed to the journal, and never handed to a person.
              if ! ${garageCli} key list | grep -qw "${cfg.bootstrapKeyName}"; then
                ${garageCli} key create ${cfg.bootstrapKeyName} >/dev/null
              fi
              ${garageCli} bucket allow --read --write --owner \
                ${cfg.graveyardBucket} --key ${cfg.bootstrapKeyName} >/dev/null
              ${garageCli} bucket allow --read --write --owner \
                ${cfg.bucket} --key ${cfg.bootstrapKeyName} >/dev/null

              key_info=$(${garageCli} key info --show-secret ${cfg.bootstrapKeyName})
              AWS_ACCESS_KEY_ID=$(echo "$key_info" | sed -n 's/^Key ID: *//p')
              AWS_SECRET_ACCESS_KEY=$(echo "$key_info" | sed -n 's/^Secret key: *//p')
              export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
              export AWS_DEFAULT_REGION=${cfg.region}
              export AWS_EC2_METADATA_DISABLED=true

              for bucket in ${cfg.bucket} ${cfg.graveyardBucket}; do
                aws --endpoint-url http://127.0.0.1:${toString cfg.ports.s3} \
                  s3api put-bucket-lifecycle-configuration \
                  --bucket "$bucket" \
                  --lifecycle-configuration "file://${lifecycleFile}"
              done
            '';
          };
        in
        {
          options.services.gluck-files = {
            enable = lib.mkEnableOption "gluck-files — Garage object storage behind kelliher-web";

            rpcSecretFile = lib.mkOption {
              type = lib.types.path;
              description = ''
                Path to Garage's RPC secret (32 bytes, hex). Normally a sops
                secret path owned by the `garage` user.

                A path, not a value: `services.garage.settings` is rendered to
                /etc/garage.toml, which is world-readable, and anything inline
                would also sit in the Nix store forever. Garage refuses to
                start if this file is world-readable, which is the check we
                want rather than one we have to remember.
              '';
            };

            adminTokenFile = lib.mkOption {
              type = lib.types.nullOr lib.types.path;
              default = null;
              description = ''
                Path to the admin API bearer token. Null leaves the admin API
                unauthenticated, which is safe only because it binds loopback;
                set it once anything else on the box can reach that port.
              '';
            };

            bucket = lib.mkOption {
              type = lib.types.str;
              default = "files";
              description = ''
                The durable bucket, and the browser path's whole story.

                NOT free to choose: Garage's web endpoint resolves the bucket
                from the Host header against `s3_web.root_domain`, so serving
                files.kelliher.info REQUIRES a bucket named `files`. Renaming
                this without renaming the subdomain yields 404 on every object.
              '';
            };

            graveyardBucket = lib.mkOption {
              type = lib.types.str;
              default = "graveyard";
              description = ''
                The transient bucket: private, never a website, reached only by
                signed requests. Its `1d/` `7d/` `30d/` prefixes expire on their
                own names, matching /var/tmp/graveyard so the estate has one
                vocabulary for expiry rather than two.
              '';
            };

            windows = lib.mkOption {
              type = lib.types.listOf lib.types.str;
              default = [ "1d" "7d" "30d" ];
              description = ''
                Retention windows. Each name is both a prefix in the graveyard
                bucket and the day count of the lifecycle rule that empties it.
              '';
            };

            zone = lib.mkOption {
              type = lib.types.str;
              default = "spain";
              description = "Layout zone name for this node.";
            };

            capacity = lib.mkOption {
              type = lib.types.str;
              default = "400G";
              description = ''
                Capacity advertised to the layout. One node on one NVMe: this
                buys availability, not durability. No replication factor helps
                when there is one disk, so treat the bucket as a convenience
                copy and not as a backup.
              '';
            };

            region = lib.mkOption {
              type = lib.types.str;
              default = "spain";
              description = "S3 region name clients must sign with.";
            };

            bootstrapKeyName = lib.mkOption {
              type = lib.types.str;
              default = "gluck-files-bootstrap";
              description = ''
                Name of the key the bootstrap unit mints for itself to apply
                lifecycle rules. Distinct from any human's key so that
                revoking a laptop's access never disarms retention.
              '';
            };

            ports = {
              s3 = lib.mkOption {
                type = lib.types.port;
                default = 3900;
                description = "S3 API. Loopback only; Caddy fronts it at s3.<domain>.";
              };
              rpc = lib.mkOption {
                type = lib.types.port;
                default = 3901;
                description = "Cluster RPC. Single node, so loopback only, and never through the firewall.";
              };
              web = lib.mkOption {
                type = lib.types.port;
                default = 3902;
                description = "Static web endpoint. Loopback only; Caddy fronts it at files.<domain>.";
              };
              admin = lib.mkOption {
                type = lib.types.port;
                default = 3903;
                description = "Admin API. Loopback only, never proxied.";
              };
            };
          };

          config = lib.mkIf cfg.enable {
            # A fixed user, against the upstream module's DynamicUser default.
            # Garage's metadata directory holds this node's cluster identity;
            # StateDirectory does survive a dynamic uid, but the estate's object
            # store is the wrong place to depend on that. A stable owner also
            # lets sops hand it secrets by name.
            users.users.garage = {
              isSystemUser = true;
              group = "garage";
            };
            users.groups.garage = { };

            services.garage = {
              enable = true;
              package = garagePkg;
              settings = {
                # /var/lib/garage/{meta,data}: the module's default, and a
                # StateDirectory it will create and chown for us.
                replication_factor = 1;
                db_engine = "lmdb";

                rpc_bind_addr = "127.0.0.1:${toString cfg.ports.rpc}";
                rpc_public_addr = "127.0.0.1:${toString cfg.ports.rpc}";
                rpc_secret_file = cfg.rpcSecretFile;

                s3_api = {
                  s3_region = cfg.region;
                  api_bind_addr = "127.0.0.1:${toString cfg.ports.s3}";
                  # Vhost-style addressing lives under a dedicated suffix so
                  # that `s3.<domain>` itself stays path-style: a request for
                  # /files/x.pdf must name a bucket, not a sub-bucket of `s3`.
                  root_domain = ".s3.${lib.head config.services.kelliher-web.baseDomains}";
                };

                s3_web = {
                  bind_addr = "127.0.0.1:${toString cfg.ports.web}";
                  # The browser path's bucket router: Host `files.<domain>`
                  # minus this suffix is the bucket name.
                  root_domain = ".${lib.head config.services.kelliher-web.baseDomains}";
                  index = "index.html";
                };

                admin = {
                  api_bind_addr = "127.0.0.1:${toString cfg.ports.admin}";
                } // lib.optionalAttrs (cfg.adminTokenFile != null) {
                  admin_token_file = cfg.adminTokenFile;
                };
              };
            };

            systemd.services.garage.serviceConfig = {
              DynamicUser = false;
              User = "garage";
              Group = "garage";
            };

            systemd.services.gluck-files-bootstrap = {
              description = "Bring Garage to the layout, buckets and lifecycle gluck-files expects";
              after = [ "garage.service" ];
              requires = [ "garage.service" ];
              wantedBy = [ "multi-user.target" ];
              serviceConfig = {
                Type = "oneshot";
                RemainAfterExit = true;
                User = "garage";
                Group = "garage";
                ExecStart = lib.getExe bootstrap;
              };
            };

            # ── the browser path ──────────────────────────────────────────
            # Authelia gates it, exactly as before. What changed is what sits
            # behind: Garage's web endpoint serves only buckets explicitly
            # marked as websites, and serves an index document or 404 — there
            # is no directory listing, so links are the interface here.
            #
            # THE BEARER BYPASS MUST DIE ON THIS HOSTNAME. `requireAuth` emits
            # the house snippet `@no_bearer not header Authorization Bearer*`,
            # so any request carrying a bearer-shaped header skips forward_auth
            # entirely. That idiom is sound only when the backend verifies the
            # JWT itself. Garage's WEB endpoint verifies nothing, by design —
            # serving a website bucket to unsigned requests is its entire job,
            # and Authelia is the only gate in front of it. Without the two
            # lines below, `curl -H 'Authorization: Bearer x'` would read this
            # bucket exactly as it reads the file_server today, and the
            # migration would carry the hole it exists to close from a
            # directory into an object store. Signed access is not turned away
            # from the estate, it is sent to the door built for it: s3.<domain>.
            services.kelliher-web.sites.gluck-files = {
              subdomains = [ "files" ];
              requireAuth = true;
              requiredGroups = [ "files-admin" ];
              proxyTo = cfg.ports.web;
              extraConfig = ''
                @bearer header Authorization Bearer*
                respond @bearer 403
                header Cache-Control "no-store"
              '';
            };

            # ── the S3 path ───────────────────────────────────────────────
            # requireAuth = false, deliberately, and this is the point of the
            # whole exercise rather than an oversight. See doc/AUTH.md: the
            # house bearer bypass exists so an API client's JWT can reach a
            # backend that verifies it, and the old gluck-files had no backend
            # at all, so the bypass fronted a file_server that verified
            # nothing. Garage verifies SigV4 on every request against keys it
            # minted itself. forward_auth here would add no security and would
            # break every S3 client, since a client that can sign a request
            # cannot follow a login redirect.
            services.kelliher-web.sites.gluck-s3 = {
              subdomains = [ "s3" ];
              requireAuth = false;
              proxyTo = cfg.ports.s3;
            };

            # The pre-Garage tree, kept as it was. Nothing serves it now; it is
            # the rollback copy, and it costs 3.7 MB. Delete it in a deliberate
            # change once the bucket has carried real traffic, not before.
            services.kelliher-web.storage.volumes.gluck-files = {
              mountPoint = "/var/lib/gluck-files";
              owner = "caddy";
              group = "caddy";
              mode = "0755";
              quota = "50G";
              recordsize = "1M";
              snapshotProfile = "media";
            };

            users.users.caddy = {
              isSystemUser = true;
              group = "caddy";
              home = "/var/lib/gluck-files";
              createHome = false;
            };
            users.groups.caddy = { };

            assertions = [
              {
                assertion = config.services.kelliher-web.baseDomains != [ ];
                message =
                  "gluck-files: Garage's web endpoint resolves buckets from the Host header "
                  + "against a root_domain, so kelliher-web.baseDomains must be non-empty.";
              }
            ];
          };
        };
    in
    {
      nixosModules.default = nixosModule;
      nixosModules.gluck-files = nixosModule;
    };
}
