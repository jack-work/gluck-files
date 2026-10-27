{ host }:
''
  @media_document path /_media/play/*
  header @media_document {
    >Content-Security-Policy "default-src 'none'; script-src ${host}:*/_media/static/; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; media-src 'self' blob:; connect-src 'self'; worker-src 'self' blob:; manifest-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
  }
  @media_app_document path /_media/app/*
  header @media_app_document {
    >Content-Security-Policy "default-src 'none'; script-src ${host}:*/_media/app/ ${host}:*/_media/static/; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; media-src 'self' blob:; connect-src 'self'; worker-src 'self' blob:; manifest-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
  }
''
