CREATE TABLE public.host (
    id          bigint NOT NULL,
    name        text NOT NULL,
    host        text NOT NULL,
    environment text NOT NULL DEFAULT 'unknown',
    updated_at  timestamp with time zone NOT NULL DEFAULT now(),
    active      boolean NOT NULL DEFAULT true,
    CONSTRAINT host_pkey PRIMARY KEY (id)
);

CREATE TABLE public.service (
    id          bigserial NOT NULL,
    name        text NOT NULL,
    description text,
    updated_at  timestamp with time zone NOT NULL DEFAULT now(),
    active      boolean NOT NULL DEFAULT true,
    CONSTRAINT service_pkey PRIMARY KEY (id),
    CONSTRAINT service_name_key UNIQUE (name)
);

CREATE TABLE public.sso_group (
    id          text NOT NULL,
    name        text NOT NULL,
    description text,
    updated_at  timestamp with time zone NOT NULL DEFAULT now(),
    active      boolean NOT NULL DEFAULT true,
    CONSTRAINT sso_group_pkey PRIMARY KEY (id),
    CONSTRAINT sso_group_name_key UNIQUE (name)
);

CREATE TABLE public.sso_user (
    id         text NOT NULL,
    username   text NOT NULL,
    firstname  text NOT NULL,
    lastname   text NOT NULL,
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    active     boolean NOT NULL DEFAULT true,
    CONSTRAINT sso_user_pkey PRIMARY KEY (id),
    CONSTRAINT sso_user_username_key UNIQUE (username)
);

CREATE TABLE public.group_host_access (
    id         bigserial NOT NULL,
    host_id    bigint NOT NULL,
    group_id   text NOT NULL,
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT group_host_access_pkey PRIMARY KEY (id),
    CONSTRAINT uq_group_host_access UNIQUE (host_id, group_id),
    CONSTRAINT fk_group_host_access_group
        FOREIGN KEY (group_id) REFERENCES public.sso_group(id) ON DELETE CASCADE,
    CONSTRAINT fk_group_host_access_host
        FOREIGN KEY (host_id) REFERENCES public.host(id) ON DELETE CASCADE
);

CREATE TABLE public.hidden_service (
    id                  bigserial NOT NULL,
    host_id             bigint NOT NULL,
    original_service_id bigint NOT NULL,
    updated_at          timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT hidden_service_pkey PRIMARY KEY (id),
    CONSTRAINT uq_hidden_service_host_original UNIQUE (host_id, original_service_id),
    CONSTRAINT fk_hidden_service_host
        FOREIGN KEY (host_id) REFERENCES public.host(id) ON DELETE CASCADE,
    CONSTRAINT fk_hidden_service_original_service
        FOREIGN KEY (original_service_id) REFERENCES public.service(id) ON DELETE CASCADE
);

CREATE TABLE public.host_ip (
    id         bigserial NOT NULL,
    host_id    bigint NOT NULL,
    ip         inet NOT NULL,
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    active     boolean NOT NULL DEFAULT true,
    CONSTRAINT host_ip_pkey PRIMARY KEY (id),
    CONSTRAINT uq_host_ip UNIQUE (host_id, ip),
    CONSTRAINT fk_host_ip_host
        FOREIGN KEY (host_id) REFERENCES public.host(id) ON DELETE CASCADE
);

CREATE TABLE public.host_service (
    id         bigserial NOT NULL,
    host_id    bigint NOT NULL,
    service_id bigint NOT NULL,
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT host_service_pkey PRIMARY KEY (id),
    CONSTRAINT uq_host_service UNIQUE (host_id, service_id),
    CONSTRAINT fk_host_service_host
        FOREIGN KEY (host_id) REFERENCES public.host(id) ON DELETE CASCADE,
    CONSTRAINT fk_host_service_service
        FOREIGN KEY (service_id) REFERENCES public.service(id) ON DELETE CASCADE
);

CREATE TABLE public.override_service (
    id                  bigserial NOT NULL,
    host_id             bigint NOT NULL,
    original_service_id bigint NOT NULL,
    name                text NOT NULL,
    description         text,
    updated_at          timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT override_service_pkey PRIMARY KEY (id),
    CONSTRAINT uq_override_service_host_original UNIQUE (host_id, original_service_id),
    CONSTRAINT fk_override_service_host
        FOREIGN KEY (host_id) REFERENCES public.host(id) ON DELETE CASCADE,
    CONSTRAINT fk_override_service_original_service
        FOREIGN KEY (original_service_id) REFERENCES public.service(id) ON DELETE CASCADE
);

CREATE TABLE public.user_added_service (
    id          bigserial NOT NULL,
    host_id     bigint NOT NULL,
    name        text NOT NULL,
    description text,
    updated_at  timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT user_added_service_pkey PRIMARY KEY (id),
    CONSTRAINT fk_user_added_service_host
        FOREIGN KEY (host_id) REFERENCES public.host(id) ON DELETE CASCADE
);

CREATE TABLE public.user_group (
    id         bigserial NOT NULL,
    user_id    text NOT NULL,
    group_id   text NOT NULL,
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT user_group_pkey PRIMARY KEY (id),
    CONSTRAINT uq_user_group UNIQUE (user_id, group_id),
    CONSTRAINT fk_user_group_group
        FOREIGN KEY (group_id) REFERENCES public.sso_group(id) ON DELETE CASCADE,
    CONSTRAINT fk_user_group_user
        FOREIGN KEY (user_id) REFERENCES public.sso_user(id) ON DELETE CASCADE
);

CREATE INDEX idx_group_host_access_group_id
    ON public.group_host_access USING btree (group_id);

CREATE INDEX idx_group_host_access_host_id
    ON public.group_host_access USING btree (host_id);

CREATE INDEX idx_hidden_service_host_id
    ON public.hidden_service USING btree (host_id);

CREATE INDEX idx_hidden_service_original_service_id
    ON public.hidden_service USING btree (original_service_id);

CREATE INDEX idx_host_ip_host_id
    ON public.host_ip USING btree (host_id);

CREATE INDEX idx_host_service_host_id
    ON public.host_service USING btree (host_id);

CREATE INDEX idx_host_service_service_id
    ON public.host_service USING btree (service_id);

CREATE INDEX idx_override_service_host_id
    ON public.override_service USING btree (host_id);

CREATE INDEX idx_override_service_original_service_id
    ON public.override_service USING btree (original_service_id);

CREATE INDEX idx_user_added_service_host_id
    ON public.user_added_service USING btree (host_id);

CREATE INDEX idx_user_group_group_id
    ON public.user_group USING btree (group_id);

CREATE INDEX idx_user_group_user_id
    ON public.user_group USING btree (user_id);
