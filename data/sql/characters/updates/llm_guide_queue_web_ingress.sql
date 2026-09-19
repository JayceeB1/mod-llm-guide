-- --------------------------------------------------------
-- LLM Guide queue: request origin, external request identity and a safe
-- structured trace for the shared bridge engine.
--
-- The base file (base/llm_guide_queue.sql) is intentionally left untouched:
-- AzerothCore re-applies a module SQL file whose hash changes, and the base
-- file drops and recreates both llm_guide_queue and llm_guide_memory, which
-- would erase every character's session history.
--
-- Idempotent: tools/llm_guide_bridge.py applies the same columns and keys in
-- _ensure_table_exists(), so either may run first. Each statement checks
-- information_schema before altering anything.
-- --------------------------------------------------------

SET @llm_guide_schema := DATABASE();

SET @llm_guide_sql := IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = @llm_guide_schema AND TABLE_NAME = 'llm_guide_queue'
     AND COLUMN_NAME = 'external_request_id') = 0,
  'ALTER TABLE `llm_guide_queue` ADD COLUMN `external_request_id` VARCHAR(64) NULL DEFAULT NULL',
  'DO 0');
PREPARE llm_guide_stmt FROM @llm_guide_sql;
EXECUTE llm_guide_stmt;
DEALLOCATE PREPARE llm_guide_stmt;

SET @llm_guide_sql := IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = @llm_guide_schema AND TABLE_NAME = 'llm_guide_queue'
     AND COLUMN_NAME = 'origin') = 0,
  'ALTER TABLE `llm_guide_queue` ADD COLUMN `origin` ENUM(''ingame'', ''web'') NOT NULL DEFAULT ''ingame''',
  'DO 0');
PREPARE llm_guide_stmt FROM @llm_guide_sql;
EXECUTE llm_guide_stmt;
DEALLOCATE PREPARE llm_guide_stmt;

SET @llm_guide_sql := IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = @llm_guide_schema AND TABLE_NAME = 'llm_guide_queue'
     AND COLUMN_NAME = 'trace_json') = 0,
  'ALTER TABLE `llm_guide_queue` ADD COLUMN `trace_json` MEDIUMTEXT NULL DEFAULT NULL',
  'DO 0');
PREPARE llm_guide_stmt FROM @llm_guide_sql;
EXECUTE llm_guide_stmt;
DEALLOCATE PREPARE llm_guide_stmt;

SET @llm_guide_sql := IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = @llm_guide_schema AND TABLE_NAME = 'llm_guide_queue'
     AND COLUMN_NAME = 'grounding_state') = 0,
  'ALTER TABLE `llm_guide_queue` ADD COLUMN `grounding_state` VARCHAR(32) NULL DEFAULT NULL',
  'DO 0');
PREPARE llm_guide_stmt FROM @llm_guide_sql;
EXECUTE llm_guide_stmt;
DEALLOCATE PREPARE llm_guide_stmt;

SET @llm_guide_sql := IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = @llm_guide_schema AND TABLE_NAME = 'llm_guide_queue'
     AND COLUMN_NAME = 'provider_ms') = 0,
  'ALTER TABLE `llm_guide_queue` ADD COLUMN `provider_ms` INT UNSIGNED NULL DEFAULT NULL',
  'DO 0');
PREPARE llm_guide_stmt FROM @llm_guide_sql;
EXECUTE llm_guide_stmt;
DEALLOCATE PREPARE llm_guide_stmt;

SET @llm_guide_sql := IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = @llm_guide_schema AND TABLE_NAME = 'llm_guide_queue'
     AND COLUMN_NAME = 'tool_ms') = 0,
  'ALTER TABLE `llm_guide_queue` ADD COLUMN `tool_ms` INT UNSIGNED NULL DEFAULT NULL',
  'DO 0');
PREPARE llm_guide_stmt FROM @llm_guide_sql;
EXECUTE llm_guide_stmt;
DEALLOCATE PREPARE llm_guide_stmt;

SET @llm_guide_sql := IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
   WHERE TABLE_SCHEMA = @llm_guide_schema AND TABLE_NAME = 'llm_guide_queue'
     AND COLUMN_NAME = 'total_ms') = 0,
  'ALTER TABLE `llm_guide_queue` ADD COLUMN `total_ms` INT UNSIGNED NULL DEFAULT NULL',
  'DO 0');
PREPARE llm_guide_stmt FROM @llm_guide_sql;
EXECUTE llm_guide_stmt;
DEALLOCATE PREPARE llm_guide_stmt;

SET @llm_guide_sql := IF(
  (SELECT COUNT(*) FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = @llm_guide_schema AND TABLE_NAME = 'llm_guide_queue'
     AND INDEX_NAME = 'uq_llm_guide_external_request') = 0,
  'ALTER TABLE `llm_guide_queue` ADD UNIQUE KEY `uq_llm_guide_external_request` (`external_request_id`)',
  'DO 0');
PREPARE llm_guide_stmt FROM @llm_guide_sql;
EXECUTE llm_guide_stmt;
DEALLOCATE PREPARE llm_guide_stmt;

SET @llm_guide_sql := IF(
  (SELECT COUNT(*) FROM information_schema.STATISTICS
   WHERE TABLE_SCHEMA = @llm_guide_schema AND TABLE_NAME = 'llm_guide_queue'
     AND INDEX_NAME = 'idx_llm_guide_origin_status') = 0,
  'ALTER TABLE `llm_guide_queue` ADD KEY `idx_llm_guide_origin_status` (`origin`, `status`)',
  'DO 0');
PREPARE llm_guide_stmt FROM @llm_guide_sql;
EXECUTE llm_guide_stmt;
DEALLOCATE PREPARE llm_guide_stmt;
