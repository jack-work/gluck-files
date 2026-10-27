{ listerAddress, garageAddress, media ? null, bearerClients ? [ ] }:
let
  bearerCaller = ''
    header Remote-Principal human
    header Remote-Client ${builtins.concatStringsSep " " bearerClients}
  '';

  bearerGate =
    if bearerClients == [ ] then ''
      @bearer header Authorization Bearer*
      respond @bearer 403
    '' else ''
      @bearer_refused {
        header Authorization Bearer*
        not {
          ${bearerCaller}
          method GET HEAD
          path_regexp ^/([^_/]|[^_/].*[^/])$
        }
        not {
          ${bearerCaller}
          method GET HEAD
          path /_media/ /_media/app/* /_media/static/* /_media/r/* /_media/api/me /_media/api/me/*
        }
        not {
          ${bearerCaller}
          method POST
          path /_media/api/me /_media/api/me/state
        }
      }
      respond @bearer_refused 403
      request_header -Authorization
    '';

  authorizeAt = uri: ''
    request_header -X-Files-URI
    forward_auth ${listerAddress} {
      uri ${uri}
      copy_headers X-Files-URI
      transport http {
        dial_timeout 1s
        response_header_timeout 3s
      }
    }
  '';

  authorize = authorizeAt "/_files/authz";
  authorizeMedia = authorizeAt "/_files/authz-media";

  garageObject = hostHeader: ''
    @files_bad_uri {
      not header_regexp X-Files-URI "^/([A-Za-z0-9._~-]|%[0-9A-F]{2})+(/([A-Za-z0-9._~-]|%[0-9A-F]{2})+)*$"
    }
    respond @files_bad_uri 502
    @files_bad_segments header_regexp X-Files-URI "(^|/)[.]{1,2}(/|$)|[.](/|$)|%20(/|$)|%25[0-9A-Fa-f]{2}|%(2[Ff]|5[Cc]|0[0-9A-Fa-f]|1[0-9A-Fa-f]|7[Ff])"
    respond @files_bad_segments 502
    rewrite * {http.request.header.X-Files-URI}?
    request_header -X-Files-URI
    reverse_proxy ${garageAddress} {
      ${if hostHeader == null then "" else "header_up Host ${hostHeader}"}
      header_up -Cookie
      header_up -Authorization
      header_up -Remote-User
      header_up -Remote-Groups
      header_up -Remote-Email
      header_up -Remote-Name
      @files_substitution status 301 302 303 305 307 308
      handle_response @files_substitution {
        respond 404
      }
      @files_errors status 4xx 5xx
      handle_response @files_errors {
        copy_response_headers {
          include Content-Range
        }
        respond "" {rp.status_code}
      }
      handle_response {
        copy_response
      }
    }
  '';

  mediaRoutes = ''
    @media_player path /_media/play/*
    handle @media_player {
      rewrite * /player.html
      root * ${media.webRoot}
      file_server
    }
    @media_static path /_media/static/*
    handle @media_static {
      uri strip_prefix /_media/static
      root * ${media.webRoot}/static
      file_server
    }
    @media_rendition path /_media/r/*
    handle @media_rendition {
      route {
        ${authorizeMedia}
        ${garageObject media.bucketHost}
      }
    }
    @media_api path /_media/api/item/* /_media/api/lib/*
    handle @media_api {
      route {
        ${authorizeMedia}
        @media_bad_uri {
          not header_regexp X-Files-URI "^/(([A-Za-z0-9._~-]|%[0-9A-F]{2})+(/([A-Za-z0-9._~-]|%[0-9A-F]{2})+)*/?)?$"
        }
        respond @media_bad_uri 502
        reverse_proxy ${media.apiAddress} {
          header_up -Cookie
          header_up -Authorization
        }
      }
    }
    @media_app path /_media/ /_media/app/* /_media/api/me /_media/api/me/* /_media/api/screens /_media/api/screens/*
    handle @media_app {
      route {
        request_header -X-Files-URI
        @media_screen_cookie header_regexp screen Cookie (?:^|;\s*)aguatv_screen=([A-Za-z0-9_-]{20,64})(?:;|$)
        @media_no_screen_cookie not header_regexp Cookie (?:^|;\s*)aguatv_screen=([A-Za-z0-9_-]{20,64})(?:;|$)
        request_header @media_screen_cookie Cookie "aguatv_screen={re.screen.1}"
        request_header @media_no_screen_cookie -Cookie
        reverse_proxy ${media.apiAddress} {
          header_up -Authorization
        }
      }
    }
    @media_other path /_media /_media/*
    respond @media_other 404
  '';
in
''
  ${bearerGate}
  @files_viewer path /_files/view
  handle @files_viewer {
    reverse_proxy ${listerAddress}
  }
  @files_internal path /_files/*
  respond @files_internal 404
  ${if media == null then "" else mediaRoutes}
  @files_object not path_regexp (/|^)$
  handle @files_object {
    route {
      ${authorize}
      ${garageObject null}
    }
  }
''
