#!/usr/bin/env python3
"""
Enhetlig HA-klient med två backends bakom samma gränssnitt.

  SupervisorHA  — HA-add-on:  SUPERVISOR_TOKEN mot http://supervisor/core/api
  SshHA         — utveckling:  paramiko-SSH till HA-värden + HS256-JWT + curl

Väljs automatiskt: finns SUPERVISOR_TOKEN används Supervisor, annars SSH.
Tvinga med BATTERYCONTROL_HA_BACKEND=supervisor|ssh.

Gränssnittet är medvetet identiskt med den gamla HAPriceAndStateClient så att
befintlig produktionskod (optimizer_v2, generate_web, run_loop) inte behöver
ändras mer än att den hämtar klienten härifrån.

    get_state(entity_id)                     -> dict | None
    post_state(entity_id, state, attrs)      -> str
    call_service(domain, service, data)      -> str
    history(entity_id, start_iso, end_iso)   -> list[dict]
    read_json_file(path)                     -> obj
    write_www(filename, text)                -> str (sökväg eller beskrivning)
    close()
"""

import datetime
import json
import os
import urllib.error
import urllib.parse
import urllib.request

try:  # importerad som src.ha_client
    from . import paths
except ImportError:  # importerad med src/ på sys.path
    import paths

TIMEOUT = 30

# Inga hemligheter i koden. HA-atkomst kommer fran miljon:
#   HA_TOKEN             fardig long-lived-token (foredraget)
#   HA_REFRESH_TOKEN_ID + HA_JWT_KEY   signera egen HS256-token (fallback)
# Bada satts i secret-valvet, aldrig i git.


def _utc_z(value):
    """HA:s history-API accepterar inte +01:00-offset ("Invalid end_time").
    Normalisera till UTC med Z-suffix oavsett vad anroparen skickar."""
    if isinstance(value, str):
        txt = value.strip()
        if txt.endswith("Z"):
            return txt
        try:
            value = datetime.datetime.fromisoformat(txt)
        except ValueError:
            return txt
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _first_series(payload):
    """Svar är normalt [[...]] men kan vara {} vid fel — krascha inte på det."""
    if isinstance(payload, list) and payload and isinstance(payload[0], list):
        return payload[0]
    return []


class SupervisorHA:
    """HA-add-on: Supervisor-proxyn. Ingen SSH, ingen JWT-signering, ingen hemlighet i kod."""

    def __init__(self):
        self.token = os.environ.get("SUPERVISOR_TOKEN")
        if not self.token:
            raise RuntimeError("SUPERVISOR_TOKEN saknas")
        self.base = os.environ.get("SUPERVISOR_API", "http://supervisor/core/api").rstrip("/")

    def _req(self, method, path, payload=None):
        url = f"{self.base}{path}"
        data = None
        headers = {"Authorization": f"Bearer {self.token}"}
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                body = r.read().decode()
        except urllib.error.HTTPError as e:
            # Logga alltid. Tidigare svaldes felet tyst for GET, sa en 403 fran
            # Supervisor-proxyn (t.ex. nar homeassistant_api saknas i
            # config.yaml) sag ut som "ingen data" och varje lasning foll
            # tillbaka pa sin default. Appen verkade fungera men styrde pa
            # hittade varden - det kostade en hel felsokningskvart.
            detail = ""
            try:
                detail = e.read().decode()[:200]
            except Exception:
                pass
            print(f"VARNING: HA {method} {path} -> HTTP {e.code} {detail}")
            if e.code in (401, 403):
                print("VARNING: behorighetsfel mot HA. I add-on-lage: kontrollera "
                      "att homeassistant_api: true finns i config.yaml.")
            return None if method == "GET" else f"HTTP {e.code}"
        except urllib.error.URLError as e:
            print(f"VARNING: HA {method} {path} -> URLError {e.reason}")
            return None if method == "GET" else "URLError"
        if method == "GET":
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return None
        return body

    def get_state(self, entity_id):
        return self._req("GET", f"/states/{entity_id}")

    def post_state(self, entity_id, state, attributes=None):
        return self._req("POST", f"/states/{entity_id}",
                         {"state": str(state), "attributes": attributes or {}})

    def call_service(self, domain, service, service_data=None, return_response=False):
        path = f"/services/{domain}/{service}"
        if return_response:
            path += "?return_response=true"
        return self._req("POST", path, service_data or {})

    def history(self, entity_id, start_iso, end_iso):
        q = urllib.parse.urlencode({
            "filter_entity_id": entity_id,
            "end_time": _utc_z(end_iso),
            "minimal_response": "",
            "no_attributes": "",
        })
        payload = self._req("GET", f"/history/period/{_utc_z(start_iso)}?{q}")
        if not payload:
            return []
        return _first_series(payload)

    def read_json_file(self, path):
        # I en add-on ligger HA:s konfigurationskatalog pa /config (map: config:rw).
        # Anvand paths.config_dir() i stallet for att hardkoda /config, sa att
        # BATTERYCONTROL_CONFIG_DIR faktiskt respekteras (den satts av run.sh och
        # av testmiljoer). /homeassistant/ ar HA:s interna prefix i dev-laget.
        candidates = [path]
        if path.startswith("/homeassistant/"):
            cfg = paths.config_dir() or "/config"
            candidates.append(
                os.path.join(cfg, path[len("/homeassistant/"):])
            )
        for p in candidates:
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
        raise FileNotFoundError(path)

    def write_www(self, filename, text):
        cfg = paths.config_dir()
        if not cfg:
            raise RuntimeError("Ingen /config katalog — add-on saknar map: config:rw?")
        target_dir = os.path.join(cfg, "www", "batterycontrol")
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, filename)
        with open(target, "w", encoding="utf-8") as f:
            f.write(text)
        return target

    def close(self):
        pass


class SshHA:
    """Utvecklingsläget: SSH till HA-värden, mintar JWT lokalt, kör curl på andra sidan."""

    def __init__(self):
        import paramiko  # lazy: add-on-imagen behöver inte paramiko

        self.host = os.environ.get("HA_HOST", "10.12.22.12")
        self.user = os.environ.get("HA_USER", "root")
        self.key = os.environ.get("HA_SSH_KEY", "/workspace/id_ed25519")
        self.api = os.environ.get("HA_API_URL", "http://172.30.32.1:8123/api")

        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh.connect(self.host, username=self.user, key_filename=self.key)
        self.token = _generate_ha_token()

    def _curl(self, url, method="GET", payload=None):
        if method == "GET":
            cmd = f'curl -s "{url}" -H "Authorization: Bearer {self.token}"'
        else:
            cmd = (
                f'''curl -s -X {method} "{url}" '''
                f'''-H "Authorization: Bearer {self.token}" '''
                f'''-H "Content-Type: application/json" '''
                f"""-d '{json.dumps(payload or {})}' """
            )
        _, stdout, _ = self.ssh.exec_command(cmd)
        return stdout.read().decode()

    def get_state(self, entity_id):
        try:
            return json.loads(self._curl(f"{self.api}/states/{entity_id}") or "null")
        except (json.JSONDecodeError, TypeError):
            return None

    def post_state(self, entity_id, state, attributes=None):
        return self._curl(f"{self.api}/states/{entity_id}", "POST",
                          {"state": str(state), "attributes": attributes or {}})

    def call_service(self, domain, service, service_data=None, return_response=False):
        url = f"{self.api}/services/{domain}/{service}"
        if return_response:
            url += "?return_response=true"
        return self._curl(url, "POST", service_data or {})

    def history(self, entity_id, start_iso, end_iso):
        url = (f"{self.api}/history/period/{_utc_z(start_iso)}"
               f"?filter_entity_id={entity_id}&end_time={_utc_z(end_iso)}"
               f"&minimal_response&no_attributes")
        try:
            payload = json.loads(self._curl(url) or "[]")
        except json.JSONDecodeError:
            return []
        return _first_series(payload)

    def read_json_file(self, path):
        sftp = self.ssh.open_sftp()
        with sftp.open(path) as f:
            return json.load(f)

    def write_www(self, filename, text):
        import subprocess
        import tempfile

        target_dir = "/config/www/batterycontrol"
        subprocess.run(
            ["ssh", "-i", self.key, "-o", "StrictHostKeyChecking=accept-new",
             f"{self.user}@{self.host}", f"mkdir -p {target_dir}"],
            check=True)
        with tempfile.NamedTemporaryFile("w", suffix=".tmp", delete=False, encoding="utf-8") as tf:
            tf.write(text)
            tmp = tf.name
        try:
            subprocess.run(
                ["scp", "-i", self.key, "-o", "StrictHostKeyChecking=accept-new",
                 tmp, f"{self.user}@{self.host}:{target_dir}/{filename}"],
                check=True)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return f"{target_dir}/{filename}"

    def close(self):
        try:
            self.ssh.close()
        except Exception:
            pass


def _generate_ha_token():
    """Fardig token ur HA_TOKEN, annars HS256-JWT ur refresh-token-id + nyckel.

    Foredraget lage ar HA_TOKEN: en fardig long-lived-token som HA redan har
    signerat. Den kan inte anvandas for att mint:a fler tokens, till skillnad
    fran signeringsnyckeln, sa den ar den mindre kansliga hemligheten.
    """
    import base64
    import hashlib
    import hmac
    import time

    ready = os.environ.get("HA_TOKEN")
    if ready:
        return ready.strip()

    rid = os.environ.get("HA_REFRESH_TOKEN_ID")
    key = os.environ.get("HA_JWT_KEY")
    if not (rid and key):
        raise RuntimeError(
            "Ingen HA-atkomst: satt HA_TOKEN (fardig token) eller "
            "HA_REFRESH_TOKEN_ID + HA_JWT_KEY i miljon."
        )

    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    payload = {"iss": rid, "iat": now, "exp": now + 3600 * 24 * 30}
    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
    signing_input = (b64(json.dumps(header, separators=(",", ":")).encode())
                     + "." + b64(json.dumps(payload, separators=(",", ":")).encode()))
    sig = hmac.new(key.encode(), signing_input.encode(), hashlib.sha256).digest()
    return signing_input + "." + b64(sig)


def get_client():
    """Returnerar rätt backend för miljön."""
    choice = os.environ.get("BATTERYCONTROL_HA_BACKEND", "").lower()
    if choice == "supervisor":
        return SupervisorHA()
    if choice == "ssh":
        return SshHA()
    if paths.is_addon():
        return SupervisorHA()
    return SshHA()
