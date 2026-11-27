"""Минимальный etcd v3 HTTP-шлюз для тестов: /v3/kv/range, /v3/kv/put, /v3/kv/txn."""
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeEtcd:
    def __init__(self):
        self.data: dict[str, tuple[str, int]] = {}
        self.rev = 1
        self.lock = threading.Lock()
        etcd = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                res = etcd.handle(self.path, body)
                out = json.dumps(res).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def set(self, key: str, value: str):
        with self.lock:
            self.rev += 1
            self.data[key] = (value, self.rev)

    def get(self, key: str):
        return self.data.get(key, (None, 0))[0]

    def handle(self, path, body):
        d = lambda s: base64.b64decode(s).decode()
        e = lambda s: base64.b64encode(s.encode()).decode()
        with self.lock:
            if path == "/v3/kv/range":
                k = d(body["key"])
                if k in self.data:
                    v, r = self.data[k]
                    return {"kvs": [{"key": e(k), "value": e(v), "mod_revision": str(r)}]}
                return {}
            if path == "/v3/kv/put":
                self.rev += 1
                self.data[d(body["key"])] = (d(body["value"]), self.rev)
                return {}
            if path == "/v3/kv/txn":
                c = body["compare"][0]
                k = d(c["key"])
                cur = self.data.get(k, (None, 0))[1]
                if cur == int(c["mod_revision"]):
                    put = body["success"][0]["request_put"]
                    self.rev += 1
                    self.data[d(put["key"])] = (d(put["value"]), self.rev)
                    return {"succeeded": True}
                return {}
        return {}

    def close(self):
        self.server.shutdown()
