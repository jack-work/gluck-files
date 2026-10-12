''
  @files_document path /_files/view */
  header @files_document {
    >Content-Security-Policy "default-src 'none'; script-src 'none'; object-src 'none'; media-src 'self'; style-src 'unsafe-inline'; font-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
  }
''
