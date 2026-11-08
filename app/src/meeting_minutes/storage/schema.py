"""SQLite schema and migration for the transcript store.

The statements are a subset of the generation-1 reference schema, copied without
changing any column contract: users, agents, meetings, speakers, source_epochs,
segments, segment_revisions, timeline_events, settings, search_documents and the
search_fts index with its triggers. The reference schema's snapshots, templates,
minutes, actions, jobs and sync tables are deliberately absent.
`user_version` 1 is the schema generation, not a product release label.
The optional OpenAI-minutes tables (`MIGRATION_MINUTES`) are added on top, additively.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1

# Applied on every connection: foreign keys, WAL, zeroed freed pages, full fsync.
CONNECTION_PRAGMAS = (
    "PRAGMA foreign_keys = ON",
    "PRAGMA journal_mode = WAL",
    "PRAGMA secure_delete = ON",
    "PRAGMA synchronous = FULL",
)

MIGRATION_V1 = """
CREATE TABLE users (
  id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE agents (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  label TEXT NOT NULL,
  credential_hash TEXT,
  revoked_at TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(id, owner_id)
);
CREATE TABLE meetings (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  agent_id TEXT,
  title TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  ended_at TEXT,
  timezone TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('starting','capturing','paused','degraded','stopping','stopped','interrupted')),
  data_mode TEXT NOT NULL CHECK(data_mode IN ('local','text_sync','remote_stt')),
  transcript_revision INTEGER NOT NULL DEFAULT 0 CHECK(transcript_revision >= 0),
  generation INTEGER NOT NULL DEFAULT 1 CHECK(generation >= 1),
  notice_json TEXT NOT NULL CHECK(json_valid(notice_json)),
  created_at TEXT NOT NULL,
  FOREIGN KEY(agent_id, owner_id) REFERENCES agents(id, owner_id),
  UNIQUE(id, owner_id)
);
CREATE INDEX meetings_owner_time ON meetings(owner_id, started_at DESC);
CREATE TABLE speakers (
  id TEXT PRIMARY KEY,
  meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
  label TEXT NOT NULL,
  confirmed INTEGER NOT NULL DEFAULT 0 CHECK(confirmed IN (0,1)),
  UNIQUE(id, meeting_id)
);
CREATE TABLE source_epochs (
  meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
  source TEXT NOT NULL CHECK(source IN ('mic','system')),
  epoch INTEGER NOT NULL CHECK(epoch >= 1),
  device_id TEXT NOT NULL,
  device_label TEXT NOT NULL,
  native_rate INTEGER NOT NULL CHECK(native_rate > 0),
  native_channels INTEGER NOT NULL CHECK(native_channels > 0),
  start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
  end_ms INTEGER CHECK(end_ms IS NULL OR end_ms >= start_ms),
  config_json TEXT NOT NULL CHECK(json_valid(config_json)),
  PRIMARY KEY(meeting_id, source, epoch)
);
CREATE TABLE segments (
  id TEXT PRIMARY KEY,
  meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
  source TEXT NOT NULL CHECK(source IN ('mic','system')),
  epoch INTEGER NOT NULL,
  start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
  end_ms INTEGER NOT NULL CHECK(end_ms >= start_ms),
  current_revision INTEGER NOT NULL CHECK(current_revision >= 1),
  engine_id TEXT NOT NULL,
  model_hash TEXT NOT NULL,
  timing_quality TEXT NOT NULL CHECK(timing_quality IN ('word','segment','chunk','estimated')),
  flags_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(flags_json)),
  FOREIGN KEY(meeting_id, source, epoch) REFERENCES source_epochs(meeting_id, source, epoch),
  UNIQUE(id, meeting_id)
);
CREATE INDEX segments_meeting_time ON segments(meeting_id, start_ms, id);
CREATE TABLE segment_revisions (
  segment_id TEXT NOT NULL,
  meeting_id TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK(revision >= 1),
  text TEXT NOT NULL,
  language TEXT NOT NULL DEFAULT 'unknown',
  speaker_id TEXT,
  origin TEXT NOT NULL CHECK(origin IN ('asr','user')),
  edited_at TEXT NOT NULL,
  PRIMARY KEY(segment_id, revision),
  FOREIGN KEY(segment_id, meeting_id) REFERENCES segments(id, meeting_id) ON DELETE CASCADE,
  FOREIGN KEY(speaker_id, meeting_id) REFERENCES speakers(id, meeting_id)
);
CREATE TABLE timeline_events (
  id TEXT PRIMARY KEY,
  meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK(kind IN ('gap','pause','resume','device_change','error','notice','mode_change')),
  source TEXT CHECK(source IN ('mic','system')),
  start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
  end_ms INTEGER CHECK(end_ms IS NULL OR end_ms >= start_ms),
  code TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(metadata_json))
);
CREATE TABLE settings (
  owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  value_json TEXT NOT NULL CHECK(json_valid(value_json)),
  PRIMARY KEY(owner_id, key)
);
CREATE TABLE search_documents (
  id INTEGER PRIMARY KEY,
  owner_id TEXT NOT NULL,
  meeting_id TEXT NOT NULL,
  entity_kind TEXT NOT NULL CHECK(entity_kind IN ('meeting','segment','minutes')),
  entity_id TEXT NOT NULL,
  normalized_text TEXT NOT NULL,
  FOREIGN KEY(meeting_id, owner_id) REFERENCES meetings(id, owner_id) ON DELETE CASCADE,
  UNIQUE(entity_kind, entity_id)
);
CREATE VIRTUAL TABLE search_fts USING fts5(
  normalized_text, content='search_documents', content_rowid='id',
  tokenize='unicode61'
);
INSERT INTO search_fts(search_fts, rank) VALUES('secure-delete', 1);
CREATE TRIGGER search_documents_ai AFTER INSERT ON search_documents BEGIN
  INSERT INTO search_fts(rowid, normalized_text) VALUES(new.id, new.normalized_text);
END;
CREATE TRIGGER search_documents_ad AFTER DELETE ON search_documents BEGIN
  INSERT INTO search_fts(search_fts, rowid, normalized_text)
    VALUES('delete', old.id, old.normalized_text);
END;
CREATE TRIGGER search_documents_au AFTER UPDATE ON search_documents BEGIN
  INSERT INTO search_fts(search_fts, rowid, normalized_text)
    VALUES('delete', old.id, old.normalized_text);
  INSERT INTO search_fts(rowid, normalized_text) VALUES(new.id, new.normalized_text);
END;
"""


# Optional OpenAI minutes. Additive tables created with IF NOT EXISTS on every open,
# so `user_version` stays 1: an older build can still open the file (it ignores these
# tables) and meeting deletion there still cascades through the foreign keys. Names
# avoid the reserved generation-1 names (minutes, actions, snapshots, jobs). Only
# segment ids/revisions are kept as the snapshot; the transcript text itself is never
# copied here. Generated text lives only in minutes_items and is deleted when a
# version is invalidated.
MIGRATION_MINUTES = """
CREATE TABLE IF NOT EXISTS minutes_versions (
  id TEXT PRIMARY KEY,
  meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
  version INTEGER NOT NULL CHECK(version >= 1),
  parent_version INTEGER,
  transcript_revision INTEGER NOT NULL CHECK(transcript_revision >= 0),
  snapshot_hash TEXT NOT NULL,
  snapshot_refs_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(snapshot_refs_json)),
  redactions_at_snapshot INTEGER NOT NULL DEFAULT 0,
  provider TEXT NOT NULL,
  model_id TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  schema_version INTEGER NOT NULL,
  output_language TEXT NOT NULL,
  input_tokens INTEGER,
  output_tokens INTEGER,
  requests INTEGER NOT NULL DEFAULT 0,
  parts INTEGER NOT NULL DEFAULT 1,
  incomplete INTEGER NOT NULL DEFAULT 0 CHECK(incomplete IN (0,1)),
  warnings_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(warnings_json)),
  state TEXT NOT NULL CHECK(state IN ('draft','review','approved')),
  user_edited INTEGER NOT NULL DEFAULT 0 CHECK(user_edited IN (0,1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  invalidated INTEGER NOT NULL DEFAULT 0 CHECK(invalidated IN (0,1)),
  invalidated_reason TEXT,
  UNIQUE(meeting_id, version)
);
CREATE TABLE IF NOT EXISTS minutes_items (
  id TEXT PRIMARY KEY,
  version_id TEXT NOT NULL REFERENCES minutes_versions(id) ON DELETE CASCADE,
  meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK(kind IN ('summary','topic','decision','question','action')),
  position INTEGER NOT NULL,
  text TEXT NOT NULL,
  detail TEXT,
  status TEXT CHECK(status IS NULL OR status IN ('agreed','superseded','uncertain')),
  owner TEXT,
  due_date TEXT,
  due_date_text TEXT,
  date_status TEXT CHECK(date_status IS NULL OR date_status IN ('explicit','resolved_relative','ambiguous','unspecified')),
  commitment TEXT CHECK(commitment IS NULL OR commitment IN ('explicit','proposed','uncertain')),
  origin TEXT NOT NULL CHECK(origin IN ('model','user')),
  reviewed INTEGER NOT NULL DEFAULT 0 CHECK(reviewed IN (0,1)),
  evidence_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(evidence_json))
);
CREATE INDEX IF NOT EXISTS minutes_items_version ON minutes_items(version_id, kind, position);
CREATE INDEX IF NOT EXISTS minutes_versions_meeting ON minutes_versions(meeting_id, version);
"""


class SchemaError(RuntimeError):
    pass


def connect(path: str) -> sqlite3.Connection:
    """Open the store with CONNECTION_PRAGMAS; autocommit off, explicit transactions."""
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=5.0)
    for pragma in CONNECTION_PRAGMAS:
        conn.execute(pragma)
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Create the schema in an empty database, add the minutes tables if missing, and
    refuse unknown newer versions."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, SCHEMA_VERSION):
        raise SchemaError(f"unsupported transcript database version {version}")
    if version == SCHEMA_VERSION and _has_minutes_tables(conn):
        return version
    conn.execute("BEGIN IMMEDIATE")
    try:
        if version == 0:
            for statement in _statements(MIGRATION_V1):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        for statement in _statements(MIGRATION_MINUTES):  # additive, idempotent
            conn.execute(statement)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return SCHEMA_VERSION


def _has_minutes_tables(conn: sqlite3.Connection) -> bool:
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('minutes_versions', 'minutes_items')")}
    return len(names) == 2


def _statements(script: str) -> list[str]:
    """Split on statement ends, keeping trigger bodies (which contain ';') whole."""
    out: list[str] = []
    buf: list[str] = []
    for line in script.strip().splitlines():
        buf.append(line)
        text = "\n".join(buf)
        if line.rstrip().endswith(";") and sqlite3.complete_statement(text):
            out.append(text)
            buf = []
    if "".join(buf).strip():
        raise SchemaError("incomplete migration statement")
    return out
