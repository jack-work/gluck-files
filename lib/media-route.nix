{ listerAddress, garageAddress }:
''
  @bearer header Authorization Bearer*
  respond @bearer 403
  @files_internal path /_files/*
  respond @files_internal 404
  @files_object not path_regexp (/|^)$
  handle @files_object {
    route {
      request_header -X-Files-URI
      forward_auth ${listerAddress} {
        uri /_files/authz
        copy_headers X-Files-URI
        transport http {
          dial_timeout 1s
          response_header_timeout 3s
        }
      }
      @files_bad_uri {
        not header_regexp X-Files-URI "^/([A-Za-z0-9._~-]|%[0-9A-F]{2})+(/([A-Za-z0-9._~-]|%[0-9A-F]{2})+)*$"
      }
      respond @files_bad_uri 502
      @files_bad_segments header_regexp X-Files-URI "(^|/)[.]{1,2}(/|$)|[.](/|$)|%20(/|$)|%(25|2[Ff]|5[Cc]|0[0-9A-Fa-f]|1[0-9A-Fa-f]|7[Ff])"
      respond @files_bad_segments 502
      rewrite * {http.request.header.X-Files-URI}
      request_header -X-Files-URI
      reverse_proxy ${garageAddress} {
        header_up -Cookie
        header_up -Authorization
        header_up -Remote-User
        header_up -Remote-Groups
        header_up -Remote-Email
        header_up -Remote-Name
        @files_substitution status 301 302 303 307 308 404
        handle_response @files_substitution {
          respond 404
        }
        @files_inline {
          header Content-Type audio/mpeg audio/mp4 audio/aac audio/ogg audio/wav audio/x-wav audio/flac video/mp4 video/webm video/ogg image/jpeg image/png image/gif image/webp
        }
        handle_response @files_inline {
          header >Content-Disposition inline
          copy_response
        }
        handle_response {
          header >Content-Disposition attachment
          copy_response
        }
      }
    }
  }
''
