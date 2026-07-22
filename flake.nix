{
  description = "gluck-files — static file host at files.kelliher.info (Caddy file_server + Authelia)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs, ... }:
    let
      nixosModule =
        { config, lib, pkgs, ... }:
        let
          cfg = config.services.gluck-files;
        in
        {
          options.services.gluck-files = {
            enable = lib.mkEnableOption
              "gluck-files — static file host at files.<baseDomain>";

            # No `port` option: this service does not run a backend
            # process. The platform's Caddy serves files directly from
            # the storage volume's mount point via the site's `rootPath`.
          };

          config = lib.mkIf cfg.enable {
            # Platform-managed storage volume. On the `plain` backend
            # this is just a directory; flip the platform backend to
            # `zfs` and it becomes a dedicated dataset with quota,
            # compression, and sanoid snapshots — no changes here.
            #
            # Owned by `caddy`: the platform's kelliher-web-caddy unit
            # runs under `DynamicUser = true`, and the daemon logs in
            # as user `caddy`. Owning the tree by caddy:caddy is the
            # simplest way to give it read access; uploads happen
            # out-of-band (see README).
            services.kelliher-web.storage.volumes.gluck-files = {
              mountPoint = "/var/lib/gluck-files";
              owner = "caddy";
              group = "caddy";
              mode = "0755";        # world-readable so Caddy can traverse
              quota = "50G";
              recordsize = "1M";    # tuned for large-file streaming (zfs only)
              snapshotProfile = "media";
            };

            # Ensure the caddy user/group actually exist on the host so
            # the storage ensurer's chown succeeds. The platform's
            # kelliher-web-caddy unit uses DynamicUser and doesn't
            # declare these itself.
            users.users.caddy = {
              isSystemUser = true;
              group = "caddy";
              home = "/var/lib/gluck-files";
              createHome = false;
            };
            users.groups.caddy = { };

            # Public site: gated by Authelia + membership in the
            # `files-admin` lldap group (the platform bootstraps
            # the group from `requiredGroups`). `rootPath` (mutable
            # filesystem dir) rather than `root` (Nix store package)
            # because the whole point is that files change at runtime.
            services.kelliher-web.sites.gluck-files = {
              subdomains = [ "files" ];
              requireAuth = true;
              requiredGroups = [ "files-admin" ];
              rootPath = "/var/lib/gluck-files";
              extraConfig = ''
                file_server browse
                header Cache-Control "no-store"
              '';
            };
          };
        };
    in
    {
      nixosModules.default = nixosModule;
    };
}
