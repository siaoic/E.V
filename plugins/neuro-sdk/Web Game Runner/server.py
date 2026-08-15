#!/usr/bin/env python3

from http.server import HTTPServer, SimpleHTTPRequestHandler, test  # type: ignore
from pathlib import Path
import os
import sys
import argparse
import subprocess

class CORSRequestHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/$env"):
            self.__handle_env()
        else:
            super().do_GET()

    def __handle_env(self):
        requested_var = self.path.removeprefix("/$env/")
        if requested_var in os.environ:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(os.environ[requested_var].encode())
        else:
            self.send_response(404)
            self.end_headers()

    def end_headers(self):
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Origin", "*")
        if self.path.endswith(".br"):
            self.send_header("Content-Encoding", "br")
        elif self.path.endswith(".gz"):
            self.send_header("Content-Encoding", "gzip")

        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

def shell_open(url):
    if sys.platform == "win32":
        os.startfile(url)
    else:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.call([opener, url])


def serve(root, port, run_browser):
    os.chdir(root)

    if run_browser:
        # Open the served page in the user's default browser.
        print("Opening the served URL in the default browser (use `--no-browser` or `-n` to disable this).")
        shell_open(f"http://localhost:{port}")

    test(CORSRequestHandler, HTTPServer, port=port)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-p", "--port", help="port to listen on", default=8060, type=int)
    parser.add_argument(
        "-r", "--root", help="path to serve as root (relative to `platform/web/`)", default="../../bin", type=Path
    )
    browser_parser = parser.add_mutually_exclusive_group(required=False)
    browser_parser.add_argument(
        "-n", "--no-browser", help="don't open default web browser automatically", dest="browser", action="store_false"
    )
    parser.set_defaults(browser=True)
    args = parser.parse_args()

    # Change to the directory where the script is located,
    # so that the script can be run from any location.
    os.chdir(Path(__file__).resolve().parent)

    serve(args.root, args.port, args.browser)
