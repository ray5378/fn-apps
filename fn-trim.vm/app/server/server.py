#!/usr/bin/env python3
import argparse
import json
import logging
import mimetypes
import os
import signal
import subprocess
import sys
import threading
from http import HTTPStatus
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn


class ThreadedServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


WWW_DIR = ""
LOG = logging.getLogger("fn-trim.vm")
TARGET_FILE = "/var/apps/trim.vm/target/static/index.html"
PATCH_MARKER = "<!-- fn-trim.vm-patch -->"
PATCH_ANCHOR = "</title>"
PATCH_STYLE = '''    <!-- fn-trim.vm-patch -->
    <style>
      @media all {
        #root button {
          display: inline-flex !important;
        }
        #root [class*="hidden"] {
          display: block !important;
        }
      }
    </style>'''


def run_cmd(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return -1, "", str(e)


def get_display_name(name):
    rc, out, _ = run_cmd(["virsh", "dumpxml", name])
    if rc != 0:
        return name
    for line in out.split("\n"):
        line = line.strip()
        if line.startswith("<title>") and line.endswith("</title>"):
            title = line[7:-8]
            if title:
                return title
    return name


def list_vms():
    rc, out, _ = run_cmd(["virsh", "list", "--all", "--name"])
    if rc != 0:
        return []
    names = [n.strip() for n in out.split("\n") if n.strip()]
    vms = []
    for name in names:
        rc2, info, _ = run_cmd(["virsh", "dominfo", name])
        if rc2 != 0:
            continue
        state = ""
        uuid = ""
        maxmem = "0"
        vcpu = "0"
        for line in info.split("\n"):
            line = line.strip()
            if line.startswith("State:"):
                v = line.split(":", 1)[1].strip()
                if v == "running":
                    state = "running"
                elif v == "shut off":
                    state = "shut_off"
                elif v == "paused":
                    state = "paused"
                else:
                    state = "unknown"
            elif line.startswith("UUID:"):
                uuid = line.split(":", 1)[1].strip()
            elif line.startswith("Max memory:"):
                maxmem = line.split(":", 1)[1].strip().split()[0]
            elif line.startswith("CPU(s):"):
                vcpu = line.split(":", 1)[1].strip()
        display_name = get_display_name(name)
        vms.append({
            "name": name,
            "displayName": display_name,
            "state": state, "uuid": uuid,
            "vcpu": int(vcpu) if vcpu.isdigit() else 0,
            "memory": int(maxmem) if maxmem.isdigit() else 0
        })
    return vms


def vm_action(name, action):
    rc, out, err = run_cmd(["virsh", action, name])
    return rc == 0, out or err


def get_patch_status():
    if not os.path.isfile(TARGET_FILE):
        return False
    with open(TARGET_FILE, "r", errors="replace") as f:
        return PATCH_MARKER in f.read()


def apply_patch(enable):
    if enable:
        if get_patch_status():
            return True, "already patched"
        style = PATCH_STYLE + "\n"
        tmp = TARGET_FILE + ".tmp"
        found = False
        with open(TARGET_FILE, "r", errors="replace") as f:
            content = f.read()
        idx = content.find(PATCH_ANCHOR)
        if idx == -1:
            return False, f"anchor '{PATCH_ANCHOR}' not found"
        idx += len(PATCH_ANCHOR)
        content = content[:idx] + "\n" + style + content[idx:]
        with open(tmp, "w") as f:
            f.write(content)
        os.replace(tmp, TARGET_FILE)
        return True, "patch applied"
    else:
        if not get_patch_status():
            return True, "not patched"
        with open(TARGET_FILE, "r", errors="replace") as f:
            content = f.read()
        while PATCH_MARKER in content:
            start = content.find(PATCH_MARKER)
            end = content.find("</style>", start)
            if end == -1:
                break
            end += len("</style>")
            content = content[:start] + content[end:]
        tmp = TARGET_FILE + ".tmp"
        with open(tmp, "w") as f:
            f.write(content)
        os.replace(tmp, TARGET_FILE)
        return True, "patch removed"


def send_json(handler, code, data):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


def send_error(handler, code, msg):
    send_json(handler, code, {"error": msg})


def read_json_body(handler):
    length = int(handler.headers.get("Content-Length", 0))
    if length <= 0:
        return None
    data = handler.rfile.read(length)
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None


class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        LOG.info("%s - %s", self.address_string(), format % args)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/vms":
            self.handle_list_vms()
        elif path == "/api/patch":
            self.handle_get_patch()
        elif path.startswith("/api/vm/"):
            self.handle_get_vm()
        else:
            self.serve_static()

    def do_POST(self):
        path = self.path.split("?")[0]
        if path.startswith("/api/vm/"):
            self.handle_vm_action()
        elif path == "/api/patch":
            self.handle_post_patch()
        else:
            send_error(self, HTTPStatus.NOT_FOUND, "not found")

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def handle_list_vms(self):
        try:
            vms = list_vms()
            send_json(self, HTTPStatus.OK, vms)
        except Exception as e:
            send_error(self, HTTPStatus.INTERNAL_SERVER_ERROR, str(e))

    def handle_get_vm(self):
        parts = self.path.split("/")
        if len(parts) < 4:
            send_error(self, HTTPStatus.BAD_REQUEST, "invalid path")
            return
        name = parts[3]
        rc, info, _ = run_cmd(["virsh", "dominfo", name])
        if rc != 0:
            send_error(self, HTTPStatus.NOT_FOUND, f"vm '{name}' not found")
            return
        data = {"name": name}
        for line in info.split("\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                data[k.strip().lower().replace(" ", "_")] = v.strip()
        send_json(self, HTTPStatus.OK, data)

    def handle_vm_action(self):
        parts = self.path.split("/")
        if len(parts) < 5:
            send_error(self, HTTPStatus.BAD_REQUEST, "invalid path")
            return
        name = parts[3]
        action = parts[4]
        valid = ["start", "shutdown", "destroy", "reboot", "reset"]
        if action not in valid:
            send_error(self, HTTPStatus.BAD_REQUEST, f"invalid action '{action}'")
            return
        try:
            ok, msg = vm_action(name, action)
            if ok:
                send_json(self, HTTPStatus.OK, {
                    "success": True, "message": msg,
                    "vm": name, "action": action
                })
            else:
                send_json(self, HTTPStatus.OK, {
                    "success": False, "error": msg,
                    "vm": name, "action": action
                })
        except Exception as e:
            send_error(self, HTTPStatus.INTERNAL_SERVER_ERROR, str(e))

    def handle_get_patch(self):
        try:
            patched = get_patch_status()
            send_json(self, HTTPStatus.OK, {"patched": patched})
        except Exception as e:
            send_error(self, HTTPStatus.INTERNAL_SERVER_ERROR, str(e))

    def handle_post_patch(self):
        body = read_json_body(self)
        if body is None or "enabled" not in body:
            send_error(self, HTTPStatus.BAD_REQUEST, "enabled field required")
            return
        try:
            ok, msg = apply_patch(body["enabled"])
            if ok:
                send_json(self, HTTPStatus.OK, {
                    "success": True, "patched": body["enabled"],
                    "message": msg
                })
            else:
                send_json(self, HTTPStatus.OK, {
                    "success": False, "patched": get_patch_status(),
                    "error": msg
                })
        except Exception as e:
            send_error(self, HTTPStatus.INTERNAL_SERVER_ERROR, str(e))

    def serve_static(self):
        path = self.path.split("?")[0]
        if path == "/" or not path:
            path = "/index.html"
        filepath = os.path.normpath(os.path.join(WWW_DIR, path.lstrip("/")))
        if not filepath.startswith(os.path.normpath(WWW_DIR)):
            send_error(self, HTTPStatus.FORBIDDEN, "forbidden")
            return
        if not os.path.isfile(filepath):
            send_error(self, HTTPStatus.NOT_FOUND, "not found")
            return
        ctype, _ = mimetypes.guess_type(filepath)
        if ctype is None:
            ctype = "application/octet-stream"
        body = open(filepath, "rb").read()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


def main():
    global WWW_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5800)
    ap.add_argument("--www", default="")
    ap.add_argument("--log", default="", help="log file path")
    args = ap.parse_args()
    log_format = "%(asctime)s [%(levelname)s] %(message)s"
    if args.log:
        logging.basicConfig(filename=args.log, level=logging.INFO, format=log_format)
    else:
        logging.basicConfig(level=logging.WARNING, format=log_format)
    WWW_DIR = args.www or os.path.join(os.path.dirname(__file__), "..", "www")
    WWW_DIR = os.path.abspath(WWW_DIR)
    LOG.info("starting on port %d, www=%s", args.port, WWW_DIR)
    server = ThreadedServer(("0.0.0.0", args.port), Handler)

    def sigterm(*_):
        threading.Thread(target=server.shutdown).start()

    signal.signal(signal.SIGTERM, sigterm)
    signal.signal(signal.SIGINT, sigterm)
    server.serve_forever()


if __name__ == "__main__":
    main()
