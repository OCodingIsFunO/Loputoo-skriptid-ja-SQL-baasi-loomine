#!/opt/zabbix-host/venv/bin/python
import os
import sys
import json
import re
import logging
from datetime import datetime, timezone

import requests
import psycopg
from dotenv import load_dotenv

ENV_FILE = "/opt/zabbix-host/.env"
load_dotenv(ENV_FILE)

ZABBIX_URL = os.getenv("ZABBIX_URL")
ZABBIX_TOKEN = os.getenv("ZABBIX_TOKEN")

DB_HOST = os.environ["DB_HOST"]
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ["DB_NAME"]
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]

LOG_DIR = os.getenv("LOG_DIR", "/opt/zabbix-host/logs")

if not ZABBIX_URL or not ZABBIX_TOKEN:
    print("Missing ZABBIX_URL or ZABBIX_TOKEN in .env", file=sys.stderr)
    sys.exit(1)

os.makedirs(LOG_DIR, exist_ok=True)

log_filename = datetime.now().strftime("%m%Y.log")
log_path = os.path.join(LOG_DIR, log_filename)

logging.basicConfig(filename=log_path, level=logging.INFO, format="%(message)s")

HEADERS = {
    "Content-Type": "application/json-rpc",
    "Authorization": f"Bearer {ZABBIX_TOKEN}",
}


def log_event(
    *,
    action: str,
    entity: str,
    host_id: int | None = None,
    host: str | None = None,
    target: str | None = None,
    details: str | None = None,
) -> None:
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    parts = [ts, "source=zabbix", f"action={action}", f"entity={entity}"]
    if host_id is not None:
        parts.append(f"host_id={host_id}")
    if host:
        parts.append(f'host="{host.replace(chr(34), chr(39))}"')
    if target:
        parts.append(f'target="{target.replace(chr(34), chr(39))}"')
    if details:
        parts.append(f'details="{details.replace(chr(34), chr(39))}"')
    logging.info(" ".join(parts))


def log_summary(
    *,
    status: str,
    seen: int,
    inserted: int,
    updated: int,
    unchanged: int,
    ip_inserted: int,
    ip_deleted: int,
    services_inserted: int,
    host_service_links_inserted: int,
    error: str | None = None,
) -> None:
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    parts = [
        ts,
        "source=zabbix",
        "action=summary",
        f"status={status}",
        f"seen={seen}",
        f"inserted={inserted}",
        f"updated={updated}",
        f"unchanged={unchanged}",
        f"ip_inserted={ip_inserted}",
        f"ip_deleted={ip_deleted}",
        f"services_inserted={services_inserted}",
        f"host_service_links_inserted={host_service_links_inserted}",
    ]
    if error:
        parts.append(f'error="{error.replace(chr(34), chr(39))}"')
    logging.info(" ".join(parts))


def normalize_text(value: str | None) -> str:
    if value is None:
        return ""
    return str(value).strip()


def extract_platform(uname_value: str | None) -> str:
    value = normalize_text(uname_value)
    if not value:
        return "Unknown"
    parts = value.split()
    return parts[0] if parts else "Unknown"


def load_service_keywords() -> list[str]:
    return [
        keyword.strip().lower()
        for keyword in os.getenv("SERVICE_KEYWORDS", "").split(",")
        if keyword.strip()
    ]


def extract_service_name(item: dict, keywords: list[str]) -> str | None:
    item_name = normalize_text(item.get("name"))
    if not item_name or not keywords:
        return None

    item_name_lower = item_name.lower()
    keywords_lower = [k.lower() for k in keywords if k]

    if item_name_lower.startswith("state of service") and any(
        keyword in item_name_lower for keyword in keywords_lower
    ):
        match = re.search(r"^State of service\b.*\(([^()]*)\)\s*$", item_name)
        if match:
            service = normalize_text(match.group(1))
            return service.lower() if service else None

    if any(keyword in item_name_lower for keyword in keywords_lower):
        first_word = normalize_text(item_name.split()[0]).lower()
        cleaned = re.match(r"^[a-z0-9]+", first_word)
        if cleaned:
            result = cleaned.group(0)
            return result if len(result) > 4 else None

    return None


def fetch_hosts() -> list[dict]:
    payload = {
        "jsonrpc": "2.0",
        "method": "host.get",
        "params": {
            "output": ["hostid", "name", "host"],
            "selectInterfaces": ["ip", "main"],
            "selectItems": ["name", "key_", "lastvalue"],
        },
        "id": 1,
    }

    response = requests.post(ZABBIX_URL, headers=HEADERS, json=payload, timeout=30)
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


def pick_environment(items: list[dict]) -> str:
    if not items:
        return "Unknown"

    for item in items:
        if item.get("key_") == "system.uname":
            return extract_platform(item.get("lastvalue"))

    return "Unknown"


def sync_services_for_host(
    cur: psycopg.Cursor,
    host_id: int,
    host_name: str,
    items: list[dict],
    keywords: list[str],
    stats: dict,
) -> None:
    if not keywords or not items:
        return

    service_names = set()

    for item in items:
        service_name = extract_service_name(item, keywords)
        if not service_name:
            continue
        service_names.add(service_name)

    if not service_names:
        return

    service_upsert_sql = """
        INSERT INTO public.service (
            name,
            description,
            updated_at
        )
        VALUES (%s, %s, NOW())
        ON CONFLICT (name) DO UPDATE
        SET updated_at = NOW()
        RETURNING id, (xmax = 0) AS inserted
    """

    host_service_insert_sql = """
        INSERT INTO public.host_service (
            host_id,
            service_id,
            updated_at
        )
        VALUES (%s, %s, NOW())
        ON CONFLICT (host_id, service_id) DO NOTHING
        RETURNING id
    """

    for service_name in sorted(service_names):
        cur.execute(service_upsert_sql, (service_name, None))
        service_id, service_inserted = cur.fetchone()

        if service_inserted:
            stats["services_inserted"] += 1
            log_event(
                action="insert",
                entity="service",
                host_id=host_id,
                host=host_name,
                target="public.service",
                details=service_name,
            )
        else:
            log_event(
                action="skip",
                entity="service",
                host_id=host_id,
                host=host_name,
                target="public.service",
                details=f"exists:{service_name}",
            )

        cur.execute(host_service_insert_sql, (host_id, service_id))
        link_row = cur.fetchone()

        if link_row is not None:
            stats["host_service_links_inserted"] += 1
            log_event(
                action="insert",
                entity="host_service",
                host_id=host_id,
                host=host_name,
                target="public.host_service",
                details=f"service_id={service_id}",
            )
        else:
            log_event(
                action="skip",
                entity="host_service",
                host_id=host_id,
                host=host_name,
                target="public.host_service",
                details=f"service_id={service_id}",
            )


def sync_host_ips(
    cur: psycopg.Cursor,
    host_id: int,
    host_name: str,
    ips: list[str],
    stats: dict,
) -> None:
    select_existing_sql = """
        SELECT host(ip)
        FROM public.host_ip
        WHERE host_id = %s
    """

    insert_ip_sql = """
        INSERT INTO public.host_ip (
            host_id,
            ip,
            updated_at
        )
        VALUES (%s, %s::inet, NOW())
        ON CONFLICT (host_id, ip) DO NOTHING
        RETURNING id
    """

    delete_ip_sql = """
        DELETE FROM public.host_ip
        WHERE host_id = %s
          AND ip = %s::inet
    """

    cur.execute(select_existing_sql, (host_id,))
    existing_ips = {normalize_text(row[0]) for row in cur.fetchall()}
    desired_ips = {normalize_text(ip) for ip in ips}

    for ip in sorted(desired_ips - existing_ips):
        cur.execute(insert_ip_sql, (host_id, ip))
        row = cur.fetchone()
        if row is not None:
            stats["ip_inserted"] += 1
            log_event(
                action="insert",
                entity="host_ip",
                host_id=host_id,
                host=host_name,
                target="public.host_ip",
                details=ip,
            )

    for ip in sorted(existing_ips & desired_ips):
        log_event(
            action="skip",
            entity="host_ip",
            host_id=host_id,
            host=host_name,
            target="public.host_ip",
            details=f"exists:{ip}",
        )

    for ip in sorted(existing_ips - desired_ips):
        cur.execute(delete_ip_sql, (host_id, ip))
        stats["ip_deleted"] += 1
        log_event(
            action="delete",
            entity="host_ip",
            host_id=host_id,
            host=host_name,
            target="public.host_ip",
            details=ip,
        )


def save_hosts_to_db(hosts: list[dict]) -> dict:
    conninfo = (
        f"host={DB_HOST} "
        f"port={DB_PORT} "
        f"dbname={DB_NAME} "
        f"user={DB_USER} "
        f"password={DB_PASSWORD}"
    )

    stats = {
        "seen": len(hosts),
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "ip_inserted": 0,
        "ip_deleted": 0,
        "services_inserted": 0,
        "host_service_links_inserted": 0,
    }

    keywords = load_service_keywords()

    upsert_host_sql = """
        INSERT INTO public.host (
            id,
            name,
            host,
            environment,
            active,
            updated_at
        )
        VALUES (%s, %s, %s, %s, TRUE, NOW())
        ON CONFLICT (id) DO UPDATE
        SET
            name = EXCLUDED.name,
            host = EXCLUDED.host,
            environment = EXCLUDED.environment,
            active = TRUE,
            updated_at = NOW()
        WHERE
            public.host.name IS DISTINCT FROM EXCLUDED.name
            OR public.host.host IS DISTINCT FROM EXCLUDED.host
            OR public.host.environment IS DISTINCT FROM EXCLUDED.environment
            OR public.host.active IS DISTINCT FROM TRUE
        RETURNING (xmax = 0) AS inserted, (xmax <> 0) AS updated
    """

    with psycopg.connect(conninfo) as conn:
        with conn.cursor() as cur:
            for host_obj in hosts:
                ips = pick_host_ips(host_obj.get("interfaces", []))
                if not ips:
                    log_event(
                        action="skip",
                        entity="host",
                        host_id=int(host_obj["hostid"]),
                        host=normalize_text(host_obj.get("host")),
                        target="public.host",
                        details="no_valid_ips",
                    )
                    continue

                zabbix_host_id = int(host_obj["hostid"])
                name = normalize_text(host_obj.get("name"))
                technical_host = normalize_text(host_obj.get("host"))
                items = host_obj.get("items", [])
                environment = pick_environment(items)

                cur.execute(
                    upsert_host_sql,
                    (zabbix_host_id, name, technical_host, environment),
                )
                row = cur.fetchone()

                if row is None:
                    stats["unchanged"] += 1
                    log_event(
                        action="skip",
                        entity="host",
                        host_id=zabbix_host_id,
                        host=technical_host,
                        target="public.host",
                        details=f"unchanged environment={environment}",
                    )
                else:
                    inserted, updated = row
                    if inserted:
                        stats["inserted"] += 1
                        log_event(
                            action="insert",
                            entity="host",
                            host_id=zabbix_host_id,
                            host=technical_host,
                            target="public.host",
                            details=f"name={name}, environment={environment}",
                        )
                    elif updated:
                        stats["updated"] += 1
                        log_event(
                            action="update",
                            entity="host",
                            host_id=zabbix_host_id,
                            host=technical_host,
                            target="public.host",
                            details=f"name={name}, environment={environment}",
                        )
                    else:
                        stats["unchanged"] += 1
                        log_event(
                            action="skip",
                            entity="host",
                            host_id=zabbix_host_id,
                            host=technical_host,
                            target="public.host",
                            details=f"unchanged environment={environment}",
                        )

                sync_host_ips(
                    cur=cur,
                    host_id=zabbix_host_id,
                    host_name=technical_host,
                    ips=ips,
                    stats=stats,
                )

                sync_services_for_host(
                    cur=cur,
                    host_id=zabbix_host_id,
                    host_name=technical_host,
                    items=items,
                    keywords=keywords,
                    stats=stats,
                )

        conn.commit()

    return stats


def main() -> int:
    try:
        hosts = fetch_hosts()
        stats = save_hosts_to_db(hosts)
        log_summary(status="ok", **stats)
        print(
            "Sync complete: "
            f"seen={stats['seen']} "
            f"inserted={stats['inserted']} "
            f"updated={stats['updated']} "
            f"unchanged={stats['unchanged']} "
            f"ip_inserted={stats['ip_inserted']} "
            f"ip_deleted={stats['ip_deleted']} "
            f"services_inserted={stats['services_inserted']} "
            f"host_service_links_inserted={stats['host_service_links_inserted']}"
        )
        return 0
    except requests.RequestException as exc:
        log_summary(
            status="error",
            seen=0,
            inserted=0,
            updated=0,
            unchanged=0,
            ip_inserted=0,
            ip_deleted=0,
            services_inserted=0,
            host_service_links_inserted=0,
            error=str(exc),
        )
        print(f"HTTP error: {exc}", file=sys.stderr)
        return 1
    except KeyError as exc:
        log_summary(
            status="error",
            seen=0,
            inserted=0,
            updated=0,
            unchanged=0,
            ip_inserted=0,
            ip_deleted=0,
            services_inserted=0,
            host_service_links_inserted=0,
            error=f"Missing required environment variable: {exc}",
        )
        print(f"Missing required environment variable: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        log_summary(
            status="error",
            seen=0,
            inserted=0,
            updated=0,
            unchanged=0,
            ip_inserted=0,
            ip_deleted=0,
            services_inserted=0,
            host_service_links_inserted=0,
            error=str(exc),
        )
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
