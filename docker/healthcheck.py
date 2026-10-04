"""Local liveness only; never authenticates to or changes a tenant."""
import http.client
import os


def main():
    connection = http.client.HTTPConnection("127.0.0.1", int(os.environ.get("ZTB_PORT", "8765")), timeout=3)
    try:
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        return 0 if response.status == 200 and response.read() == b'{"status": "ok"}' else 1
    except (OSError, http.client.HTTPException):
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
