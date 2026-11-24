{ config, lib, pkgs, ... }:
let
  cfg = config.services.gluck-files;
  ub = cfg.userBuckets;
  lifecycle = pkgs.writeText "gluck-files-user-lifecycle.json" (builtins.toJSON {
    Rules = [{
      ID = "abort-incomplete-multipart";
      Status = "Enabled";
      Filter.Prefix = "";
      AbortIncompleteMultipartUpload.DaysAfterInitiation = 1;
    }];
  });
in
{
  options.services.gluck-files.userBuckets = {
    enable = lib.mkEnableOption "a Garage bucket per user, made on first use by a narrow provisioner";
    socket = lib.mkOption {
      type = lib.types.path;
      default = "/run/gluck-files/provision.sock";
      description = "The provisioner's socket: group listerUser, one verb, ensure <user>.";
    };
    keysDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/gluck-files/keys";
      description = "Each user's key, 0400 listerUser, written by the provisioner.";
    };
  };

  config = lib.mkIf (cfg.enable && ub.enable) {
    systemd.tmpfiles.rules = [
      "d ${builtins.dirOf ub.keysDir} 0755 root root -"
      "d ${ub.keysDir} 0700 ${cfg.listerUser} ${cfg.listerUser} -"
    ];

    systemd.sockets.gluck-files-provision = {
      wantedBy = [ "sockets.target" ];
      socketConfig = {
        ListenStream = ub.socket;
        SocketUser = "root";
        SocketGroup = cfg.listerUser;
        SocketMode = "0660";
        Accept = false;
      };
    };

    systemd.services.gluck-files-provision = {
      description = "makes a user's own Garage bucket and key, and nothing else";
      after = [ "gluck-files-bootstrap.service" ];
      requires = [ "gluck-files-bootstrap.service" ];
      environment = {
        PROVISION_GARAGE = "${pkgs.garage_1_x}/bin/garage -c /etc/garage.toml";
        PROVISION_AWS = "${pkgs.awscli2}/bin/aws";
        PROVISION_S3 = "http://127.0.0.1:${toString cfg.ports.s3}";
        PROVISION_REGION = cfg.region;
        PROVISION_ACL_DB = cfg.aclDatabase;
        PROVISION_KEYS_DIR = ub.keysDir;
        PROVISION_LIFECYCLE = "${lifecycle}";
        PROVISION_PEER_USER = cfg.listerUser;
      };
      serviceConfig = {
        ExecStart = "${pkgs.python3}/bin/python3 ${../lister/provision.py}";
        User = "root";
        ProtectSystem = "strict";
        ReadWritePaths = [ ub.keysDir (builtins.dirOf cfg.aclDatabase) ];
        PrivateTmp = true;
        NoNewPrivileges = true;
        ProtectHome = true;
        RestrictAddressFamilies = [ "AF_UNIX" "AF_INET" "AF_INET6" ];
        UMask = "0077";
      };
    };
  };
}
