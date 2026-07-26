#!/usr/bin/env python3
"""Start the local FPL dashboard service and open it in the default browser."""

import argparse
from pathlib import Path
import sys
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fpl_intel.server import create_server


def main():
    parser = argparse.ArgumentParser(description="Start the local-only FPL dashboard service")
    parser.add_argument("--port", type=int, default=8877)
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser automatically")
    args = parser.parse_args()

    server = create_server(ROOT, host="127.0.0.1", port=args.port)
    url = f"http://127.0.0.1:{server.server_port}/dashboard.html"
    print(f"FPL dashboard: {url}")
    print("Refreshes run only when you press 'Refresh now'. No schedule is configured.")
    print("Press Control-C to stop the local service.")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping FPL dashboard service.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
