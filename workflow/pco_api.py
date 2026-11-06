"""Read-only PCO access, plus the attachment-open action needed for downloads."""

import base64
import ipaddress
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import credential_store

ORIGIN = "https://api.planningcenteronline.com"
MAX_FILE_BYTES = 30 * 1024 * 1024


class WorkflowError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def identifier(value):
    value = str(value)
    if not value.isascii() or not value.isdigit():
        raise WorkflowError("invalid_input", "PCO IDs must be decimal digits.")
    return value


def public_https(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise WorkflowError("unsafe_url", "Only public HTTPS download URLs are allowed.")
    if parsed.port not in (None, 443):
        raise WorkflowError("unsafe_url", "Unexpected download port.")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise WorkflowError("unsafe_url", "Non-public download host.")
    except OSError:
        raise WorkflowError("network_error", "Download host could not be resolved.") from None
    return url


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise WorkflowError("unsafe_redirect", "Authenticated API redirects are not allowed.")


class DownloadRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_https(newurl)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected:
            redirected.remove_header("Authorization")
        return redirected


def credentials():
    values = [os.environ.get("PCO_CLIENT_ID"), os.environ.get("PCO_SECRET")]
    if any(values) and not all(values):
        raise WorkflowError("auth_error", "Both PCO environment credentials must be set.")
    if not all(values):
        stored = credential_store.load()
        if any(stored):
            if not all(stored):
                raise WorkflowError("auth_error", "Incomplete OS credentials; configure PCO locally.")
            values = stored
    if not all(values):
        values = []
        for account in ("client_id", "client_secret"):
            try:
                result = subprocess.run(
                    ["security", "find-generic-password", "-s", "pco-cli", "-a", account, "-w"],
                    capture_output=True, text=True, timeout=30, check=True,
                )
            except (OSError, subprocess.SubprocessError):
                raise WorkflowError(
                    "auth_error", "OS store/legacy Keychain unavailable; approve access or configure locally with reborn.py auth."
                ) from None
            values.append(result.stdout.strip())
    if not all(values):
        raise WorkflowError("auth_error", "Missing PCO credentials.")
    return base64.b64encode(":".join(values).encode()).decode()


class PCO:
    def __init__(self):
        self._auth = None
        self._api = urllib.request.build_opener(NoRedirect())
        self._files = urllib.request.build_opener(DownloadRedirect())

    def request(self, path, method="GET"):
        url = urllib.parse.urljoin(ORIGIN, path)
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme, parsed.netloc) != ("https", "api.planningcenteronline.com"):
            raise WorkflowError("unsafe_url", "API origin mismatch.")
        if not parsed.path.startswith("/services/v2/"):
            raise WorkflowError("unsafe_url", "Only Services API paths are allowed.")
        if method != "GET":
            parts = parsed.path.strip("/").split("/")
            if not (method == "POST" and len(parts) == 5
                    and parts[:3] == ["services", "v2", "attachments"]
                    and parts[3].isdigit() and parts[4] == "open"):
                raise WorkflowError("unsafe_action", "PCO content mutations are not supported.")
        if self._auth is None:
            self._auth = credentials()
        for attempt in range(2):
            request = urllib.request.Request(
                url, method=method, data=b"" if method == "POST" else None,
                headers={"Authorization": "Basic " + self._auth, "Accept": "application/json"},
            )
            try:
                with self._api.open(request, timeout=40) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if error.code in (401, 403):
                    raise WorkflowError("auth_error", "PCO denied authentication or permission.") from None
                if error.code == 429 and attempt == 0:
                    delay = error.headers.get("Retry-After", "1")
                    if not delay.isdigit() or int(delay) > 30:
                        raise WorkflowError(
                            "rate_limited", "PCO requested a longer cooldown; retry later."
                        ) from None
                    time.sleep(int(delay))
                    continue
                if error.code >= 500 and attempt == 0:
                    time.sleep(1)
                    continue
                raise WorkflowError("api_error", f"PCO returned HTTP {error.code}.") from None
            except (OSError, urllib.error.URLError):
                if attempt == 0:
                    time.sleep(1)
                    continue
                raise WorkflowError("network_error", "PCO request failed after one retry.") from None
            except (ValueError, KeyError, TypeError):
                raise WorkflowError("invalid_response", "PCO returned an invalid JSON response.") from None
        raise WorkflowError("api_error", "PCO retry limit reached.")

    def pages(self, path):
        rows, visited = [], set()
        while path:
            if path in visited:
                raise WorkflowError("invalid_response", "PCO pagination cycle detected.")
            visited.add(path)
            page = self.request(path)
            if not isinstance(page.get("data"), list):
                raise WorkflowError("invalid_response", "Expected a PCO collection.")
            rows.extend(page["data"])
            path = page.get("links", {}).get("next")
        return rows

    def catalog(self, query=None):
        path = "/services/v2/songs?per_page=100"
        if query:
            path += "&" + urllib.parse.urlencode({"where[title]": query})
        return [
            {"song_id": r["id"], "title": r["attributes"]["title"],
             "hidden": r["attributes"].get("hidden", False)}
            for r in self.pages(path) if not r["attributes"].get("hidden")
        ]

    def inspect(self, song_id):
        song_id = identifier(song_id)
        path = f"/services/v2/songs/{song_id}"
        song = self.request(path)["data"]
        if song["attributes"].get("hidden"):
            raise WorkflowError("needs_review", "Song is hidden; choose an active library song.")
        rows = self.pages(path + "/arrangements?per_page=100")
        return [
            {"song_id": song_id, "title": song["attributes"]["title"],
             "arrangement_id": r["id"], "arrangement": r["attributes"]["name"],
             "bpm": r["attributes"].get("bpm"), "meter": r["attributes"].get("meter"),
             "length": r["attributes"].get("length"),
             "updated_at": r["attributes"].get("updated_at"),
             "lyrics": r["attributes"].get("lyrics") or ""}
            for r in rows if not r["attributes"].get("archived_at")
        ]

    def file(self, attachment_id):
        result = self.request(
            f"/services/v2/attachments/{identifier(attachment_id)}/open", method="POST"
        )
        try:
            url = public_https(result["data"]["attributes"]["attachment_url"])
        except (KeyError, TypeError):
            raise WorkflowError("invalid_response", "PCO did not return a download URL.") from None
        # The file opener never receives API Authorization, including on redirects.
        for attempt in range(2):
            try:
                with self._files.open(urllib.request.Request(url), timeout=40) as response:
                    content = response.read(MAX_FILE_BYTES + 1)
                if not content or len(content) > MAX_FILE_BYTES:
                    raise WorkflowError("invalid_file", "Empty file or file exceeds 30 MiB.")
                return content
            except urllib.error.HTTPError as error:
                raise WorkflowError("download_error", f"File server returned HTTP {error.code}.") from None
            except (OSError, urllib.error.URLError):
                if attempt == 0:
                    continue
                raise WorkflowError("network_error", "File transfer failed after one retry.") from None
