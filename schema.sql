-- EVE ESI 持久化数据库表结构
-- 用法: mysql -u eve_esi -p eve_esi < schema.sql
-- 或通过程序自动初始化 (main.py --init-db)
-- 时区：所有时间列统一存 UTC+8（北京时间）的裸时间

-- 角色表
CREATE TABLE IF NOT EXISTS characters (
    character_id BIGINT PRIMARY KEY,
    character_name VARCHAR(64) NOT NULL,
    scopes TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- OAuth token 表（每个角色一条，支持多角色）
CREATE TABLE IF NOT EXISTS oauth_tokens (
    character_id BIGINT PRIMARY KEY,
    access_token TEXT NOT NULL,
    refresh_token TEXT,
    token_type VARCHAR(16),
    expires_at BIGINT NOT NULL COMMENT 'epoch 秒',
    scope TEXT,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_tokens_char FOREIGN KEY (character_id)
        REFERENCES characters(character_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 钱包变动流水表（持久化历史记录）
CREATE TABLE IF NOT EXISTS wallet_journal (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    character_id BIGINT NOT NULL,
    ref_id BIGINT NOT NULL COMMENT 'ESI journal ref_id',
    journal_date DATETIME NULL COMMENT 'UTC+8 北京时间',
    amount DECIMAL(20,2) NULL,
    balance DECIMAL(20,2) NULL,
    description VARCHAR(512) NULL,
    first_party_id BIGINT NULL,
    second_party_id BIGINT NULL,
    tax DECIMAL(20,2) NULL,
    reason VARCHAR(512) NULL,
    context_id BIGINT NULL,
    context_id_type VARCHAR(64) NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_char_ref (character_id, ref_id),
    KEY idx_char_date (character_id, journal_date),
    CONSTRAINT fk_journal_char FOREIGN KEY (character_id)
        REFERENCES characters(character_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 钱包余额历史快照表（每次查询记录一条）
CREATE TABLE IF NOT EXISTS wallet_balance (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    character_id BIGINT NOT NULL,
    balance DECIMAL(20,2) NOT NULL,
    recorded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT 'UTC+8 北京时间',
    KEY idx_char_time (character_id, recorded_at),
    CONSTRAINT fk_balance_char FOREIGN KEY (character_id)
        REFERENCES characters(character_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- EVE 物品类型表（type_id -> 名称，用于交易详情翻译与「查价」模糊匹配）
CREATE TABLE IF NOT EXISTS item_types (
    type_id BIGINT PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    name_en VARCHAR(255) NULL COMMENT '英文名（SDE），供模糊查询',
    name_norm VARCHAR(512) NULL COMMENT '规范化搜索键 |中文|英文|',
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 市场交易详情表（按角色 + transaction_id 去重）
CREATE TABLE IF NOT EXISTS wallet_transactions (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    character_id BIGINT NOT NULL,
    transaction_id BIGINT NOT NULL COMMENT 'ESI wallet transaction id',
    journal_ref_id BIGINT NULL,
    type_id BIGINT NULL,
    location_id BIGINT NULL,
    client_id BIGINT NULL,
    date DATETIME NULL COMMENT 'UTC+8 北京时间',
    is_buy TINYINT(1) NULL,
    is_personal TINYINT(1) NULL,
    quantity BIGINT NULL,
    unit_price DECIMAL(20,2) NULL,
    total_price DECIMAL(20,2) NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_char_txn (character_id, transaction_id),
    KEY idx_char_date (character_id, date),
    KEY idx_char_journal_ref (character_id, journal_ref_id),
    CONSTRAINT fk_txn_char FOREIGN KEY (character_id)
        REFERENCES characters(character_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
