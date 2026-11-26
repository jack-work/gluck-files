''
  @thumb_tile path /_thumb/*
  header @thumb_tile {
    defer
    Cache-Control "{http.response.header.X-Thumb-Cache}"
    Cache-Control "^$" "private, no-store"
    -X-Thumb-Cache
  }
''
