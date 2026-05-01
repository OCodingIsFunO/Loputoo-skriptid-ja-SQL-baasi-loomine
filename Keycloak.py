#!/opt/keycloak-audit/venv/bin/python
import os, sys, time, logging, psycopg, requests
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from dotenv import load_dotenv

ENV_FILE = "/opt/keycloak-audit/.env" ; load_dotenv(ENV_FILE)
KEYCLOAK_URL = os.environ["KEYCLOAK_URL"].rstrip("/")
REALM = os.environ["KEYCLOAK_REALM"]
CLIENT_ID = os.environ["KEYCLOAK_CLIENT_ID"]
CLIENT_SECRET = os.environ["KEYCLOAK_CLIENT_SECRET"]
VERIFY_TLS_RAW = os.environ.get("VERIFY_TLS", "true").strip().lower()
CA_CERT_PATH = os.environ.get("CA_CERT_PATH", "").strip()

DB_HOST = os.environ["DB_HOST"]
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ["DB_NAME"]
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]

PAGE_SIZE = int(os.environ.get("KEYCLOAK_PAGE_SIZE", "200"))
LOG_DIR = os.environ.get("LOG_DIR", "/opt/keycloak-audit/logs")

def get_verify_value() -> Any:
    if VERIFY_TLS_RAW == "false": return False
    if CA_CERT_PATH: return CA_CERT_PATH
    return True

VERIFY_VALUE = get_verify_value()
_token_cache: Dict[str, Any] = {"access_token": None, "expires_at": 0}

os.makedirs(LOG_DIR, exist_ok=True)
log_path = os.path.join(LOG_DIR, datetime.now().strftime("%m%Y.log"))
logging.basicConfig(filename=log_path, level=logging.INFO, format="%(message)s")

def log_summary(*, status: str, users_seen: int, users_inserted: int, users_updated: int, users_unchanged: int,
                groups_seen: int, groups_inserted: int, groups_updated: int, groups_unchanged: int,
                links_seen: int, links_inserted: int, links_unchanged: int, links_removed: int,
                error: str | None = None) -> None:
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    parts = [
        ts, "source=keycloak", f"status={status}",
        f"users_seen={users_seen}", f"users_inserted={users_inserted}", f"users_updated={users_updated}", f"users_unchanged={users_unchanged}",
        f"groups_seen={groups_seen}", f"groups_inserted={groups_inserted}", f"groups_updated={groups_updated}", f"groups_unchanged={groups_unchanged}",
        f"links_seen={links_seen}", f"links_inserted={links_inserted}", f"links_unchanged={links_unchanged}", f"links_removed={links_removed}",
    ]
    if error: parts.append(f'error="{error.replace(chr(34), chr(39))}"')
    logging.info(" ".join(parts))

def normalize_text(value: str | None) -> str:
    return "" if value is None else str(value).strip()

def get_db_connection():
    return psycopg.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD)

def get_access_token() -> str:
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 5:
        return _token_cache["access_token"]
    token_url = f"{KEYCLOAK_URL}/realms/{REALM}/protocol/openid-connect/token"
    response = requests.post(
        token_url,
        data={"grant_type": "client_credentials", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        timeout=30, verify=VERIFY_VALUE
    )
    response.raise_for_status()
    data = response.json()
    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = now + int(data.get("expires_in", 60))
    return _token_cache["access_token"]

def auth_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {get_access_token()}", "Accept": "application/json"}

def get_json(url: str, params: Optional[Dict[str, Any]] = None) -> Any:
    response = requests.get(url, headers=auth_headers(), params=params, timeout=60, verify=VERIFY_VALUE)
    if response.status_code == 401:
        _token_cache["access_token"] = None; _token_cache["expires_at"] = 0
        response = requests.get(url, headers=auth_headers(), params=params, timeout=60, verify=VERIFY_VALUE)
    response.raise_for_status()
    return response.json()

def get_users() -> List[Dict[str, Any]]:
    users: List[Dict[str, Any]] = []
    first = 0
    while True:
        page = get_json(f"{KEYCLOAK_URL}/admin/realms/{REALM}/users", params={"first": first, "max": PAGE_SIZE})
        if not page: break
        users.extend(page)
        if len(page) < PAGE_SIZE: break
        first += PAGE_SIZE
    return users

def get_groups() -> List[Dict[str, Any]]:
    return get_json(f"{KEYCLOAK_URL}/admin/realms/{REALM}/groups")

def flatten_groups(groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    flattened: List[Dict[str, Any]] = []
    for group in groups:
        flattened.append({
            "id": normalize_text(group.get("id")),
            "name": normalize_text(group.get("name")),
            "description": normalize_text(group.get("path")),
        })
        children = group.get("subGroups") or []
        if children: flattened.extend(flatten_groups(children))
    return flattened

def get_user_groups(user_id: str) -> List[Dict[str, Any]]:
    return get_json(f"{KEYCLOAK_URL}/admin/realms/{REALM}/users/{user_id}/groups")

def build_snapshot() -> Dict[str, Any]:
    users = get_users()
    groups = flatten_groups(get_groups())
    user_group_links: Set[Tuple[str, str]] = set()

    for user in users:
        user_id = normalize_text(user.get("id"))
        if not user_id: continue
        for group in get_user_groups(user_id):
            group_id = normalize_text(group.get("id"))
            if group_id: user_group_links.add((user_id, group_id))

    return {
        "users": [
            {"id": normalize_text(u.get("id")), "username": normalize_text(u.get("username")),
             "firstname": normalize_text(u.get("firstName")), "lastname": normalize_text(u.get("lastName"))}
            for u in users if normalize_text(u.get("id")) and normalize_text(u.get("username"))
        ],
        "groups": [g for g in groups if g["id"] and g["name"]],
        "user_groups": sorted(user_group_links),
    }

def sync_to_db(snapshot: Dict[str, Any]) -> Dict[str, int]:
    stats = {
        "users_seen": len(snapshot["users"]), "users_inserted": 0, "users_updated": 0, "users_unchanged": 0,
        "groups_seen": len(snapshot["groups"]), "groups_inserted": 0, "groups_updated": 0, "groups_unchanged": 0,
        "links_seen": len(snapshot["user_groups"]), "links_inserted": 0, "links_unchanged": 0, "links_removed": 0,
    }

    user_upsert_sql = """
        INSERT INTO public.sso_user (id, username, firstname, lastname, updated_at)
        VALUES (%s, %s, %s, %s, NOW())
       oliCONFLICT (id) DO UPDATE
        SET username = EXCLUDED.username, firstname = EXCLUDED.firstname, lastname = EXCLUDED.lastname, updated_at = NOW()
        WHERE public.sso_user.username IS DISTINCT FROM EXCLUDED.username
           OR public.sso_user.firstname IS DISTINCT FROM EXCLUDED.firstname
           OR public.sso_user.lastname IS DISTINCT FROM EXCLUDED.lastname
        RETURNING (xmax = 0) AS inserted, (xmax <> 0) AS updated
    """

    group_upsert_sql = """
        INSERT INTO public.sso_group (id, name, description, updated_at)
        VALUES (%s, %s, %s, NOW())
       oliCONFLICT (id) DO UPDATE
        SET name = EXCLUDED.name, description = EXCLUDED.description, updated_at = NOW()
        WHERE public.sso_group.name IS DISTINCT FROM EXCLUDED.name
           OR public.sso_group.description IS DISTINCT FROM EXCLUDED.description
        RETURNING (xmax = 0) AS inserted, (xmax <> 0) AS updated
    """

    link_upsert_sql = """
        INSERT INTO public.user_group (user_id, group_id, updated_at)
        VALUES (%s, %s, NOW())
       oliCONFLICT (user_id, group_id) DO UPDATE
        SET updated_at = NOW()
        WHERE FALSE
        RETURNING 1
    """

    current_links_sql = "SELECT user_id, group_id FROM public.user_group"
    delete_link_sql = "DELETE FROM public.user_group WHERE user_id = %s AND group_id = %s"

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            for user in snapshot["users"]:
                cur.execute(user_upsert_sql, (user["id"], user["username"], user["firstname"], user["lastname"]))
                row = cur.fetchone()
                if row is None: stats["users_unchanged"] += 1
                else:
                    inserted, updated = row
                    if inserted: stats["users_inserted"] += 1
                    elif updated: stats["users_updated"] += 1
                    else: stats["users_unchanged"] += 1

            for group in snapshot["groups"]:
                cur.execute(group_upsert_sql, (group["id"], group["name"], group["description"]))
                row = cur.fetchone()
                if row is None: stats["groups_unchanged"] += 1
                else:
                    inserted, updated = row
                    if inserted: stats["groups_inserted"] += 1
                    elif updated: stats["groups_updated"] += 1
                    else: stats["groups_unchanged"] += 1
            desired_links = set(snapshot["user_groups"])
            cur.execute(current_links_sql)
            existing_links = set((row[0], row[1]) for row in cur.fetchall())
            for user_id, group_id in desired_links:
                cur.execute(link_upsert_sql, (user_id, group_id))
                row = cur.fetchone()
                if row is None: stats["links_unchanged"] += 1
                else: stats["links_inserted"] += 1
            for user_id, group_id in (existing_links - desired_links):
                cur.execute(delete_link_sql, (user_id, group_id))
                stats["links_removed"] += 1

        conn.commit()
    return stats

def main() -> int:
    try:
        snapshot = build_snapshot(); stats = sync_to_db(snapshot); log_summary(status="ok", **stats)
        print(
            "Sync complete: "
            f"users_seen={stats['users_seen']} users_inserted={stats['users_inserted']} users_updated={stats['users_updated']} users_unchanged={stats['users_unchanged']} "
            f"groups_seen={stats['groups_seen']} groups_inserted={stats['groups_inserted']} groups_updated={stats['groups_updated']} groups_unchanged={stats['groups_unchanged']} "
            f"links_seen={stats['links_seen']} links_inserted={stats['links_inserted']} links_unchanged={stats['links_unchanged']} links_removed={stats['links_removed']}"
        )
        return 0
    except requests.RequestException as exc:
        log_summary(status="error", users_seen=0, users_inserted=0, users_updated=0, users_unchanged=0,
                    groups_seen=0, groups_inserted=0, groups_updated=0, groups_unchanged=0,
                    links_seen=0, links_inserted=0, links_unchanged=0, links_removed=0, error=str(exc))
        print(f"HTTP error: {exc}", file=sys.stderr); return 1
    except KeyError as exc:
        log_summary(status="error", users_seen=0, users_inserted=0, users_updated=0, users_unchanged=0,
                    groups_seen=0, groups_inserted=0, groups_updated=0, groups_unchanged=0,
                    links_seen=0, links_inserted=0, links_unchanged=0, links_removed=0,
                    error=f"Missing required environment variable: {exc}")
        print(f"Missing required environment variable: {exc}", file=sys.stderr); return 1
    except Exception as exc:
        log_summary(status="error", users_seen=0, users_inserted=0, users_updated=0, users_unchanged=0,
                    groups_seen=0, groups_inserted=0, groups_updated=0, groups_unchanged=0,
                    links_seen=0, links_inserted=0, links_unchanged=0, links_removed=0, error=str(exc))
        print(f"Error: {exc}", file=sys.stderr); return 1

if __name__ == "__main__": raise SystemExit(main())
