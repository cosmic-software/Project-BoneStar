#!/usr/bin/env python3
"""Local dev server for testing the WebVerse world -- adds MIME types http.server
doesn't know about (.veml, .glb), which the default server serves as
application/octet-stream, apparently confusing the WebVerse XML/HTTP pipeline.
"""
import http.server
import socketserver

http.server.SimpleHTTPRequestHandler.extensions_map.update({
    ".veml": "application/xml",
    ".glb": "model/gltf-binary",
    ".json": "application/json",
})

PORT = 8000
with socketserver.TCPServer(("", PORT), http.server.SimpleHTTPRequestHandler) as httpd:
    print(f"Serving on port {PORT} with VEML/glb MIME types")
    httpd.serve_forever()
