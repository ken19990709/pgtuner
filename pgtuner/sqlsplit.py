"""SQL statement splitter: lexically aware semicolon splitting.

Shared low-level piece of `cli._split_sql_file` (SQL file entry) and `sqlcmd`
(single-statement validation). Historical defect (R3): splitting on bare
semicolons after stripping comments tore the body of
CREATE FUNCTION ... AS $$ ...;... $$ LANGUAGE plpgsql into garbage statements —
this module is a minimal implementation following PG lexical rules (manual,
chapter 4):

  - Single-quoted strings: '' is an escaped quote; inside an E'...' prefix
    (E immediately adjacent to the quote and E itself a standalone word)
    backslash escapes apply
  - Double-quoted identifiers: "" is an escaped quote; semicolons inside do not split
  - dollar-quote $tag$...$tag$: tag is empty ($$) or starts with a letter/underscore;
    the closer must match the tag exactly; no escaping, different tags may nest;
    a $ immediately after identifier characters (e.g. column name price$total)
    is not treated as an opener (approximating the PG lexer: $ does not open a
    dollar-quote from the middle of an identifier); comments/quotes inside the
    body are kept as literal content
  - No splitting inside `--` line comments / `/* */` block comments (PG dialect:
    block comments nest); a comment is replaced wholesale with a single space
    (matching the pre-existing "strip comments" semantics from the D1 fix era —
    statement text contains no comments and no word is broken; comments inside
    a dollar-quote body are unaffected)

Output: list of non-empty statements (each stripped); a trailing segment without
a final semicolon also counts as one statement.
Unclosed string / dollar-quote / block comment raises ValueError — better to
error locally than send garbage statements to the server (the debugging
experience of a "syntax error" surfacing only after some statements already
executed is far worse).
"""
from __future__ import annotations

import re

# dollar-quote opener: $$ or $identifier$ (identifier starts with letter/underscore; excludes parameter placeholders like $1)
_DOLLAR_OPEN = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")
# Characters allowed in PG identifiers (to decide whether a $ sits mid-identifier; " included to defend against adjacent quotes)
_IDENT_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_$\"")
# Lexer whitespace for the UESCAPE clause scan (PG's {space} class; comments are
# handled separately by _skip_ws_and_comments because the lexer counts them as
# token separators too — verified on a live server: a UESCAPE clause is honored
# even when separated from the literal only by a comment or by nothing at all).
_LEXER_SPACE = " \t\r\n\f\v"
_HEX_DIGITS = set("0123456789abcdefABCDEF")
# plain '...' string that must hold the UESCAPE character (a one-char body cannot
# contain a quote, so no '' doubling handling is needed here)
_UESCAPE_STRING = re.compile(r"'([^']*)'")


def split_statements(text: str) -> list[str]:
    """Lexically aware splitting. See module docstring; any character not in a special region stays in the current statement buffer."""
    stmts: list[str] = []
    buf: list[str] = []

    def emit() -> None:
        stmt = "".join(buf).strip()
        if stmt:
            stmts.append(stmt)
        buf.clear()

    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":                 # line comment: consume to end of line (excluding newline), replace with a single space
            j = text.find("\n", i)
            j = n if j == -1 else j
            buf.append(" ")
            i = j
            continue
        if ch == "/" and nxt == "*":                 # block comment: nestable in the PG dialect, replaced wholesale with a single space
            depth, j = 1, i + 2
            while j < n and depth:
                if text[j:j + 2] == "/*":
                    depth += 1
                    j += 2
                elif text[j:j + 2] == "*/":
                    depth -= 1
                    j += 2
                else:
                    j += 1
            if depth:
                raise ValueError("未闭合的 /* 块注释")
            buf.append(" ")
            i = j
            continue
        if ch == "'":
            i = _consume_single_quoted(text, i, buf)
            continue
        if ch == '"':
            i = _consume_double_quoted(text, i, buf)
            continue
        if ch == "$":
            tag = _dollar_opener_at(text, i, buf)
            if tag is not None:
                i = _consume_dollar_quoted(text, i, tag, buf)
                continue
            # Not an opener ($1 placeholder / part of an identifier / plain char): falls through to the buffer
        if ch == ";":
            emit()
            i += 1
            continue
        buf.append(ch)
        i += 1
    emit()
    return stmts


def _prev_raw(buf: list[str]) -> str:
    """Most recently appended raw character in the buffer (to detect the E'' prefix and the identifier context of $; whitespace not considered)."""
    return buf[-1] if buf else ""


def _dollar_opener_at(text: str, i: int, buf: list[str]) -> str | None:
    """Whether the $ at position i opens a dollar-quote. Yes → return the tag ("" for an empty tag); no → None."""
    if _prev_raw(buf) and _prev_raw(buf) in _IDENT_CHARS:
        return None                          # mid-identifier (e.g. price$total$): not an opener
    m = _DOLLAR_OPEN.match(text, i)
    return (m.group(1) or "") if m else None


def _single_quoted_end(text: str, start: int) -> int:
    """Index just past the closing quote of the '...' literal at text[start] == "'".

    Single source of truth shared by split_statements and mask_literals so the two
    scanners always agree on quote spans (round-3 review F3: mask used to ignore the
    E'' backslash escapes that split honors — the same input split as one string
    raised "unclosed" in mask, or worse, mask closed the string early and turned a
    legal SELECT into a false write-smuggle hit).

    The E prefix is decided from the RAW preceding characters (E immediately adjacent
    to the quote and itself a standalone word), which is exactly what split's old
    buffer-based check saw: the only characters split rewrites before the quote are
    comments → ' ', and a comment char is no more an E than a space is (and a closing
    " of an identifier is raw in split's buffer, matching text[start-1] as well).
    """
    escaped = False
    if start >= 1 and text[start - 1] in ("E", "e"):
        before = text[start - 2] if start >= 2 else ""
        if not before or before not in _IDENT_CHARS:
            escaped = True
    j, n = start + 1, len(text)
    while j < n:
        c = text[j]
        if escaped and c == "\\":
            j += 2
            continue
        if c == "'":
            if j + 1 < n and text[j + 1] == "'":
                j += 2
                continue
            return j + 1
        j += 1
    raise ValueError("未闭合的 ' 字符串")


def _consume_single_quoted(text: str, start: int, buf: list[str]) -> int:
    """Consume a '...' string ('' escaping; \\ escaping inside E'...'). Appends to the buffer, returns the index after the close."""
    end = _single_quoted_end(text, start)
    buf.append(text[start:end])
    return end


def _consume_double_quoted(text: str, start: int, buf: list[str]) -> int:
    """Consume a "..." identifier ("" escaping). Appends to the buffer, returns the index after the close."""
    j, n = start + 1, len(text)
    while j < n:
        if text[j] == '"':
            if j + 1 < n and text[j + 1] == '"':
                j += 2
                continue
            buf.append(text[start:j + 1])
            return j + 1
        j += 1
    raise ValueError('未闭合的 " 标识符')


def _consume_dollar_quoted(text: str, start: int, tag: str, buf: list[str]) -> int:
    """Consume $tag$...$tag$ (no escaping; closed at the first exact same-tag delimiter). Returns the index after the close."""
    closer = f"${tag}$"
    j = text.find(closer, start + len(closer))
    if j == -1:
        raise ValueError(f"未闭合的 dollar-quote {closer}")
    end = j + len(closer)
    buf.append(text[start:end])
    return end


# ---------------------------- read-only surface guards (R2 hardening) ----------------------------
# PostgreSQL read-only transactions do NOT prevent function side effects or
# program execution: COPY ... TO PROGRAM runs an external program (the PG source
# checks read-only only on the COPY FROM branch), pg_terminate_backend kills
# other sessions, pg_read_file/lo_export read/write the filesystem — and pgtuner
# instances are superuser + trust by default. These guards turn "read-only by the
# database layer" back into a true statement for the agent-facing surfaces.

# Functions with non-transactional or filesystem/process side effects (superuser
# or otherwise); pg_sleep is capped by statement_timeout on the sql surface and
# excluded there, but blocked on the EXPLAIN ANALYZE surfaces.
# nextval/setval/pg_nextoid (round-4 D-F6): sequence advances are
# NON-transactional (they survive rollback — live-verified: select nextval('s')
# really advanced the sequence through advise --query --analyze despite the hypo
# rollback). They are on the analyze list for that reason, and ALSO on the sql
# list: PG read-only transactions already refuse them with SQLSTATE 25001
# (sequence.c: "read-only transactions may only modify temp sequences" →
# PreventCommandIfReadOnly, live-verified against the source), so the tool-level
# reject merely front-runs the identical server refusal with a friendlier message
# — no legitimate probe is lost, and currval/lastval (true read-only probes) stay
# off both lists.
# Round-5 D5-F1 (blocker, live-verified): the lo_* WRITE family —
# select lowrite(lo_open(lo_create(0),131072),'x') passed all three tool layers
# (head whitelist / write-smuggle / side-effect blacklist) and diagnose
# --analyze really executed and committed it on the rw autocommit connection
# (pg_largeobject_metadata 0→1). lo_open is refused in ALL modes on purpose:
# only the INV_WRITE mode is dangerous, but INV_READ-only use is rare and the
# blanket reject also kills wrap-smuggles like lowrite(lo_open(…)).
# Round-5 D5-F2 (major, live-verified on the sql surface): the ops side-effect
# family — read-only transactions stop none of them: pg_stat_statements_reset
# (0,0,0) cleared the pgss stats of ALL instances, pg_replication_origin_create
# ('x') really created a persistent origin (drop/advance likewise), and
# pg_create_restore_point / pg_switch_wal / pg_wal_replay_pause / resume /
# pg_promote all force cluster-level state changes.
# Round-6 D6-F1/F2/F4-F6 (live-verified in review round 6): alias/variant
# family completions that \b word boundaries cannot cover from a shorter entry
# ('_' is a word character, so \blo_create\b misses lo_creat and
# \bpg_stat_reset\b misses pg_stat_reset_shared — each name must be listed
# verbatim): lo_creat is the OLD ALIAS of lo_create, still a real pg_catalog
# function (pronargs=1) — select lo_creat(-1) on the rw autocommit surfaces
# really persisted large objects (pg_largeobject_metadata 0→14); the
# pg_stat_reset trio (pg_stat_reset / pg_stat_reset_shared / pg_stat_reset_slru)
# has no read-only-transaction check at all and _shared is CLUSTER-level;
# lo_truncate64, the pg_try_advisory_* / pg_advisory_unlock* halves of the
# advisory-lock family, and pg_replication_origin_session_assign (removed in
# PG 17, present through PG<=16 — cross-version completeness).
# Round-7 D7-F2 (blocker, live-verified in review round 7): the BRIN/GIN
# index-maintenance family — select brin_summarize_new_values('t') returned 44
# on a READ-ONLY session and re-ran to 0 (the summarized pages were committed);
# brin_summarize_range / brin_desummarize_range / gin_clean_pending_list behave
# the same (index maintenance functions carry no PreventCommandIfReadOnly) —
# real, committed index writes through every read-only surface.
# Round-7 D7-F3 (major, live-verified): the replication-slot family — a
# persistent physical/logical slot pins WAL forever (disk-fill on a lab box) and
# pg_drop_replication_slot / pg_replication_slot_advance break a live standby.
# D7-F4 (major): pg_logical_emit_message writes into the WAL stream.
# D7-F7 (minor): the four _shared advisory-lock variants complete the existing
# lock-family entries. D7-F8 (minor): PG17+ renamed the per-object stats resets
# — pg_stat_reset_replication_slot / pg_stat_reset_subscription_stats join the
# already-listed pg_stat_reset trio (same missing read-only check).
# D7-F9 (minor): the named pg_ls_* variants (waldir/archive_statusdir/logdir/
# tmpdir) enumerate the same filesystem layout as the already-listed pg_ls_dir
# (information-disclosure family). D7-F10 (nit): pg_backup_start/pg_backup_stop
# (source-level: no read-only check) — exclusive-backup mode is itself a
# cluster state change.
# Round-8 systematic family sweep (audit target: live instance rel19, PG19devel,
# the fullest function surface — full pg_catalog proname dump via read-only
# `pgtuner sql`; candidates = every catalog function whose name carries a
# mutating semantic root: creat|drop|reset|clear|restore|import|switch|advanc|
# summar|desummar|truncat|unlink|write|put|emit|promot|assign|start|stop|inval|
# free|rotate|reload|terminate|cancel|signal|advise|clean|consume|forward|move|
# purge|refresh|spawn|kill|seal|lock|unlock|export|read_file|ls_dir|sleep, then
# a family fan-out on every blocked family prefix: pg_ls_*, pg_stat_reset*,
# binary_upgrade_*, lo_*, pg_logical_*, pg_replication_origin_*, …). Reconciled
# 128 root candidates: 49 already covered, 14 newly blocked (13 on both lists +
# pg_sleep_for analyze-only), 56 deliberately allowed; the fan-out added 19 more
# family completions plus the cross-version twin pg_import_snapshot. Frozen
# snapshot contract: tests/test_hardening_r1.py test_round8_sweep_snapshot_contract.
#
# ADDED this round (refuse-beats-release; none of these names can appear in a
# legitimate read-only probe):
#   - PG18+ statistics import/clear family (D8-F1 BLOCKER, live-verified:
#     pg_restore_relation_stats persisted reltuples 10→4242 and read-only
#     transactions do not stop any of them): pg_restore_relation_stats,
#     pg_restore_attribute_stats, pg_restore_extended_stats,
#     pg_clear_relation_stats, pg_clear_attribute_stats, pg_clear_extended_stats;
#     plus their PG17-development-era old names set_relation_stats /
#     set_attribute_stats (removed before release but present in PG17devel
#     builds — cross-version completeness, same stance as lo_creat).
#   - pg_import_system_collations (D8-F1b, live-verified: persistently created
#     854 pg_collation rows on a read-only surface).
#   - pg_stat_reset_backend_stats (D8-F3, PG18+ per-backend stats reset) and
#     the older per-object resets pg_stat_reset_single_table_counters /
#     pg_stat_reset_single_function_counters (same no-read-only-check class as
#     the already-listed pg_stat_reset trio, which they predate).
#   - pg_advisory_xact_lock_shared (D8-F5: the try twin was listed, the
#     blocking lock itself was not).
#   - pg_logical_slot_get_changes / pg_logical_slot_get_binary_changes (D8-F6:
#     the get variants CONSUME the logical slot and durably advance
#     confirmed_flush_lsn; the peek twins decode without consuming and stay off
#     the lists).
#   - PG<10 old aliases pg_switch_xlog / pg_xlog_replay_pause /
#     pg_xlog_replay_resume (C8-F5, cross-version completeness).
#   - pg_export_snapshot (one-shot export marker mutates transaction state) and
#     its PG≤17 twin pg_import_snapshot (installs an exported snapshot into the
#     current transaction); pg_replication_origin_session_reset /
#     pg_replication_origin_xact_reset (completing the already-blocked
#     replication-origin family); pg_stat_clear_snapshot (discards the backend
#     statistics snapshot — reset-type operation).
#   - WAL-summary disclosure trio pg_ls_summariesdir / pg_available_wal_summaries
#     / pg_wal_summary_contents (directory enumeration + summary-content
#     disclosure — same information-disclosure class as the D7-F9 pg_ls_*
#     family) and the three D7-F9 misses found by the family fan-out:
#     pg_ls_logicalmapdir / pg_ls_logicalsnapdir / pg_ls_replslotdir.
#   - The binary_upgrade hijack family: binary_upgrade_create_empty_extension,
#     binary_upgrade_create_conflict_detection_slot,
#     binary_upgrade_replorigin_advance, binary_upgrade_add_sub_rel_state,
#     binary_upgrade_set_missing_value, binary_upgrade_set_record_init_privs,
#     pg_stop_making_pinned_objects and the sixteen
#     binary_upgrade_set_next_* OID/relfilenode hijackers. Outside binary
#     upgrade mode the server itself refuses them ("can only be called when
#     server is in binary upgrade mode"), so these entries merely front-run an
#     identical server refusal (the same stance as nextval/setval on the sql
#     list); inside that mode they hijack catalog OID assignment.
#   - pg_sleep_for joins the sleep family on the ANALYZE list only (the
#     interval twin of pg_sleep/pg_sleep_until; the sql surface keeps the
#     documented differential — statement_timeout caps it there).
# Round-9 red-team confirmation gaps (R9-F1..F12; the round-8 root sweep
# missed whole roots — copy/log/sync/setup/checksum/file — all names below
# exist in the live rel19 catalog unless noted):
#   - pg_disable_data_checksums / pg_enable_data_checksums (R9-F1 BLOCKER:
#     persistent cluster-level checksum flip plus a whole-cluster rewrite
#     scan).
#   - pg_copy_physical_replication_slot / pg_copy_logical_replication_slot
#     (R9-F2: creates PERSISTENT slots — red-team measured them landing on
#     disk from the analyze surface).
#   - pg_log_backend_memory_contexts (R9-F3: dumps any session's memory
#     contexts into the server log — loopable log flooding),
#     pg_log_standby_snapshot (R9-F4: writes directly into WAL).
#   - pg_replication_origin_session_setup / pg_replication_origin_xact_setup
#     (R9-F7: the setup twins — the reset halves were already blocked).
#   - pg_sync_replication_slots (R9-F8: real slot changes on a standby).
#   - txid_current / pg_current_xact_id (R9-F9: really allocate and burn an
#     xid; the *_if_assigned twins allocate nothing and stay OFF both lists).
#   - pg_stat_force_next_flush (R9-F11: benign stats flush — blocked for
#     consistency with pg_stat_clear_snapshot) and pg_extension_config_dump
#     (R9-F11: the server itself refuses it outside extension scripts —
#     front-running the identical refusal, the nextval/setval precedent).
#   - adminpack legacy pg_catalog seats pg_file_read / pg_file_write /
#     pg_file_rename / pg_file_unlink / pg_file_ls (R9-F12: server-side
#     file I/O on any old instance that ever installed adminpack; absent
#     from the rel19 dump — blocked by policy, the same stance as lo_creat).
# Round-9 INVERTED full sweep (seals the "missed mutating function" pattern
# for good): instead of hunting mutating roots, the complete pg_catalog
# proname dump of the live rel19 instance (2803 distinct names, read-only
# `pgtuner sql`) minus the blacklists minus an explicit allow group minus
# the documented read-only pattern groups below was driven to a ZERO
# manual-review remainder:
#     2803 = 123 blacklist hits + 66 round-8 allow + 136 round-9 allow
#            + 2478 pattern-group hits, remainder 0.
#     (Round 10 later MOVED the six query-string executors from the
#     round-9 allow group into BOTH blacklists — see the R10 paragraph
#     below the allow groups; the reconciliation shifts to 129 blacklist
#     + 66 round-8 + 130 round-9 allow + 2478 pattern = 2803.)
# ADDED this round by the sweep itself (all three live-verified on the
# read-only sql surface — each call RETURNED, piercing the read-only
# transaction exactly the way pg_stat_reset does):
#   - set_config: session-level GUC assignment with no read-only check; a
#     session_preload_libraries / search_path hijack steers every LATER
#     statement of the same session — the same sticky session-state class
#     as pg_advisory_lock.
#   - pg_notify: queues a cross-session notification (the NOTIFY queue
#     write the read-only transaction does not stop).
#   - setseed: benign but mutating — non-transactional backend PRNG state
#     that survives the statement (the pg_stat_force_next_flush class).
# DELIBERATELY ALLOWED by the round-9 audit (read-only, zero blacklist
# hits; frozen as _R9_ALLOWED_FNS in tests/test_hardening_r1.py):
#   - sequence READS completing the blocked family: currval / lastval
#     (probes; nextval/setval/pg_nextoid stay blocked), plus loread
#     (descriptor-only lo_* reader; lo_open is refused in ALL modes).
#   - snapshot / xid / WAL-position reads: pg_current_snapshot,
#     pg_current_wal_{lsn,flush_lsn,insert_lsn}, pg_last_wal_receive_lsn,
#     pg_last_wal_replay_lsn, pg_last_xact_replay_timestamp,
#     pg_is_in_recovery, pg_is_wal_replay_paused, pg_last_committed_xact,
#     pg_xact_commit_timestamp(_origin), pg_xact_status, txid_status,
#     pg_notification_queue_usage, pg_listening_channels, pg_wal_lsn_diff,
#     pg_walfile_name(_offset), pg_split_walfile_name.
#   - replication-origin READS: pg_replication_origin_{oid,progress,
#     session_progress,session_is_setup} (the create/setup/advance/reset
#     family stays blocked).
#   - structured config-audit views (parsed rules, not arbitrary file
#     reads — the raw primitives pg_read_file/pg_stat_file/pg_ls_dir stay
#     blocked): pg_hba_file_rules, pg_ident_file_mappings,
#     pg_show_all_settings, pg_show_all_file_settings,
#     pg_show_replication_origin_status, pg_settings_get_flags, pg_config.
#   - catalog/metadata probes: pg_describe_object, pg_identify_object(_as_
#     address), pg_options_to_table, pg_partition_root,
#     pg_prepared_{statement,xact}, pg_cursor, pg_stat_have_stats,
#     pg_numa_available (new in PG19), pg_backend_pid, pg_conf_load_time,
#     pg_jit_available, pg_my_temp_schema, pg_is_other_temp_schema,
#     pg_trigger_depth, pg_typeof, pg_basetype, pg_filenode_relation,
#     pg_char_to_encoding, pg_client_encoding, getdatabaseencoding,
#     getpgusername, get_current_ts_config, pg_extension_update_paths,
#     pg_available_extension(s/_versions), pg_mcv_list_items,
#     {obj,col,shobj}_description, pg_tablespace_location,
#     pg_indexam_{has_property,progress_phasename}, row_security_active,
#     querytree, currtid2, pg_event_trigger_ddl_commands (needs event-
#     trigger context server-side).
#   - xml mappers over NAMED relations plus the text-search/xml VALUE
#     family (arguments are relation names or documents, never a query
#     string to execute): {table,schema,database}_to_xml(_schema,
#     _and_xmlschema), ts_debug, ts_lexize, ts_parse, ts_token_type,
#     xmlvalidate, xmlexists, xpath(_exists). The query-string executor
#     halves this group originally also allowed on the "SPI read-only at
#     source level" rationale (xml.c / tsvector_op.c) — query_to_xml
#     (_schema,_and_xmlschema), cursor_to_xml(schema), ts_stat — were
#     REMOVED by round 10; see the R10 paragraph below.
#   - VALUE-level setters/generators whose set/seed/convert root is
#     copy-in/copy-out (no server state touched): set_bit, set_byte,
#     set_masklen, setweight, strip, numnode, parse_ident, get_bit,
#     get_byte, convert(_from/_to), random(_normal), gen_random_uuid,
#     uuidv4, uuidv7, in_range, any_value, error_on_null (raises),
#     satisfies_hash_partition, num_{nonnulls,nulls}, phraseto_tsquery,
#     plainto_tsquery, aclcontains, acldefault, aclexplode, aclitemeq,
#     aclinsert (upstream error stub, acl.c), makeaclitem, amvalidate,
#     anytextcat, session_user, system, system_user, version, timeofday,
#     icu_unicode_version, unicode_version, pg_lsn (type constructor).
# Round-10 red-team finding (R10-F1 BLOCKER, live-verified): the
# QUERY-STRING EXECUTOR category — ts_stat, query_to_xml, query_to_xmlschema,
# query_to_xml_and_xmlschema, cursor_to_xml, cursor_to_xmlschema — executes
# an SQL statement handed in as a STRING LITERAL argument, and two of our
# defenses are STRUCTURALLY blind to what runs inside it:
#   (a) the inner query IS string data — mask_literals blanks literal bodies
#       before every blacklist regex, so ts_stat('select pg_stat_reset()')
#       hides the blacklisted inner call completely (the blacklist never
#       sees it; only the outer executor name is matchable);
#   (b) the executor's SPI runs with read_only=true, but that is the SAME
#       mechanism as a session read-only transaction (XactReadOnly /
#       PreventCommandIfReadOnly): it stops DML/DDL only and NEVER the side
#       effects of a function that carries no read-only check itself
#       (pg_stat_reset & co. have none).
# R10 measured it live: ts_stat embedding pg_stat_reset_single_table_counters
# REALLY reset the table's statistics through BOTH agent-facing surfaces
# (the read-only `pgtuner sql` surface and diagnose --analyze). The round-9
# allow rationale ("SPI read-only at source level", xml.c/tsvector_op.c) is
# hereby RETIRED for this whole category: any function whose argument is an
# arbitrary query string is rejected by default — do NOT re-allow a future
# PG name of this shape on the old SPI-read-only rationale. Still allowed,
# because their text arguments are DOCUMENTS or relation NAMES, not SQL to
# execute: ts_debug / ts_lexize / ts_parse / ts_token_type (regconfig+text),
# the {table,schema,database}_to_xml* mappers, and the xml VALUE family
# xmlvalidate / xmlexists / xpath(_exists).
# READ-ONLY PATTERN GROUPS of the round-9 sweep (reproduce them on the next
# re-audit; a name matching a group is read-only by family; names matching
# NO group or list force manual review; blacklist entries resolve BEFORE
# patterns — e.g. brin_summarize_* survives the brin_ support pattern, and
# (since R10) ts_stat survives the ts_* value group).
# rel19 hit counts in parentheses; counts sum to 2478:
#   - pg_stat_get_* (99): statistics ACCESSORS — only the reset/flush
#     family is blocked.  pg_get_* (43): catalog deparse/introspection
#     (defs, exprs, pg_get_replication_slots …).  pg_size_* (1) and
#     *_size (3): size calculations.  pg_table_* (2), pg_column_* (4),
#     pg_index_* (2), pg_relation_* (5): visibility/property probes.
#     pg_sequence_* (2): sequence parameters + pg_sequence_last_value PEEK
#     (no advance).  pg_encoding_* (2): encoding name<->id mapping.
#     pg_ts_* (4): text-search visibility probes.  *collation* (9):
#     collation metadata accessors + regcollation I/O.  quote* (3),
#     regexp* (9), format* (2), to_* (22), numeric* (58): value-level
#     string/number helpers.  abs|div|floor|ceil|width* (7): value math.
#     current_* (6): session-state READS (the mutating half set_config is
#     blocked by name).  now/clock/statement_timestamp/transaction_
#     timestamp (3): clock reads.  *refcount* (0 hits on rel19; kept for
#     future rounds).
#   - *_to_* (69): cross-type/encoding conversion value functions.
#     json*/jsonb*/jsonpath* (113): JSON value family (jsonb_set & co.
#     return new values).  xml*/xpath* (12): XML value builders and
#     mappers (SPI read-only, see allow group).  ts_* (10) and tsvector*/
#     tsquery*/tsq*/tsmatch* (33): text-search value functions.
#     md5/sha*/crc32* (7): digest value functions.
#   - type I/O converters _in/_out/_recv/_send/typmodin/typmodout (210):
#     pure value transforms (many take cstring/internal pseudotypes and
#     are not even SQL-callable).  _eq/_ne/_lt/_le/_gt/_ge/_cmp/_hash/
#     _hash_extended (176): comparison/hash operator support.  _pl/_mi/
#     _mul/_div/_mod/_um/_up/_abs/_larger/_smaller/_cat/_and/_or/_xor/
#     _not/_shl/_shr (64): arithmetic/bitwise operator support.  *sel/
#     *joinsel (44): selectivity-estimation support.
#   - executor/planner machinery (155): _transfn/_finalfn/_combinefn/
#     _combine/_serialize/_deserialfn/_deserialize/_accum(_inv)/_statefunc/
#     _support/_typanalyze/_validator/_options/_sortsupport/_skipsupport/
#     _subscript_handler/_opcinfo/_consistent/_triconsistent/_union/
#     _penalty/_picksplit/_compress/_decompress/_fetch/_same/_distance/
#     _config/_choose/_inner_consistent/_leaf_consistent/_add_value/_final/
#     _transition(_multi)/_inc_any/_dec_any/_handler — aggregate,
#     index-AM, validator, handler and window internals over their own
#     arguments.
#   - index-AM support families brin*/gin*/gist*/spg*/gtsvector* (112),
#     bt* (50), hash* (61): opcinfo/consistent/penalty-style internals
#     (internal pseudotype args; the MUTATING brin_summarize_*/gin_clean_
#     pending_list are blacklisted BEFORE the pattern applies).
#   - trigger internals RI_FKey_*/tsvector_update_trigger(_column)/
#     unique_key_recheck/suppress_redundant_updates_trigger (14): the
#     server refuses direct calls — ri_CheckTrigger / CALLED_AS_TRIGGER
#     guards raise "was not called by trigger manager" / "must be called
#     as trigger" (ri_triggers.c, trigfuncs.c, constraint.c,
#     tsvector_op.c); they only run from write statements the surfaces
#     refuse at the head layer.
#   - scalar-type operator support int*/oid*/oidvector*/xid*/cid*/cash*/
#     float*/bool*/bit*/varbit*/bpchar*/char*/varchar*/bytea*/name*/
#     text*/uuid*/macaddr*/tid*/money* (483); date/time/timestamp/
#     timestamptz/timetz/interval/make_*/justify_*/extract/age/timezone
#     (87); geometric box/circle/poly(point)/line/lseg/path/polygon/
#     dist_/on_/close_/inter_/area/center/height/radius/slope/pclose/
#     popen/isempty/is_normalized/scale (139); inet/cidr/network/netmask/
#     hostmask/masklen/broadcast/host/family/mxid_age (25); range/
#     multirange constructors + operators + *_subdiff (65); array*/unnest
#     (20); reg* type I/O (45); enum_* (2); acl value helpers (2); string
#     value fns (42); math value fns (52); window/aggregate SQL names
#     (36) + *_agg (8); pg_lsn* family (2); *snapshot* / *_in_snapshot
#     (9: pg_snapshot_*, txid_snapshot_*, snapshot readers — the mutating
#     pg_stat_clear_snapshot resolves against the blacklist first);
#     pg_timezone_* (3); pg_control_* (4); pg_partition_* (2);
#     pg_tablespace_* (2); has_*_privilege / pg_has_role (15);
#     *_is_visible (7); plpgsql_/fmgr_/postgresql_fdw_ validators and
#     handlers (7); generate_*/bernoulli/system SRFs (3); pseudotype/
#     record/window_/ordered_set/percentile machinery (2); domain_*/
#     internal/shell/cstring/trigger/event_trigger/*_handler pseudotype
#     I/O (2); string_to_* SRF tail (1).
# ALREADY COVERED by earlier rounds (the 49 root candidates re-verified verbatim
# on both lists): the lo_* write family, the complete advisory-lock set, the
# pg_stat_reset trio + replication-slot/subscription resets, the
# replication-origin create/drop/advance/assign set, pg_switch_wal / replay
# pause+resume / pg_promote, the BRIN summarize/desummarize + GIN clean family,
# the replication-slot create/drop/advance + logical-emit family, pg_ls_dir +
# the four named-dir variants, pg_backup_start/stop, pg_sleep(_until).
# DELIBERATELY ALLOWED after audit (read-only; zero blacklist hits):
#   - the pg_stat_get_* accessor tail the roots drag in (…_stat_reset_time,
#     …_blocks_fetched/hit, …_write_time, …_restartpoints_*, …_conflict_lock,
#     …_deadlocks, …_sessions_killed, …_backend_*_start, …_bgwriter_*_clean,
#     …_get_lock …): pure statistics reads — the RESETS are blocked, the
#     ACCESSORS are not (pg_stat_get_replication_slot precedent, round 7).
#   - pg_blocking_pids / pg_safe_snapshot_blocking_pids (wait-graph reads),
#     pg_current_xact_id_if_assigned / txid_current_if_assigned (return NULL
#     without allocating an xid), pg_postmaster_start_time,
#     pg_get_wal_summarizer_state, pg_lock_status (the pg_locks view backing
#     SRF), the pg_event_trigger_* read-only SRFs, pg_input_is_valid /
#     pg_input_error_info (pure validators),
#     pg_isolation_test_session_is_blocked (read-only regression-test helper).
#   - value-level pure functions the roots surface: aclremove, array_remove,
#     ts_rewrite, starts_with, text_starts_with_support, tid_block,
#     unicode_assigned, clock_timestamp (the "lock" root fires inside c-lock-k),
#     prsd_start (internal text-search parser support), and the eight
#     brin_*summary_in/out/recv/send type-I/O functions.
#   - descriptor-only lo_* readers: lo_get / lo_close / lo_lseek / lo_lseek64 /
#     lo_tell / lo_tell64 — meaningful only with a lo_open descriptor, which is
#     refused in ALL modes; lo_get itself is a plain database-data read.
#   - the logical-slot peek twins pg_logical_slot_peek_changes /
#     pg_logical_slot_peek_binary_changes (decode without consuming) and
#     binary_upgrade_check_logical_slot_pending_wal (read-only check used by
#     pg_upgrade --check against live servers).
# When a new PG version adds another name to any family above, rerun the
# INVERTED sweep (full pg_catalog dump − blacklists − allow group − pattern
# groups, remainder must reach zero again) and update both the lists and the
# test snapshot — the contract test failing is the reminder that a human
# audit is due (see also the resweep invariant in CONTRIBUTING.md).
SIDE_EFFECT_FUNCTIONS = re.compile(
    r"\b(pg_terminate_backend|pg_cancel_backend|pg_reload_conf|pg_rotate_logfile"
    r"|pg_read_file|pg_read_binary_file|pg_ls_dir|pg_stat_file|pg_current_logfile"
    r"|lo_import|lo_export|lo_put|largeobject"
    r"|lo_create|lo_creat|lo_open|lowrite|lo_truncate|lo_truncate64|lo_unlink|lo_from_bytea"
    r"|dblink|dblink_exec|postgres_fdw_connect"
    r"|pg_advisory_lock|pg_advisory_xact_lock|pg_try_advisory_lock|pg_try_advisory_xact_lock"
    r"|pg_advisory_unlock|pg_advisory_unlock_all"
    r"|pg_stat_statements_reset|pg_stat_reset|pg_stat_reset_shared|pg_stat_reset_slru"
    r"|pg_replication_origin_create|pg_replication_origin_drop"
    r"|pg_replication_origin_advance|pg_replication_origin_session_assign"
    r"|pg_create_restore_point|pg_switch_wal"
    r"|pg_wal_replay_pause|pg_wal_replay_resume|pg_promote"
    r"|brin_summarize_new_values|brin_summarize_range|brin_desummarize_range"
    r"|gin_clean_pending_list"
    r"|pg_create_physical_replication_slot|pg_create_logical_replication_slot"
    r"|pg_drop_replication_slot|pg_replication_slot_advance|pg_logical_emit_message"
    r"|pg_advisory_lock_shared|pg_try_advisory_lock_shared"
    r"|pg_try_advisory_xact_lock_shared|pg_advisory_unlock_shared"
    r"|pg_stat_reset_replication_slot|pg_stat_reset_subscription_stats"
    r"|pg_ls_waldir|pg_ls_archive_statusdir|pg_ls_logdir|pg_ls_tmpdir"
    r"|pg_backup_start|pg_backup_stop"
    r"|pg_restore_relation_stats|pg_restore_attribute_stats|pg_restore_extended_stats"
    r"|pg_clear_relation_stats|pg_clear_attribute_stats|pg_clear_extended_stats"
    r"|set_relation_stats|set_attribute_stats"
    r"|pg_import_system_collations"
    r"|pg_stat_reset_backend_stats|pg_stat_reset_single_table_counters"
    r"|pg_stat_reset_single_function_counters"
    r"|pg_advisory_xact_lock_shared"
    r"|pg_logical_slot_get_changes|pg_logical_slot_get_binary_changes"
    r"|pg_switch_xlog|pg_xlog_replay_pause|pg_xlog_replay_resume"
    r"|pg_export_snapshot|pg_import_snapshot"
    r"|pg_replication_origin_session_reset|pg_replication_origin_xact_reset"
    r"|pg_stat_clear_snapshot"
    r"|pg_ls_summariesdir|pg_ls_logicalmapdir|pg_ls_logicalsnapdir|pg_ls_replslotdir"
    r"|pg_available_wal_summaries|pg_wal_summary_contents"
    r"|binary_upgrade_create_empty_extension|binary_upgrade_create_conflict_detection_slot"
    r"|binary_upgrade_replorigin_advance|binary_upgrade_add_sub_rel_state"
    r"|binary_upgrade_set_missing_value|binary_upgrade_set_record_init_privs"
    r"|binary_upgrade_set_next_pg_type_oid|binary_upgrade_set_next_array_pg_type_oid"
    r"|binary_upgrade_set_next_heap_pg_class_oid|binary_upgrade_set_next_heap_relfilenode"
    r"|binary_upgrade_set_next_index_pg_class_oid|binary_upgrade_set_next_index_relfilenode"
    r"|binary_upgrade_set_next_multirange_pg_type_oid"
    r"|binary_upgrade_set_next_multirange_array_pg_type_oid"
    r"|binary_upgrade_set_next_pg_authid_oid|binary_upgrade_set_next_pg_enum_oid"
    r"|binary_upgrade_set_next_pg_tablespace_oid"
    r"|binary_upgrade_set_next_toast_pg_class_oid|binary_upgrade_set_next_toast_relfilenode"
    r"|pg_stop_making_pinned_objects"
    r"|pg_disable_data_checksums|pg_enable_data_checksums"
    r"|pg_copy_physical_replication_slot|pg_copy_logical_replication_slot"
    r"|pg_log_backend_memory_contexts|pg_log_standby_snapshot"
    r"|pg_replication_origin_session_setup|pg_replication_origin_xact_setup"
    r"|pg_sync_replication_slots"
    r"|txid_current|pg_current_xact_id"
    r"|pg_stat_force_next_flush|pg_extension_config_dump"
    r"|pg_file_read|pg_file_write|pg_file_rename|pg_file_unlink|pg_file_ls"
    r"|set_config|pg_notify|setseed"
    r"|ts_stat|query_to_xml_and_xmlschema|query_to_xmlschema|query_to_xml"
    r"|cursor_to_xmlschema|cursor_to_xml"
    r"|pg_sleep|pg_sleep_until|pg_sleep_for"
    r"|nextval|setval|pg_nextoid)\b", re.I)
SIDE_EFFECT_FUNCTIONS_SQL = re.compile(
    r"\b(pg_terminate_backend|pg_cancel_backend|pg_reload_conf|pg_rotate_logfile"
    r"|pg_read_file|pg_read_binary_file|pg_ls_dir|pg_stat_file|pg_current_logfile"
    r"|lo_import|lo_export|lo_put|largeobject"
    r"|lo_create|lo_creat|lo_open|lowrite|lo_truncate|lo_truncate64|lo_unlink|lo_from_bytea"
    r"|dblink|dblink_exec|postgres_fdw_connect"
    r"|pg_advisory_lock|pg_advisory_xact_lock|pg_try_advisory_lock|pg_try_advisory_xact_lock"
    r"|pg_advisory_unlock|pg_advisory_unlock_all"
    r"|pg_stat_statements_reset|pg_stat_reset|pg_stat_reset_shared|pg_stat_reset_slru"
    r"|pg_replication_origin_create|pg_replication_origin_drop"
    r"|pg_replication_origin_advance|pg_replication_origin_session_assign"
    r"|pg_create_restore_point|pg_switch_wal"
    r"|pg_wal_replay_pause|pg_wal_replay_resume|pg_promote"
    r"|brin_summarize_new_values|brin_summarize_range|brin_desummarize_range"
    r"|gin_clean_pending_list"
    r"|pg_create_physical_replication_slot|pg_create_logical_replication_slot"
    r"|pg_drop_replication_slot|pg_replication_slot_advance|pg_logical_emit_message"
    r"|pg_advisory_lock_shared|pg_try_advisory_lock_shared"
    r"|pg_try_advisory_xact_lock_shared|pg_advisory_unlock_shared"
    r"|pg_stat_reset_replication_slot|pg_stat_reset_subscription_stats"
    r"|pg_ls_waldir|pg_ls_archive_statusdir|pg_ls_logdir|pg_ls_tmpdir"
    r"|pg_backup_start|pg_backup_stop"
    r"|pg_restore_relation_stats|pg_restore_attribute_stats|pg_restore_extended_stats"
    r"|pg_clear_relation_stats|pg_clear_attribute_stats|pg_clear_extended_stats"
    r"|set_relation_stats|set_attribute_stats"
    r"|pg_import_system_collations"
    r"|pg_stat_reset_backend_stats|pg_stat_reset_single_table_counters"
    r"|pg_stat_reset_single_function_counters"
    r"|pg_advisory_xact_lock_shared"
    r"|pg_logical_slot_get_changes|pg_logical_slot_get_binary_changes"
    r"|pg_switch_xlog|pg_xlog_replay_pause|pg_xlog_replay_resume"
    r"|pg_export_snapshot|pg_import_snapshot"
    r"|pg_replication_origin_session_reset|pg_replication_origin_xact_reset"
    r"|pg_stat_clear_snapshot"
    r"|pg_ls_summariesdir|pg_ls_logicalmapdir|pg_ls_logicalsnapdir|pg_ls_replslotdir"
    r"|pg_available_wal_summaries|pg_wal_summary_contents"
    r"|binary_upgrade_create_empty_extension|binary_upgrade_create_conflict_detection_slot"
    r"|binary_upgrade_replorigin_advance|binary_upgrade_add_sub_rel_state"
    r"|binary_upgrade_set_missing_value|binary_upgrade_set_record_init_privs"
    r"|binary_upgrade_set_next_pg_type_oid|binary_upgrade_set_next_array_pg_type_oid"
    r"|binary_upgrade_set_next_heap_pg_class_oid|binary_upgrade_set_next_heap_relfilenode"
    r"|binary_upgrade_set_next_index_pg_class_oid|binary_upgrade_set_next_index_relfilenode"
    r"|binary_upgrade_set_next_multirange_pg_type_oid"
    r"|binary_upgrade_set_next_multirange_array_pg_type_oid"
    r"|binary_upgrade_set_next_pg_authid_oid|binary_upgrade_set_next_pg_enum_oid"
    r"|binary_upgrade_set_next_pg_tablespace_oid"
    r"|binary_upgrade_set_next_toast_pg_class_oid|binary_upgrade_set_next_toast_relfilenode"
    r"|pg_stop_making_pinned_objects"
    r"|pg_disable_data_checksums|pg_enable_data_checksums"
    r"|pg_copy_physical_replication_slot|pg_copy_logical_replication_slot"
    r"|pg_log_backend_memory_contexts|pg_log_standby_snapshot"
    r"|pg_replication_origin_session_setup|pg_replication_origin_xact_setup"
    r"|pg_sync_replication_slots"
    r"|txid_current|pg_current_xact_id"
    r"|pg_stat_force_next_flush|pg_extension_config_dump"
    r"|pg_file_read|pg_file_write|pg_file_rename|pg_file_unlink|pg_file_ls"
    r"|set_config|pg_notify|setseed"
    r"|ts_stat|query_to_xml_and_xmlschema|query_to_xmlschema|query_to_xml"
    r"|cursor_to_xmlschema|cursor_to_xml"
    r"|nextval|setval|pg_nextoid)\b", re.I)
_HEAD_WORD = re.compile(r"^\s*(\w+)")


def mask_literals(text: str) -> str:
    """Blank out string/dollar-quote literal BODIES and comments so keyword guards
    match only what the server would parse as SQL — 'insert into x' inside a WHERE
    literal is data, not a write (R2-C finding: the unmasked smuggle regex
    false-positived there). Double-quoted identifiers are the OPPOSITE case
    (round-3 review F1): the quotes are syntax, not data — only the quote
    characters are blanked and the identifier body is kept verbatim, so
    "pg_read_file"('x') and pg_read_file('x') are indistinguishable to the
    blacklist regexes (masking the body blinded every side-effect blacklist to
    quoted function names; PG accepts that call syntax).
    U&"..." Unicode-escaped identifiers follow the same strategy (round-4 D-F1):
    the token is replaced by space+DECODED body+space — U&"pg_\\0072ead_file"(…)
    must be exactly as visible to the blacklists as "pg_read_file"(…). The escape
    character defaults to \\ and may be overridden by a trailing UESCAPE 'c'
    clause; anything undecodable raises ValueError (fail-safe = refuse).
    U&'...' by contrast is a Unicode STRING literal: data masking applies (quotes
    kept, body blanked; the U& prefix characters are harmless). Identifiers glued
    to a preceding identifier character (aU&"x" lexes as aU & "x") are NOT U&
    tokens — their quotes fall through to the plain double-quote branch above.
    Known, accepted tradeoff (defense first — refusing beats releasing): after
    unquoting/decoding, a plain identifier reference whose body collides with a
    write keyword or a blacklisted function name (a column literally named
    "pg_sleep", select "into" from t) is rejected too. Documented limitation, do
    not "fix" by re-masking identifier bodies."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":
            j = text.find("\n", i)
            j = n if j == -1 else j
            out.append(" ")
            i = j
            continue
        if ch == "/" and nxt == "*":
            depth, j = 1, i + 2
            while j < n and depth:
                if text[j:j + 2] == "/*":
                    depth += 1
                    j += 2
                elif text[j:j + 2] == "*/":
                    depth -= 1
                    j += 2
                else:
                    j += 1
            if depth:
                raise ValueError("未闭合的 /* 块注释")
            out.append(" ")
            i = j
            continue
        if ch == "'":
            end = _single_quoted_end(text, i)      # shared with split_statements (F3)
            out.append("'" + " " * max(end - i - 2, 0) + "'")
            i = end
            continue
        if ch in "Uu" and text[i + 1:i + 3] == '&"':
            prev = text[i - 1] if i else ""
            if not prev or prev not in _IDENT_CHARS:
                # U&"..." Unicode-escaped IDENTIFIER (round-4 D-F1): token replaced
                # by space+decoded body+space, mirroring the plain double-quote
                # "unquote, keep the body" strategy — escapes resolved, so the
                # blacklists see the real server-side name.
                end = _skip_quoted(text, i + 2, '"', '""')
                escape, after = _uescape_after_literal(text, end)
                out.append(" " + _decode_unicode_escapes(text[i + 3:end - 1], escape) + " ")
                i = after
                continue
            # else: U& glued to an identifier char is not a prefix — the quote
            # falls through to the plain branch below (body kept verbatim)
        if ch == '"':
            end = _skip_quoted(text, i, '"', '""')
            out.append(" " + text[i + 1:end - 1] + " ")   # quotes blanked, body kept (F1)
            i = end
            continue
        if ch == "$":
            tag = _dollar_opener_at(text, i, out)
            if tag is not None:
                closer = f"${tag}$"
                j = text.find(closer, i + len(closer))
                if j == -1:
                    raise ValueError(f"未闭合的 dollar-quote {closer}")
                end = j + len(closer)
                out.append(text[i:i + len(closer)] + " " * max(end - i - 2 * len(closer), 0)
                           + text[end - len(closer):end])
                i = end
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _skip_quoted(text: str, start: int, quote: str, escape: str) -> int:
    """Index just past the closing quote of a simple doubled-quote-escape literal."""
    j, n = start + 1, len(text)
    while j < n:
        if text.startswith(escape, j):
            j += len(escape)
            continue
        if text[j] == quote:
            return j + 1
        j += 1
    raise ValueError(f"未闭合的 {quote} 字符串")


def _skip_ws_and_comments(text: str, pos: int) -> int:
    """Skip lexer whitespace: spaces, -- line comments and nestable /* */ block
    comments (the PG lexer treats all of these as token separators)."""
    i, n = pos, len(text)
    while i < n:
        ch = text[i]
        if ch in _LEXER_SPACE:
            i += 1
        elif ch == "-" and text[i + 1:i + 2] == "-":
            j = text.find("\n", i)
            i = n if j == -1 else j
        elif ch == "/" and text[i + 1:i + 2] == "*":
            depth, j = 1, i + 2
            while j < n and depth:
                if text[j:j + 2] == "/*":
                    depth += 1
                    j += 2
                elif text[j:j + 2] == "*/":
                    depth -= 1
                    j += 2
                else:
                    j += 1
            if depth:
                raise ValueError("未闭合的 /* 块注释")
            i = j
        else:
            break
    return i


def _uescape_after_literal(text: str, end: int) -> tuple[str, int]:
    """Escape character declared by a UESCAPE clause following a U& literal, plus
    the scan position just past that clause (round-4 D-F1).

    Returns the PG default "\\" and `end` when no clause is present. The clause may
    be glued to the literal or separated by whitespace/comments (live-verified PG
    behavior). Fail-safe: anything that scans like the UESCAPE keyword but is not
    followed by a plain 'c' single-character string raises ValueError — guessing
    the wrong escape character would silently decode the body differently from the
    server. Valid escape characters exclude hex digits (they would be unreadable
    inside \\XXXX), quote/double-quote, whitespace and '+' (it would collide with
    the \\+XXXXXX six-digit form) — all live-verified PG rejections.
    """
    j = _skip_ws_and_comments(text, end)
    if text[j:j + 7].lower() != "uescape" or (j + 7 < len(text) and text[j + 7] in _IDENT_CHARS):
        return "\\", end
    j = _skip_ws_and_comments(text, j + 7)
    m = _UESCAPE_STRING.match(text, j)
    if not m:
        raise ValueError("UESCAPE 子句残缺(应为 UESCAPE '单字符')")
    c = m.group(1)
    if len(c) != 1 or c in _HEX_DIGITS or c in "'\"+" or c in _LEXER_SPACE:
        raise ValueError(f"非法 UESCAPE 转义符 {c!r}")
    return c, m.end()


def _decode_unicode_escapes(body: str, escape: str) -> str:
    """Decode PG Unicode escape sequences in a U& literal body: escape+4 hex digits
    or escape+'+'+6 hex digits → the code point, doubled escape → the literal
    escape character. Malformed sequences raise ValueError (PG rejects them at
    parse time with \"invalid Unicode escape\"); so do surrogate code points (PG
    pairs \\D800\\DC00 halves, we refuse instead — a decode we cannot vouch for
    must fail closed, never silently diverge from the server), anything above
    U+10FFFF, and code point 0 (round-5 D5-F9: PG rejects \\0000 too — the tool
    layer must not decode chr(0) into the masked text either). No new
    dependencies: chr(int(hex, 16)) only."""
    out: list[str] = []
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch != escape:
            out.append(ch)
            i += 1
            continue
        nxt = body[i + 1:i + 2]
        if nxt == escape:                       # doubled escape character → literal
            out.append(escape)
            i += 2
            continue
        if nxt == "+":
            digits, width = body[i + 2:i + 8], 6
        else:
            digits, width = body[i + 1:i + 5], 4
        if len(digits) != width or any(d not in _HEX_DIGITS for d in digits):
            raise ValueError("非法 Unicode 转义(须为 \\XXXX 或 \\+XXXXXX 十六进制)")
        cp = int(digits, 16)
        if cp == 0 or 0xD800 <= cp <= 0xDFFF or cp > 0x10FFFF:
            raise ValueError(f"非法 Unicode 码点 U+{cp:04X}")
        out.append(chr(cp))
        i += 1 + (1 if nxt == "+" else 0) + width
    return "".join(out)


def statement_head(stmt: str) -> str:
    """First keyword of a (comment-stripped) statement, lowercased."""
    m = _HEAD_WORD.match(stmt)
    return m.group(1).lower() if m else ""


def find_side_effect_calls(sql: str, *, surface: str = "analyze") -> list[str]:
    """Side-effect function calls in the statement (literal bodies masked first;
    string data is invisible, but double-quoted function names ARE visible —
    "lo_import"(...) matches lo_import, round-3 F1).
    surface='sql' uses the tighter list (statement_timeout already caps pg_sleep);
    surface='analyze' also blocks sleep-family calls (EXPLAIN ANALYZE would run them)."""
    masked = mask_literals(sql)
    regex = SIDE_EFFECT_FUNCTIONS_SQL if surface == "sql" else SIDE_EFFECT_FUNCTIONS
    return sorted(set(m.group(1).lower() for m in regex.finditer(masked)))
