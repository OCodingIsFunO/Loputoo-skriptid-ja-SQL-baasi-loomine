#!/opt/inventory-reconcile/venv/bin/python
import os
import sys
import json
import time
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import psycopg
import requests
from dotenv import load_dotenv

ENV_FILE = "/opt/inventory-reconcile/.env"
load_dotenv(ENV_FILE)

# Zabbix
ZABBIX_URL = os.getenv("ZABBIX_URL")
ZABBIX_TOKEN = os.getenv("ZABBIX_TOKEN")

# Keycloak
KEYCLOAK_URL = os.getenv("KEYCLOAK_URL", "").rstrip("/")
REALM = os.getenv("KEYCLOAK_REALM")
CLIENT_ID = os.getenv("KEYCLOAK_CLIENT_ID")
CLIENT_SECRET = os.getenv("KEYCLOAK_CLIENT_SECRET")

VERIFY_TLS_RAW = os.getenv("VERIFY_TLS", "true").strip().lower()
CA_CERT_PATH = os.getenv("CA_CERT_PATH", "").strip()

# Database
DB_HOST = os.environ["DB_HOST"]
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ["DB_NAME"]
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]

# Logging
LOG_DIR = os.getenv("LOG_DIR", "/opt/inventory-reconcile/logs")

# Keycloak paging
PAGE_SIZE = int(os.getenv("KEYCLOAK_PAGE_SIZE", "200"))

if not ZABBIX_URL or not ZABBIX_TOKEN:
    print("Missing ZABBIX_URL or ZABBIX_TOKEN in .env", file=sys.stderr)
    sys.exit(1)

if not KEYCLOAK_URL or not REALM or not CLIENT_ID or not CLIENT_SECRET:
    print("Missing Keycloak settings in .env", file=sys.stderr)
    sys.exit(1)

os.makedirs(LOG_DIR, exist_ok=True)
log_filename = datetime.now().strftime("%m%Y.log")
log_path = os.path.join(LOG_DIR, log_filename)
logging.basicConfig(filename=log_path, level=logging.INFO, format="%(message)s")

ZABBIX_HEADERS = {
    "Content-Type": "application/json-rpc",
    "Authorization": f"Bearer {ZABBIX_TOKEN}",
}

_token_cache: Dict[str, Any] = {"access_token": None, "expires_at": 0}


def log_event(*, source: str, action: str, entity: str, details: str) -> None:
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    logging.info(
        f'{ts} source={source} action={action} entity={entity} '
        f'details="{details.replace(chr(34), chr(39))}"'
    )


def log_summary(
    *,
    status: str,
    zabbix_hosts_seen: int,
    zabbix_ips_seen: int,
    keycloak_users_seen: int,
    keycloak_groups_seen: int,
    hosts_reactivated: int,
    hosts_deactivated: int,
    ips_deleted: int,
    users_reactivated: int,
    users_deactivated: int,
    groups_reactivated: int,
    groups_deactivated: int,
    error: str | None = None,
) -> None:
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    parts = [
        ts,
        "source=reconcile",
        "action=summary",
        f"status={status}",
        f"zabbix_hosts_seen={zabbix_hosts_seen}",
        f"zabbix_ips_seen={zabbix_ips_seen}",
        f"keycloak_users_seen={keycloak_users_seen}",
        f"keycloak_groups_seen={keycloak_groups_seen}",
        f"hosts_reactivated={hosts_reactivated}",
        f"hosts_deactivated={hosts_deactivated}",
        f"ips_deleted={ips_deleted}",
        f"users_reactivated={users_reactivated}",
        f"users_deactivated={users_deactivated}",
        f"groups_reactivated={groups_reactivated}",
        f"groups_deactivated={groups_deactivated}",
    ]
    if error:
        parts.append(f'error="{error.replace(chr(34), chr(39))}"')
    logging.info(" ".join(parts))


def normalize_text(value: str | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def get_db_connection():
    return psycopg.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )


def get_verify_value() -> Any:
    if VERIFY_TLS_RAW == "false":
        return False
    if CA_CERT_PATH:
        return CA_CERT_PATH
    return True


VERIFY_VALUE = get_verify_value()


def get_keycloak_access_token() -> str:
    now = time.time()

    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 5:
        return _token_cache["access_token"]

    token_url = f"{KEYCLOAK_URL}/realms/{REALM}/protocol/openid-connect/token"
    response = requests.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        timeout=30,
        verify=VERIFY_VALUE,
    )
    response.raise_for_status()

    data = response.json()
    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = now + int(data.get("expires_in", 60))
    return _token_cache["access_token"]


def keycloak_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {get_keycloak_access_token()}",
        "Accept": "application/json",
    }


def keycloak_get_json(url: str, params: Optional[Dict[str, Any]] = None) -> Any:
    response = requests.get(
        url,
        headers=keycloak_headers(),
        params=params,
        timeout=60,
        verify=VERIFY_VALUE,
    )

    if response.status_code == 401:
        _token_cache["access_token"] = None
        _token_cache["expires_at"] = 0
        response = requests.get(
            url,
            headers=keycloak_headers(),
            params=params,
            timeout=60,
            verify=VERIFY_VALUE,
        )

    response.raise_for_status()
    return response.json()


def fetch_zabbix_hosts() -> list[dict]:
    payload = {
        "jsonrpc": "2.0",
        "method": "host.get",
        "params": {
            "output": ["hostid", "name", "host"],
            "selectInterfaces": ["ip", "main"],
        },
        "id": 1,
    }

    response = requests.post(ZABBIX_URL, headers=ZABBIX_HEADERS, json=payload, timeout=30)
    response.raise_for_status()
    data = response.json()

    if "error" in data:
        raise RuntimeError(f"Zabbix API error: {json.dumps(data['error'])}")

    return data.get("result", [])


def pick_host_ips(interfaces: list[dict]) -> list[str]:
    if not interfaces:
        return []

    ips: list[str] = []
    seen: set[str] = set()

    for interface in interfaces:
        ip = normalize_text(interface.get("ip"))
        if not ip:
            continue
        if ip in {"127.0.0.1", "::1", "0.0.0.0"}:
            continue
        if ip.startswith("127."):
            continue
        if ip in seen:
            continue

        seen.add(ip)
        ips.append(ip)

    return ips


def get_keycloak_users() -> List[Dict[str, Any]]:
    users: List[Dict[str, Any]] = []
    first = 0

    while True:
        page = keycloak_get_json(
            f"{KEYCLOAK_URL}/admin/realms/{REALM}/users",
            params={"first": first, "max": PAGE_SIZE},
        )
        if not page:
            break

        users.extend(page)

        if len(page) < PAGE_SIZE:
            break

        first += PAGE_SIZE

    return users


def get_keycloak_groups() -> List[Dict[str, Any]]:
    return keycloak_get_json(f"{KEYCLOAK_URL}/admin/realms/{REALM}/groups")


def flatten_groups(groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    flattened: List[Dict[str, Any]] = []

    for group in groups:
        flattened.append(
            {
                "id": normalize_text(group.get("id")),
                "name": normalize_text(group.get("name")),
            }
        )

        children = group.get("subGroups") or []
        if children:
            flattened.extend(flatten_groups(children))

    return flattened


def build_seen_sets() -> Tuple[Set[int], Set[Tuple[int, str]], Set[str], Set[str]]:
    zabbix_hosts = fetch_zabbix_hosts()
    keycloak_users = get_keycloak_users()
    keycloak_groups_tree = get_keycloak_groups()
    keycloak_groups = flatten_groups(keycloak_groups_tree)

    seen_host_ids: Set[int] = set()
    seen_host_ips: Set[Tuple[int, str]] = set()
    seen_user_ids: Set[str] = set()
    seen_group_ids: Set[str] = set()

    for host_obj in zabbix_hosts:
        host_id = int(host_obj["hostid"])
        seen_host_ids.add(host_id)

        for ip in pick_host_ips(host_obj.get("interfaces", [])):
            seen_host_ips.add((host_id, ip))

    for user in keycloak_users:
        user_id = normalize_text(user.get("id"))
        if user_id:
            seen_user_ids.add(user_id)

    for group in keycloak_groups:
        group_id = normalize_text(group.get("id"))
        if group_id:
            seen_group_ids.add(group_id)

    return seen_host_ids, seen_host_ips, seen_user_ids, seen_group_ids


def reconcile() -> dict:
    seen_host_ids, seen_host_ips, seen_user_ids, seen_group_ids = build_seen_sets()

    stats = {
        "zabbix_hosts_seen": len(seen_host_ids),
        "zabbix_ips_seen": len(seen_host_ips),
        "keycloak_users_seen": len(seen_user_ids),
        "keycloak_groups_seen": len(seen_group_ids),
        "hosts_reactivated": 0,
        "hosts_deactivated": 0,
        "ips_deleted": 0,
        "users_reactivated": 0,
        "users_deactivated": 0,
        "groups_reactivated": 0,
        "groups_deactivated": 0,
    }

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Hosts: reactivate if returned again
            if seen_host_ids:
                cur.execute(
                    """
                    SELECT id
                    FROM public.host
                    WHERE active = FALSE
                      AND id = ANY(%s)
                    """,
                    (list(seen_host_ids),),
                )
                reactivated_host_ids = [row[0] for row in cur.fetchall()]

                for host_id in reactivated_host_ids:
                    cur.execute(
                        """
                        UPDATE public.host
                        SET active = TRUE, updated_at = NOW()
                        WHERE id = %s
                        """,
                        (host_id,),
                    )
                    stats["hosts_reactivated"] += 1
                    log_event(
                        source="zabbix",
                        action="reactivate",
                        entity="host",
                        details=f"id={host_id}",
                    )

                cur.execute(
                    """
                    SELECT id
                    FROM public.host
                    WHERE active = TRUE
                      AND id <> ALL(%s)
                    """,
                    (list(seen_host_ids),),
                )
                stale_host_ids = [row[0] for row in cur.fetchall()]

                for host_id in stale_host_ids:
                    cur.execute(
                        """
                        UPDATE public.host
                        SET active = FALSE, updated_at = NOW()
                        WHERE id = %s
                        """,
                        (host_id,),
                    )
                    stats["hosts_deactivated"] += 1
                    log_event(
                        source="zabbix",
                        action="deactivate",
                        entity="host",
                        details=f"id={host_id}",
                    )
            else:
                log_event(
                    source="zabbix",
                    action="skip",
                    entity="host_cleanup",
                    details="Zabbix returned 0 hosts, cleanup skipped",
                )

            # Host IPs: delete if no longer present in Zabbix
            if seen_host_ids:
                cur.execute("SELECT host_id, host(ip) FROM public.host_ip")
                db_host_ips = {(int(row[0]), normalize_text(row[1])) for row in cur.fetchall()}

                stale_host_ips = db_host_ips - seen_host_ips

                for host_id, ip in sorted(stale_host_ips):
                    cur.execute(
                        """
                        DELETE FROM public.host_ip
                        WHERE host_id = %s
                          AND ip = %s::inet
                        """,
                        (host_id, ip),
                    )
                    stats["ips_deleted"] += 1
                    log_event(
                        source="zabbix",
                        action="delete",
                        entity="host_ip",
                        details=f"host_id={host_id}, ip={ip}",
                    )
            else:
                log_event(
                    source="zabbix",
                    action="skip",
                    entity="host_ip_cleanup",
                    details="Zabbix returned 0 hosts, IP cleanup skipped",
                )

            # Users: reactivate if returned again
            if seen_user_ids:
                cur.execute(
                    """
                    SELECT id
                    FROM public.sso_user
                    WHERE active = FALSE
                      AND id = ANY(%s)
                    """,
                    (list(seen_user_ids),),
                )
                reactivated_user_ids = [row[0] for row in cur.fetchall()]

                for user_id in reactivated_user_ids:
                    cur.execute(
                        """
                        UPDATE public.sso_user
                        SET active = TRUE, updated_at = NOW()
                        WHERE id = %s
                        """,
                        (user_id,),
                    )
                    stats["users_reactivated"] += 1
                    log_event(
                        source="keycloak",
                        action="reactivate",
                        entity="sso_user",
                        details=f"id={user_id}",
                    )

                cur.execute(
                    """
                    SELECT id
                    FROM public.sso_user
                    WHERE active = TRUE
                      AND id <> ALL(%s)
                    """,
                    (list(seen_user_ids),),
                )
                stale_user_ids = [row[0] for row in cur.fetchall()]

                for user_id in stale_user_ids:
                    cur.execute(
                        """
                        UPDATE public.sso_user
                        SET active = FALSE, updated_at = NOW()
                        WHERE id = %s
                        """,
                        (user_id,),
                    )
                    stats["users_deactivated"] += 1
                    log_event(
                        source="keycloak",
                        action="deactivate",
                        entity="sso_user",
                        details=f"id={user_id}",
                    )
            else:
                log_event(
                    source="keycloak",
                    action="skip",
                    entity="user_cleanup",
                    details="Keycloak returned 0 users, cleanup skipped",
                )

            # Groups: reactivate if returned again
            if seen_group_ids:
                cur.execute(
                    """
                    SELECT id
                    FROM public.sso_group
                    WHERE active = FALSE
                      AND id = ANY(%s)
                    """,
                    (list(seen_group_ids),),
                )
                reactivated_group_ids = [row[0] for row in cur.fetchall()]

                for group_id in reactivated_group_ids:
                    cur.execute(
                        """
                        UPDATE public.sso_group
                        SET active = TRUE, updated_at = NOW()
                        WHERE id = %s
                        """,
                        (group_id,),
                    )
                    stats["groups_reactivated"] += 1
                    log_event(
                        source="keycloak",
                        action="reactivate",
                        entity="sso_group",
                        details=f"id={group_id}",
                    )

                cur.execute(
                    """
                    SELECT id
                    FROM public.sso_group
                    WHERE active = TRUE
                      AND id <> ALL(%s)
                    """,
                    (list(seen_group_ids),),
                )
                stale_group_ids = [row[0] for row in cur.fetchall()]

                for group_id in stale_group_ids:
                    cur.execute(
                        """
                        UPDATE public.sso_group
                        SET active = FALSE, updated_at = NOW()
                        WHERE id = %s
                        """,
                        (group_id,),
                    )
                    stats["groups_deactivated"] += 1
                    log_event(
                        source="keycloak",
                        action="deactivate",
                        entity="sso_group",
                        details=f"id={group_id}",
                    )
            else:
                log_event(
                    source="keycloak",
                    action="skip",
                    entity="group_cleanup",
                    details="Keycloak returned 0 groups, cleanup skipped",
                )

        conn.commit()

    return stats


def main() -> int:
    try:
        stats = reconcile()
        log_summary(status="ok", **stats)
        print(
            "Reconcile complete: "
            f"zabbix_hosts_seen={stats['zabbix_hosts_seen']} "
            f"zabbix_ips_seen={stats['zabbix_ips_seen']} "
            f"keycloak_users_seen={stats['keycloak_users_seen']} "
            f"keycloak_groups_seen={stats['keycloak_groups_seen']} "
            f"hosts_reactivated={stats['hosts_reactivated']} "
            f"hosts_deactivated={stats['hosts_deactivated']} "
            f"ips_deleted={stats['ips_deleted']} "
            f"users_reactivated={stats['users_reactivated']} "
            f"users_deactivated={stats['users_deactivated']} "
            f"groups_reactivated={stats['groups_reactivated']} "
            f"groups_deactivated={stats['groups_deactivated']}"
        )
        return 0
    except requests.RequestException as exc:
        log_summary(
            status="error",
            zabbix_hosts_seen=0,
            zabbix_ips_seen=0,
            keycloak_users_seen=0,
            keycloak_groups_seen=0,
            hosts_reactivated=0,
            hosts_deactivated=0,
            ips_deleted=0,
            users_reactivated=0,
            users_deactivated=0,
            groups_reactivated=0,
            groups_deactivated=0,
            error=str(exc),
        )
        print(f"HTTP error: {exc}", file=sys.stderr)
        return 1
    except KeyError as exc:
        log_summary(
            status="error",
            zabbix_hosts_seen=0,
            zabbix_ips_seen=0,
            keycloak_users_seen=0,
            keycloak_groups_seen=0,
            hosts_reactivated=0,
            hosts_deactivated=0,
            ips_deleted=0,
            users_reactivated=0,
            users_deactivated=0,
            groups_reactivated=0,
            groups_deactivated=0,
            error=f"Missing required environment variable: {exc}",
        )
        print(f"Missing required environment variable: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        log_summary(
            status="error",
            zabbix_hosts_seen=0,
            zabbix_ips_seen=0,
            keycloak_users_seen=0,
            keycloak_groups_seen=0,
            hosts_reactivated=0,
            hosts_deactivated=0,
            ips_deleted=0,
            users_reactivated=0,
            users_deactivated=0,
            groups_reactivated=0,
            groups_deactivated=0,
            error=str(exc),
        )
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
