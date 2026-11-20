"""Verify the structures shared by the two semifinal database histories."""

SCHEMA_LOCK = 8123419426
SEMIFINAL_VERSION = 26
SEMIFINAL_SCRIPT = "026_semifinal_schema.sql"
REQUIRED_VERSIONS = {
    5,
    7,
    8,
    10,
    11,
    12,
    13,
    14,
    16,
    17,
    18,
    19,
    20,
    21,
    22,
    23,
    24,
    25,
    26,
    27,
    28,
}

_COLUMNS = {
    "workbook_snapshots": {"version_id": "uuid", "snapshot": "jsonb"},
    "workbook_results": {
        "result_id": "uuid",
        "run_id": "uuid",
        "version_id": "uuid",
        "query": "jsonb",
        "result": "jsonb",
    },
    "knowledge_strategies": {"company_id": "text", "revision": "integer"},
    "knowledge_strategy_versions": {
        "company_id": "text",
        "version": "integer",
        "config": "jsonb",
        "actor_id": "text",
        "created_at": "timestamp with time zone",
    },
    "write_plans": {"task_id": "uuid", "task_generation": "bigint", "task_period": "text"},
    "summaries": {"agent_scope": "text"},
    "schema_migrations": {"script_name": "text"},
    "default_model_versions": {
        "version": "bigint",
        "source": "text",
        "model_id": "text",
        "config_fingerprint": "text",
    },
    "turns": {"default_model_version": "bigint"},
}

_CONSTRAINTS = {
    "workbook_snapshots_pkey": ("workbook_snapshots", "PRIMARY KEY (version_id)"),
    "workbook_results_pkey": ("workbook_results", "PRIMARY KEY (result_id)"),
    "workbook_snapshots_version_id_fkey": (
        "workbook_snapshots",
        "FOREIGN KEY (version_id) REFERENCES knowledge_versions(version_id) ON DELETE CASCADE",
    ),
    "workbook_results_run_id_fkey": (
        "workbook_results",
        "FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE",
    ),
    "workbook_results_version_id_fkey": (
        "workbook_results",
        "FOREIGN KEY (version_id) REFERENCES workbook_snapshots(version_id) ON DELETE CASCADE",
    ),
    "knowledge_strategies_pkey": ("knowledge_strategies", "PRIMARY KEY (company_id)"),
    "knowledge_strategies_company_id_fkey": (
        "knowledge_strategies",
        "FOREIGN KEY (company_id) REFERENCES companies(company_id) ON DELETE CASCADE",
    ),
    "knowledge_strategies_revision_check": ("knowledge_strategies", "CHECK ((revision > 0))"),
    "knowledge_strategy_versions_pkey": (
        "knowledge_strategy_versions",
        "PRIMARY KEY (company_id, version)",
    ),
    "knowledge_strategy_versions_company_id_fkey": (
        "knowledge_strategy_versions",
        "FOREIGN KEY (company_id) REFERENCES knowledge_strategies(company_id) ON DELETE CASCADE",
    ),
    "knowledge_strategy_versions_actor_id_fkey": (
        "knowledge_strategy_versions",
        "FOREIGN KEY (actor_id) REFERENCES users(user_id)",
    ),
    "knowledge_strategy_versions_version_check": (
        "knowledge_strategy_versions",
        "CHECK ((version > 0))",
    ),
    "write_plans_task_id_fkey": ("write_plans", "FOREIGN KEY (task_id) REFERENCES tasks(task_id)"),
    "write_plan_task_scope": (
        "write_plans",
        "CHECK ((((task_id IS NULL) AND (task_generation IS NULL) AND (task_period IS NULL)) OR "
        "((task_id IS NOT NULL) AND (task_generation IS NOT NULL) AND (task_period IS NOT NULL))))",
    ),
    "schema_migrations_script_name_key": ("schema_migrations", "UNIQUE (script_name)"),
    "default_model_versions_pkey": ("default_model_versions", "PRIMARY KEY (version)"),
    "turns_default_model_version_fkey": (
        "turns",
        "FOREIGN KEY (default_model_version) REFERENCES default_model_versions(version)",
    ),
}

_INDEXES = {
    "workbook_results_run": ("workbook_results", "btree (run_id)"),
    "write_plans_task_generation": (
        "write_plans",
        "btree (task_id, task_generation) WHERE (task_id IS NOT NULL)",
    ),
    "summaries_agent_scope": (
        "summaries",
        "btree (user_id, session_id, scope, agent_scope, memory_epoch, generation DESC)",
    ),
}


def migration_ledger(conn):
    if not conn.execute("SELECT to_regclass('public.schema_migrations') AS ledger").fetchone()[
        "ledger"
    ]:
        return {}
    return {
        row["version"]: row["script_name"]
        for row in conn.execute(
            "SELECT version,to_jsonb(m)->>'script_name' AS script_name FROM public.schema_migrations m"
        )
    }


def semifinal_schema_issues(conn):
    columns = {
        (row["table_name"], row["column_name"]): (
            row["data_type"],
            row["required"],
            row["default_value"],
        )
        for row in conn.execute(
            "SELECT r.relname AS table_name,a.attname AS column_name,"
            "format_type(a.atttypid,a.atttypmod) AS data_type,a.attnotnull AS required,"
            "pg_get_expr(d.adbin,d.adrelid) AS default_value "
            "FROM pg_attribute a JOIN pg_class r ON r.oid=a.attrelid "
            "JOIN pg_namespace n ON n.oid=r.relnamespace "
            "LEFT JOIN pg_attrdef d ON d.adrelid=r.oid AND d.adnum=a.attnum "
            "WHERE n.nspname='public' AND r.relkind='r' AND r.relname=ANY(%s) "
            "AND a.attnum>0 AND NOT a.attisdropped",
            (list(_COLUMNS),),
        )
    }
    issues = []
    for table, fields in _COLUMNS.items():
        for field, data_type in fields.items():
            required = table not in ("write_plans", "schema_migrations", "turns")
            default = {"agent_scope": "''::text", "created_at": "clock_timestamp()"}.get(field)
            if columns.get((table, field)) != (data_type, required, default):
                issues.append(f"{table}.{field}")
    constraints = {
        (row["table_name"], row["conname"]): row["definition"]
        for row in conn.execute(
            "SELECT p.conname,r.relname AS table_name,pg_get_constraintdef(p.oid) AS definition "
            "FROM pg_constraint p JOIN pg_class r ON r.oid=p.conrelid "
            "JOIN pg_namespace n ON n.oid=r.relnamespace "
            "WHERE n.nspname='public' AND p.convalidated AND p.conname=ANY(%s)",
            (list(_CONSTRAINTS),),
        )
    }
    indexes = {
        row["index_name"]: (row["table_name"], row["definition"].partition(" USING ")[2])
        for row in conn.execute(
            "SELECT i.relname AS index_name,r.relname AS table_name,pg_get_indexdef(i.oid) AS definition "
            "FROM pg_index p JOIN pg_class i ON i.oid=p.indexrelid "
            "JOIN pg_class r ON r.oid=p.indrelid JOIN pg_namespace n ON n.oid=r.relnamespace "
            "WHERE n.nspname='public' AND p.indisvalid AND i.relname=ANY(%s)",
            (list(_INDEXES),),
        )
    }
    issues.extend(
        name
        for name, (table, definition) in _CONSTRAINTS.items()
        if constraints.get((table, name)) != definition
    )
    issues.extend(name for name, definition in _INDEXES.items() if indexes.get(name) != definition)
    active_index = conn.execute(
        "SELECT pg_get_indexdef(indexrelid) AS definition FROM pg_index "
        "WHERE indrelid=to_regclass('public.tasks') "
        "AND indexrelid=to_regclass('public.tasks_unfinished_scope') "
        "AND indisvalid AND indisunique AND indnullsnotdistinct"
    ).fetchone()
    if not active_index or active_index["definition"].partition(" USING ")[2] != (
        "btree (user_id, company_id, intent, period) NULLS NOT DISTINCT "
        "WHERE (status = ANY (ARRAY['active'::text, 'waiting_user'::text]))"
    ):
        issues.append("tasks_unfinished_scope")
    if conn.execute("SELECT to_regclass('public.tasks_one_unfinished') AS legacy").fetchone()[
        "legacy"
    ]:
        issues.append("tasks_one_unfinished")
    return issues
