import http.server
import os
import socketserver


PORT = int(os.environ.get("PORT", "8080"))


class AppHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def do_GET(self):
        path = self.translate_path(self.path.split("?", 1)[0])
        if self.path != "/" and not os.path.exists(path):
            self.path = "/index.html"
        return super().do_GET()


with socketserver.TCPServer(("0.0.0.0", PORT), AppHandler) as server:
    print(f"Сейчас запущен на порту {PORT}")
    server.serve_forever()
