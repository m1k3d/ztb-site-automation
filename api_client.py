"""Shared ZTB session and single-retry authentication, with no import-time I/O."""

import requests

from automation_config import Settings, credential, normalize_base
from ztb_login import ztb_login


class ZTBClient:
    def __init__(self, config: Settings, *, session=None, debug=False, emit=print):
        self.config = config
        self.base_root = normalize_base(config.ztb_api_base)
        self.api_v3 = self.base_root + "/api/v3"
        self.api_v2 = self.base_root + "/api/v2"
        self.origin = self.base_root.replace("-api.", ".")
        self.referer = self.origin + "/" + config.referer_path.strip("/")
        self.session = session if session is not None else requests.Session()
        self.debug, self.emit = debug, emit
        self.token = credential(config.bearer)
        self.session.headers.update({"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "ztb-site-automation"})

    def refresh(self):
        self.token, _ = ztb_login(config=self.config, write_env=True, quiet=True)

    def request(self, method, url, **kwargs):
        if not self.token:
            self.emit("Authenticating to ZTB…")
            self.refresh()
        self.session.headers["Authorization"] = "Bearer " + self.token
        response = self.session.request(method, url, **kwargs)
        if response.status_code == 401:
            self.emit("Refreshing expired ZTB token and retrying once…")
            self.refresh()
            self.session.headers["Authorization"] = "Bearer " + self.token
            response = self.session.request(method, url, **kwargs)
        if self.debug:
            self.emit(f"{method} {url} -> {response.status_code}")
        return response

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def close(self):
        self.session.close()
