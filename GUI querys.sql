HiddenServiceDelete
DELETE FROM public.hidden_service
WHERE host_id = {{ detail_service.updatedRow.host_id }}
  AND original_service_id = {{ detail_service.updatedRow.original_service_id }};

HiddenserviceInsert
INSERT INTO public.hidden_service (
    host_id,
    original_service_id,
    updated_at
)
VALUES (
    {{ detail_service.updatedRow.host_id }},
    {{ detail_service.updatedRow.original_service_id }},
    NOW()
)
ON CONFLICT (host_id, original_service_id) DO UPDATE
SET
    updated_at = NOW();

OverrideserviceUpsert
INSERT INTO public.override_service (
    host_id,
    original_service_id,
    name,
    description,
    updated_at
)
VALUES (
    {{ detail_service.updatedRow.host_id }},
    {{ detail_service.updatedRow.original_service_id }},
    {{ detail_service.updatedRow.service_name }},
    {{ detail_service.updatedRow.service_description || null }},
    NOW()
)
ON CONFLICT (host_id, original_service_id) DO UPDATE
SET
    name = EXCLUDED.name,
    description = EXCLUDED.description,
    updated_at = NOW();


OverrideServiceDelete
DELETE FROM public.override_service
WHERE host_id = {{ detail_service.updatedRow.host_id }}
  AND original_service_id = {{ detail_service.updatedRow.original_service_id }};
RowAccessGroup
SELECT
    g.id,
    g.name,
    g.description
FROM public.group_host_access gha
JOIN public.sso_group g
    ON g.id = gha.group_id
WHERE gha.host_id = {{ appsmith.store.selectedHostId }};

RowHost
SELECT
    h.id,
    h.name,
    h.host AS technical_host,
    h.environment
FROM public.host h
WHERE h.id = {{hosts.triggeredRow.id}};

RowIP
SELECT
    hip.id,
    hip.ip
FROM public.host_ip hip
WHERE hip.host_id = {{hosts.triggeredRow.id}}
ORDER BY hip.ip;

RowService
SELECT
    host_id,
    original_service_id,
    override_service_id,
    user_added_service_id,
    service_name,
    service_description,
    source_type,
    remove
FROM public.host_service_view
WHERE host_id = {{ hosts.triggeredRow.id }}
ORDER BY service_name;
UserAddedServiceDelete
DELETE FROM public.user_added_service
WHERE id = {{ detail_service.updatedRow.user_added_service_id }};

UserAddedServiceInsert
INSERT INTO public.user_added_service (
    host_id,
    name,
    description,
    updated_at
)
VALUES (
    {{ appsmith.store.selectedHostId }},
    {{ detail_service.newRow.service_name }},
    {{ detail_service.newRow.service_description || null }},
    NOW()
);

UserAddedServiceUpdate
UPDATE public.user_added_service
SET
    name = {{ detail_service.updatedRow.service_name }},
    description = {{ detail_service.updatedRow.service_description || null }},
    updated_at = NOW()
WHERE id = {{ detail_service.updatedRow.user_added_service_id }};

host_service
SELECT
    h.id,
    h.name,
    h.host AS technical_host,
    h.ip,
    h.environment,
    COALESCE(
        string_agg(DISTINCT hsv.service_name, E'\n' ORDER BY hsv.service_name)
        FILTER (WHERE hsv.remove = FALSE),
        ''
    ) AS services,
    COALESCE(string_agg(DISTINCT g.name, E'\n' ORDER BY g.name), '') AS groups_with_access
FROM public.host h
LEFT JOIN public.host_service_view hsv
    ON hsv.host_id = h.id
LEFT JOIN public.group_host_access gha
    ON gha.host_id = h.id
LEFT JOIN public.sso_group g
    ON g.id = gha.group_id
GROUP BY
    h.id, h.name, h.host, h.ip, h.environment
ORDER BY
    h.name;

Users
SELECT
    u.username,
    u.firstname,
    u.lastname,
    COALESCE(string_agg(DISTINCT g.name, ', ' ORDER BY g.name), '') AS groups,
    COALESCE(string_agg(DISTINCT h.name, ', ' ORDER BY h.name), '') AS accessible_hosts
FROM sso_user u
LEFT JOIN user_group ug
    ON ug.user_id = u.id
LEFT JOIN sso_group g
    ON g.id = ug.group_id
LEFT JOIN group_host_access gha
    ON gha.group_id = g.id
LEFT JOIN host h
    ON h.id = gha.host_id
GROUP BY
    u.id, u.username, u.firstname, u.lastname
ORDER BY
    u.username;
