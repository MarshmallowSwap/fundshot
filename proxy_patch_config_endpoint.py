import sys, os, re
from pathlib import Path

DO_POST_CODE = '''
    def do_POST(self):
        if self.path == "/api/config":
            self._handle_config_update()
            return
        self.send_error(404, "Not Found")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _handle_config_update(self):
        import json, os, signal
        CONFIG_PATH = os.path.expanduser("~/.funding_king_config.json")
        try:
            length  = int(self.headers.get("Content-Length", 0))
            raw     = self.rfile.read(length)
            payload = json.loads(raw)
        except Exception as e:
            self._json_response(400, {"ok": False, "error": str(e)}); return
        try:
            with open(CONFIG_PATH) as f:
                existing = json.load(f)
        except Exception:
            existing = {}
        existing.update(payload)
        existing["_updated_from_dashboard"] = True
        try:
            with open(CONFIG_PATH, "w") as f:
                json.dump(existing, f, indent=2)
        except Exception as e:
            self._json_response(500, {"ok": False, "error": str(e)}); return
        reloaded = False
        try:
            import subprocess
            subprocess.Popen(["systemctl", "reload-or-restart", "funding-king-bot"])
            reloaded = True
        except Exception:
            pass
        self._json_response(200, {"ok": True, "config_path": CONFIG_PATH, "reloaded": reloaded})

    def _json_response(self, code, data):
        import json
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
'''

def patch_proxy(proxy_path):
    path = Path(proxy_path)
    original = path.read_text()
    if "_handle_config_update" in original:
        print("Patch già applicata."); return
    match = re.search(r'class\s+\w+\(.*BaseHTTPRequestHandler.*\):', original)
    if not match:
        print("❌ Classe BaseHTTPRequestHandler non trovata."); return
    class_line_end = original.index('\n', match.end()) + 1
    patched = original[:class_line_end] + DO_POST_CODE + '\n' + original[class_line_end:]
    path.with_suffix('.py.bak').write_text(original)
    path.write_text(patched)
    print(f"✅ Patch applicata! Backup in {path.with_suffix('.py.bak')}")

patch_proxy(sys.argv[1])
