-- 司命（siming）核心数据库结构（SQLite WAL）
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'local',
    cursor TEXT NOT NULL DEFAULT '',
    max_silence_sec INTEGER NOT NULL DEFAULT 3600,
    last_ok_at TEXT,
    last_run_at TEXT,
    last_used_at TEXT,
    auth_state TEXT NOT NULL DEFAULT 'OK',
    freshness TEXT NOT NULL DEFAULT 'UNKNOWN',
    detail TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    thread_key TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    raw_ref TEXT,
    fingerprint TEXT,
    processed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_unprocessed ON events(processed_at, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_thread ON events(thread_key, occurred_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_fingerprint ON events(fingerprint) WHERE fingerprint IS NOT NULL;

CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    next_step TEXT NOT NULL DEFAULT '',
    due_at TEXT,
    done_rule TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '待处理',
    strength TEXT NOT NULL DEFAULT 'SOFT',
    category TEXT NOT NULL DEFAULT 'general',
    rule_id TEXT NOT NULL DEFAULT 'manual',
    state_version INTEGER NOT NULL DEFAULT 1,
    last_notified_hash TEXT,
    evidence_ids TEXT NOT NULL DEFAULT '[]',
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_cases_status ON cases(status, due_at);

CREATE TABLE IF NOT EXISTS waits (
    wait_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'WAITING',
    strength TEXT NOT NULL DEFAULT 'explicit',
    trigger_kind TEXT NOT NULL,
    trigger_expr TEXT NOT NULL,
    probe_interval_sec INTEGER NOT NULL DEFAULT 1800,
    last_probed_at TEXT,
    next_fire_at TEXT,
    expires_at TEXT NOT NULL,
    action_mode TEXT NOT NULL DEFAULT 'remind',
    session_ref TEXT NOT NULL DEFAULT '',
    mandate_json TEXT NOT NULL DEFAULT '{}',
    quote TEXT NOT NULL DEFAULT '',
    case_id TEXT,
    result_summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    fired_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_waits_active ON waits(status, trigger_kind, next_fire_at);

CREATE TABLE IF NOT EXISTS subscriptions (
    sub_key TEXT PRIMARY KEY,
    target_kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'ACTIVE',
    ended_at TEXT,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_subscriptions_target ON subscriptions(target_kind, target_id);

CREATE TABLE IF NOT EXISTS effects (
    effect_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_run_at TEXT NOT NULL,
    last_error TEXT NOT NULL DEFAULT '',
    worker_pid INTEGER,
    unit_name TEXT NOT NULL DEFAULT '',
    attempt_gen INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_effects_pending ON effects(status, next_run_at);

CREATE TABLE IF NOT EXISTS approvals (
    token TEXT PRIMARY KEY,
    bundle_id TEXT NOT NULL DEFAULT '',
    tier TEXT NOT NULL DEFAULT 'A2',
    action TEXT NOT NULL,
    target_id TEXT NOT NULL,
    params_json TEXT NOT NULL,
    params_digest TEXT NOT NULL,
    state_version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'PENDING',
    expires_at TEXT NOT NULL,
    claimed_at TEXT,
    claimer_pid INTEGER,
    claimer_boot_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    applied_at TEXT
);

CREATE TABLE IF NOT EXISTS ledger_txns (
    txn_id TEXT PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'CNY',
    account_alias TEXT NOT NULL,
    card_last4 TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '消费',
    status TEXT NOT NULL DEFAULT 'PENDING',
    merchant TEXT NOT NULL DEFAULT '',
    channel_order_id TEXT NOT NULL DEFAULT '',
    parent_txn_id TEXT,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_account_time ON ledger_txns(account_alias, occurred_at);
CREATE INDEX IF NOT EXISTS idx_ledger_status ON ledger_txns(status, occurred_at);

CREATE TABLE IF NOT EXISTS links (
    txn_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (txn_id, event_id, role)
);

CREATE TABLE IF NOT EXISTS outbox (
    id TEXT PRIMARY KEY,
    ref_id TEXT NOT NULL DEFAULT '',
    rule_id TEXT NOT NULL DEFAULT 'system',
    text TEXT NOT NULL,
    send_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'QUEUED',
    channel TEXT NOT NULL DEFAULT 'qqbot',
    dedup_hash TEXT,
    hard_deadline INTEGER NOT NULL DEFAULT 0,
    edge_synced INTEGER NOT NULL DEFAULT 0,
    sent_msg_id TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status, send_at);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    action TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_rule ON feedback(rule_id, ts);

CREATE TABLE IF NOT EXISTS rule_stats (
    rule_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT 'TRIAL',
    hard_rule INTEGER NOT NULL DEFAULT 0,
    consecutive_dismiss INTEGER NOT NULL DEFAULT 0,
    sent_14d INTEGER NOT NULL DEFAULT 0,
    acted_14d INTEGER NOT NULL DEFAULT 0,
    dismissed_14d INTEGER NOT NULL DEFAULT 0,
    fires_last_hour INTEGER NOT NULL DEFAULT 0,
    hour_bucket TEXT NOT NULL DEFAULT '',
    cooldown_until TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hypotheses (
    id TEXT PRIMARY KEY,
    claim TEXT NOT NULL,
    evidence_ids TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'OBSERVING',
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    model TEXT NOT NULL,
    runner TEXT NOT NULL DEFAULT 'coding_plan',
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_cny REAL NOT NULL DEFAULT 0.0,
    ref_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    tier TEXT NOT NULL,
    action TEXT NOT NULL,
    target_id TEXT NOT NULL DEFAULT '',
    params_digest TEXT NOT NULL,
    approval_ref TEXT NOT NULL DEFAULT '',
    exit_code INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS task_runs (
    run_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'cmd',
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'RUNNING',
    pid INTEGER,
    wrapper_pid INTEGER,
    boot_id TEXT NOT NULL DEFAULT '',
    cwd TEXT NOT NULL DEFAULT '',
    cmd_json TEXT NOT NULL DEFAULT '[]',
    target_pane TEXT NOT NULL DEFAULT '',
    terminal_id TEXT NOT NULL DEFAULT '',
    session_identity TEXT NOT NULL DEFAULT '',
    baseline_seq INTEGER NOT NULL DEFAULT 0,
    blocked_notified_seq INTEGER NOT NULL DEFAULT -1,
    last_agent_status TEXT NOT NULL DEFAULT '',
    exit_code INTEGER,
    log_path TEXT NOT NULL DEFAULT '',
    tail_excerpt TEXT NOT NULL DEFAULT '',
    notify_qq INTEGER NOT NULL DEFAULT 1,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_runs_status ON task_runs(status, kind);

CREATE TABLE IF NOT EXISTS feature_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_id TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'human',
    action TEXT NOT NULL,
    ref_id TEXT NOT NULL DEFAULT '',
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feature_usage_ts ON feature_usage(feature_id, ts);

-- 网购订单商品明细（美团外卖 H5 订单页只读采集；补货雷达按「包装规格 × 份数」推算余量）
CREATE TABLE IF NOT EXISTS shop_order_items (
    item_key TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    shop TEXT NOT NULL DEFAULT '',
    ordered_at TEXT NOT NULL,
    order_status TEXT NOT NULL DEFAULT '',
    order_amount_cents INTEGER NOT NULL DEFAULT 0,
    item_name TEXT NOT NULL,
    qty INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shop_order_items_time ON shop_order_items(platform, ordered_at);

-- 事件链六层引擎：实时情境表（loc:me / eta:* / sleep:state / tesla:main / gate:*）
CREATE TABLE IF NOT EXISTS situation (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL DEFAULT '{}',
    as_of TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'local',
    confidence REAL NOT NULL DEFAULT 1.0
);

-- 事件链六层引擎：统一确定承诺（confidence=1.0）与规律/链预期的时间线表
CREATE TABLE IF NOT EXISTS expectations (
    exp_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    chain_type TEXT NOT NULL DEFAULT 'obligation', -- dependency / habit / obligation
    title TEXT NOT NULL,
    target_at TEXT NOT NULL,
    window_end_at TEXT,
    flex_before_min INTEGER NOT NULL DEFAULT 0,
    flex_after_min INTEGER NOT NULL DEFAULT 0,
    place TEXT NOT NULL DEFAULT '',
    visibility TEXT NOT NULL DEFAULT 'high',       -- high / medium / delayed / blind
    confidence REAL NOT NULL DEFAULT 1.0,
    depends_json TEXT NOT NULL DEFAULT '[]',       -- ["eta:公司", "expectation:E001", "gate:燃气"]
    evidence_rule TEXT NOT NULL DEFAULT '',
    prepare_fn TEXT NOT NULL DEFAULT '',
    prepared_json TEXT NOT NULL DEFAULT '{}',
    remedy_fn TEXT NOT NULL DEFAULT '',
    counterparty_json TEXT NOT NULL DEFAULT '{}',
    source_ref TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ACTIVE',         -- SHADOW / ACTIVE / PREPARED / SHIFTED / MET / MISSED / CANCELLED
    version INTEGER NOT NULL DEFAULT 1,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_expectations_active ON expectations(status, target_at);

-- DeepSeek Super-agent 分拣目标：灵感与开源工具收藏夹（不污染待办 cases）
CREATE TABLE IF NOT EXISTS bookmarks (
    bookmark_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'github',           -- github / article / product / idea
    title TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    repo_slug TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    related_projects_json TEXT NOT NULL DEFAULT '[]',
    tags_json TEXT NOT NULL DEFAULT '[]',
    source_event_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bookmarks_repo ON bookmarks(repo_slug, created_at);

-- 主动求证队列（当司命对陌生停留地点、习惯变化或未知扣款存疑时，低打扰向主人确认并沉淀记忆）
CREATE TABLE IF NOT EXISTS curiosity_questions (
    qid TEXT PRIMARY KEY,
    topic TEXT NOT NULL,                           -- place_dwell / routine_drift / unknown_merchant / trip_intent
    question TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'PENDING',        -- PENDING / ASKED / ANSWERED / DISMISSED
    answer TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    asked_at TEXT,
    answered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_curiosity_status ON curiosity_questions(status, created_at);

-- ==================== SIMING V5: WAVE 1 (SURE + COMPUTE + SUBS + HEALTH + VEHICLE) ====================

CREATE TABLE IF NOT EXISTS sure_sync_map (
    local_kind TEXT NOT NULL,                      -- txn / asset / rule / vehicle
    local_id TEXT NOT NULL,
    sure_id TEXT NOT NULL DEFAULT '',
    sure_account TEXT NOT NULL DEFAULT '',
    payload_hash TEXT NOT NULL DEFAULT '',
    last_synced_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'SYNCED',         -- SYNCED / QUEUED / FAILED
    PRIMARY KEY (local_kind, local_id)
);

CREATE TABLE IF NOT EXISTS sure_rules (
    rule_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    match_pattern TEXT NOT NULL,
    account_name TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    split_ratio REAL NOT NULL DEFAULT 1.0,
    is_recurring INTEGER NOT NULL DEFAULT 0,
    cadence_days INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS compute_assets (
    asset_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,                        -- deepseek / aliyun / kimi / nous / redpill / claude / codex / magpie
    account_label TEXT NOT NULL DEFAULT '',
    currency TEXT NOT NULL DEFAULT 'CNY',
    balance REAL NOT NULL DEFAULT 0.0,
    daily_spend REAL NOT NULL DEFAULT 0.0,
    avg_daily_spend REAL NOT NULL DEFAULT 0.0,
    plan_name TEXT NOT NULL DEFAULT '',
    plan_expires_at TEXT NOT NULL DEFAULT '',
    billing_scope TEXT NOT NULL DEFAULT 'personal',-- work_cw08 / personal
    fallback_model TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'OK',             -- OK / LOW_BALANCE / BURN_SPIKE / EXPIRING / CIRCUIT_OPEN
    meta_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscription_audit (
    sub_id TEXT PRIMARY KEY,
    merchant TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    cadence_days INTEGER NOT NULL DEFAULT 30,
    amount_cents INTEGER NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'CNY',
    occurrences INTEGER NOT NULL DEFAULT 1,
    last_charged_at TEXT NOT NULL DEFAULT '',
    next_expected_at TEXT NOT NULL DEFAULT '',
    prev_amount_cents INTEGER NOT NULL DEFAULT 0,
    price_hike_pct REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'ACTIVE',         -- ACTIVE / ZOMBIE / KEEP / CANCELLED
    is_ongoing INTEGER NOT NULL DEFAULT 1,         -- 1 = 进行中 / 0 = 已停扣/未在进行
    ongoing_source TEXT NOT NULL DEFAULT 'AUTO',   -- AUTO / MANUAL
    sure_rule_id TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sub_audit_next ON subscription_audit(status, next_expected_at);

CREATE TABLE IF NOT EXISTS health_metrics (
    metric_id TEXT PRIMARY KEY,
    measured_at TEXT NOT NULL,
    metric_type TEXT NOT NULL,                     -- weight_kg / glucose / hba1c / alt / ast / uric_acid / ldl / tg / sleep_debt_h
    value_num REAL NOT NULL,
    unit TEXT NOT NULL DEFAULT '',
    ref_range TEXT NOT NULL DEFAULT '',
    flag TEXT NOT NULL DEFAULT 'NORMAL',           -- NORMAL / HIGH / LOW
    source_event_id TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_health_metrics_type ON health_metrics(metric_type, measured_at);

CREATE TABLE IF NOT EXISTS vehicle_records (
    rec_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                            -- charge / etc / parking / insurance / violation / lfp_cal
    occurred_at TEXT NOT NULL,
    amount_cents INTEGER NOT NULL DEFAULT 0,
    scope TEXT NOT NULL DEFAULT 'personal',        -- work / personal
    provider TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}',
    sure_synced INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vehicle_records_kind ON vehicle_records(kind, occurred_at);

-- ==================== SIMING V5: WAVE 2 (SMARTHOME + HOMELAB + BAMBU + PASSPORT) ====================

CREATE TABLE IF NOT EXISTS home_air_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    room TEXT NOT NULL DEFAULT '客厅',
    temp_c REAL NOT NULL DEFAULT 20.0,
    humidity_pct REAL NOT NULL DEFAULT 45.0,
    hcho_mg_m3 REAL NOT NULL DEFAULT 0.0,
    voc_ppb REAL NOT NULL DEFAULT 0.0,
    outdoor_aqi INTEGER NOT NULL DEFAULT 50,
    water_leak INTEGER NOT NULL DEFAULT 0,
    gas_leak INTEGER NOT NULL DEFAULT 0,
    power_ok INTEGER NOT NULL DEFAULT 1,
    fresh_air_mode TEXT NOT NULL DEFAULT 'auto'
);
CREATE INDEX IF NOT EXISTS idx_home_air_time ON home_air_readings(recorded_at);

CREATE TABLE IF NOT EXISTS homelab_nodes (
    node_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                            -- router / nas / modem / vps / backup
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'UNKNOWN',        -- OK / WARN / ERROR / UNKNOWN
    metrics_json TEXT NOT NULL DEFAULT '{}',
    last_checked_at TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS bambu_spools (
    spool_id TEXT PRIMARY KEY,
    material TEXT NOT NULL DEFAULT 'PLA',
    color TEXT NOT NULL DEFAULT '白色',
    brand TEXT NOT NULL DEFAULT 'Bambu',
    initial_grams REAL NOT NULL DEFAULT 1000.0,
    remaining_grams REAL NOT NULL DEFAULT 1000.0,
    drybox_humidity_pct REAL NOT NULL DEFAULT 25.0,
    low_threshold_grams REAL NOT NULL DEFAULT 180.0,
    jd_url TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bambu_jobs (
    job_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'RUNNING',        -- RUNNING / PAUSED / DONE / FAILED
    progress_pct REAL NOT NULL DEFAULT 0.0,
    spool_id TEXT NOT NULL DEFAULT '',
    used_grams REAL NOT NULL DEFAULT 0.0,
    snapshot_path TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    detail TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS home_utilities (
    util_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                            -- water / electric / gas / broadband / property
    account_no TEXT NOT NULL DEFAULT '',
    cycle_days INTEGER NOT NULL DEFAULT 30,
    last_paid_at TEXT NOT NULL DEFAULT '',
    next_due_at TEXT NOT NULL DEFAULT '',
    amount_cents INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS appliance_consumables (
    cons_id TEXT PRIMARY KEY,
    appliance_id TEXT NOT NULL,
    appliance_name TEXT NOT NULL,
    consumable_name TEXT NOT NULL,
    lifespan_days INTEGER NOT NULL DEFAULT 90,
    installed_at TEXT NOT NULL,
    next_replace_at TEXT NOT NULL,
    jd_url TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

-- ==================== SIMING V5: WAVE 3 (LINGER/PI + HINDSIGHT WIKI + M-SIM + LAB) ====================

CREATE TABLE IF NOT EXISTS msim_entries (
    entry_id TEXT PRIMARY KEY,
    problem_family TEXT NOT NULL,
    target_artifact TEXT NOT NULL DEFAULT '',
    source_model TEXT NOT NULL,
    change_rationale TEXT NOT NULL,
    expected_outcome TEXT NOT NULL DEFAULT '',
    measured_score REAL NOT NULL DEFAULT 0.0,
    baseline_score REAL NOT NULL DEFAULT 0.0,
    passed INTEGER NOT NULL DEFAULT 1,
    eval_context TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msim_family ON msim_entries(problem_family, created_at);

CREATE TABLE IF NOT EXISTS wiki_pages (
    slug TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    content_md TEXT NOT NULL DEFAULT '',
    sources_json TEXT NOT NULL DEFAULT '[]',
    conflicts_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL,
    last_distilled_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wiki_conflict_decisions (
    conflict_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'contradiction',
    topic TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL, -- keep_new / keep_old / both_valid / defer
    existing_fact_id TEXT NOT NULL DEFAULT '',
    incoming_fact_id TEXT NOT NULL DEFAULT '',
    winner_fact_id TEXT NOT NULL DEFAULT '',
    loser_fact_id TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    existing_snapshot_json TEXT NOT NULL DEFAULT '{}',
    incoming_snapshot_json TEXT NOT NULL DEFAULT '{}',
    decided_at TEXT NOT NULL,
    snooze_until TEXT
);
CREATE INDEX IF NOT EXISTS idx_wiki_conflict_dec ON wiki_conflict_decisions(decision, decided_at);

CREATE TABLE IF NOT EXISTS wiki_conflict_briefs (
    conflict_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL DEFAULT '',
    is_real_conflict INTEGER NOT NULL DEFAULT 1,
    domain_label TEXT NOT NULL DEFAULT '',
    decision_question TEXT NOT NULL DEFAULT '',
    conflict_point TEXT NOT NULL DEFAULT '',
    old_summary TEXT NOT NULL DEFAULT '',
    new_summary TEXT NOT NULL DEFAULT '',
    old_choice_label TEXT NOT NULL DEFAULT '',
    new_choice_label TEXT NOT NULL DEFAULT '',
    recommendation TEXT NOT NULL DEFAULT 'manual',
    recommendation_reason TEXT NOT NULL DEFAULT '',
    brief_source TEXT NOT NULL DEFAULT 'heuristic',
    model_used TEXT NOT NULL DEFAULT '',
    conflict_snapshot_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS overnight_handoffs (
    handoff_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    host TEXT NOT NULL DEFAULT 'local',
    pane_or_pid TEXT NOT NULL DEFAULT '',
    log_path TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'WATCHING',       -- WATCHING / CHECKPOINTED / RESUMED / DONE / FAILED
    whitelist_json TEXT NOT NULL DEFAULT '[]',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    report_md TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_pain_points (
    pp_id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    topic TEXT NOT NULL,
    keywords_json TEXT NOT NULL DEFAULT '[]',
    verify_cmd_template TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

-- ==================== SIMING V5: WAVE 4 (CONTEXT GRAPH + TRIPS + SOCIAL/BOARDGAMES) ====================

CREATE TABLE IF NOT EXISTS context_graph_nodes (
    node_id TEXT PRIMARY KEY,
    app TEXT NOT NULL,                             -- files / mail / calendar / chat / wiki / sure / msim
    entity_type TEXT NOT NULL DEFAULT 'doc',
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    provenance TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cgraph_app ON context_graph_nodes(app, occurred_at);

CREATE TABLE IF NOT EXISTS context_graph_edges (
    src_id TEXT NOT NULL,
    dst_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (src_id, dst_id, relation)
);

CREATE TABLE IF NOT EXISTS trips (
    trip_id TEXT PRIMARY KEY,
    destination TEXT NOT NULL,
    country TEXT NOT NULL DEFAULT 'CN',
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'domestic',         -- domestic / international
    status TEXT NOT NULL DEFAULT 'PLANNED',        -- PLANNED / ACTIVE / RETURNED / CLOSED
    checklist_json TEXT NOT NULL DEFAULT '[]',
    bookings_json TEXT NOT NULL DEFAULT '[]',
    expenses_json TEXT NOT NULL DEFAULT '[]',
    sure_tag TEXT NOT NULL DEFAULT '',
    cw01_draft_path TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS boardgame_crowdfunds (
    cf_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL DEFAULT '新物集',        -- 新物集 / KS / Gamefound / 闲鱼
    title TEXT NOT NULL,
    pledge_yuan REAL NOT NULL DEFAULT 0.0,
    balance_due_yuan REAL NOT NULL DEFAULT 0.0,
    balance_due_at TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PLEDGED',        -- PLEDGED / BALANCE_DUE / SHIPPED / ARRIVED / CANCELLED
    notes TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS social_commitments (
    commit_id TEXT PRIMARY KEY,
    person TEXT NOT NULL,
    promise TEXT NOT NULL,
    due_at TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'OPEN',           -- OPEN / DONE / CANCELLED
    source_quote TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS friend_profiles (
    friend_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    encrypted_notes_blob TEXT NOT NULL DEFAULT '',
    birthday TEXT NOT NULL DEFAULT '',
    dietary_tags_json TEXT NOT NULL DEFAULT '[]',
    favorite_games_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);
