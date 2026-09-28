import argparse
import logging
import sys

from werkzeug.serving import make_server

from .app import create_app


def main():
    parser = argparse.ArgumentParser(description="OAK FFC TEST: local OAK-FFC 4P testing interface")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address; use 0.0.0.0 only on a trusted LAN")
    parser.add_argument("--port", default=8080, type=int)
    parser.add_argument("--capture-dir", default="captures", help="Directory for images and metadata")
    parser.add_argument("--device-id", help="DepthAI MXID when more than one device is connected")
    parser.add_argument("--demo", action="store_true", help="Use clearly labeled synthetic cameras")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    browser_host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(args.host, args.host)
    if ":" in browser_host:
        browser_host = f"[{browser_host}]"
    app = None

    def dispatch(environ, start_response):
        return app(environ, start_response)

    # Bind before creating the camera worker, and announce success only after
    # the port is reserved. No separate port probe or check-then-bind race.
    try:
        server = make_server(args.host, args.port, dispatch, threaded=True)
    except SystemExit:
        print(
            f"\nOAK FFC TEST did not start. If another copy is already running, open\n"
            f"  http://{browser_host}:{args.port}\n"
            "To restart it, press Ctrl+C in its original terminal first.\n"
            "For a different port, run: bash scripts/run.sh --port 8081",
            file=sys.stderr,
        )
        raise

    try:
        app = create_app(capture_dir=args.capture_dir, demo=args.demo, device_id=args.device_id)
        print(f"OAK FFC TEST: http://{browser_host}:{server.port}", flush=True)
        print("Press Ctrl+C to stop the application.", flush=True)
        server.serve_forever()
    finally:
        server.server_close()
        if app is not None:
            app.extensions["camera_backend"].close()


if __name__ == "__main__":
    main()
