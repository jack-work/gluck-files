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
      @files_bad_segments header_regexp X-Files-URI "(^|/)[.]{1,2}(/|$)|[.](/|$)|%20(/|$)|%25[0-9A-Fa-f]{2}|%(2[Ff]|5[Cc]|0[0-9A-Fa-f]|1[0-9A-Fa-f]|7[Ff])"
      respond @files_bad_segments 502
      rewrite * {http.request.header.X-Files-URI}?
      request_header -X-Files-URI
      reverse_proxy ${garageAddress} {
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
        @files_inline {
          header Content-Type audio/mpeg
          header Content-Type audio/mp4
          header Content-Type audio/aac
          header Content-Type audio/ogg
          header Content-Type audio/wav
          header Content-Type audio/x-wav
          header Content-Type audio/flac
          header Content-Type video/mp4
          header Content-Type video/webm
          header Content-Type video/ogg
          header Content-Type image/jpeg
          header Content-Type image/png
          header Content-Type image/gif
          header Content-Type image/webp
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
