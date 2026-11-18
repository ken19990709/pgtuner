"""Unit tests for guard_raw_cli (PreToolUse hook) gate rules: deny bare psql/pgbench, allow "mention without executing",
escape hatch, fail-open. Script contract: stdin event JSON → exit 0 allows / exit 2 blocks."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "guard_raw_cli.py"


def _run(command: str | None, *, raw_stdin: str | None = None) -> int:
    stdin = raw_stdin if raw_stdin is not None else json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": command}})
    return subprocess.run([sys.executable, str(SCRIPT)], input=stdin,
                          capture_output=True, text=True).returncode


def test_denies_bare_psql_variants():
    assert _run(r"psql -h /tmp -p 5433 -d cl1 -c 'SELECT 1'") == 2
    assert _run("time psql -c 'SELECT 1'") == 2                       # wrapped in time
    assert _run("/usr/lib/postgresql/19/bin/psql -c 'SELECT 1'") == 2  # full path
    assert _run("echo hi; psql -c 'SELECT 1'") == 2                   # second segment of a compound command


def test_denies_pgbench_with_env_prefix():
    assert _run("PGOPTIONS='-c fsync=off' pgbench -c 10 -T 30 cl1") == 2
    assert _run("pgbench -i -s 10 cl1") == 2


def test_passes_mentions_and_sanctioned_tools():
    assert _run("echo psql is banned in this repo") == 0              # mention without executing
    assert _run("grep -n psql README.md") == 0
    assert _run('pgtuner sql --db cl1 "SELECT count(*) FROM t"') == 0
    assert _run("pg_dump --dbname=cl1 -f /tmp/dump.sql && echo done") == 0
    assert _run('db=$(pgtuner lab clone --template bench_tpl) && pgtuner sql --db "$db" "SELECT 1"') == 0


def test_escape_hatch_comment_allows():
    assert _run("psql -c 'SELECT 1' # allow-raw-psql") == 0
    assert _run("pgbench -c 10 cl1  # allow-raw-psql") == 0
    assert _run("psql -c 'SELECT 1' #allow-raw-psql") == 0            # no-space tail form


def test_escape_hatch_only_honored_at_line_tail():
    """Adversarial review R1-D: the escape string anywhere in the command used to
    disarm the whole check — `echo "# allow-raw-psql" > f && psql ...` slipped through."""
    assert _run('echo "# allow-raw-psql" > /tmp/x && psql -c "SELECT 1"') == 2
    assert _run('psql -c "SELECT 1" && echo "# allow-raw-psql"') == 2   # not at the tail


def test_denies_common_wrappers_and_shell_c_payloads():
    """Adversarial review R1-D: wrapper gap — timeout/xargs/nohup/stdbuf/env and
    `bash -c "… psql …"` payloads used to bypass the deny."""
    assert _run("timeout 30 psql -h /tmp -c 'SELECT 1'") == 2
    assert _run("nohup psql -c 'SELECT 1' >/dev/null 2>&1 &") == 2
    assert _run("env PGPASSWORD=x psql -c 'SELECT 1'") == 2
    assert _run("stdbuf -oL psql -c 'SELECT 1'") == 2
    assert _run("find . | xargs psql") == 2
    assert _run('bash -c "psql -c \'SELECT 1\'"') == 2                  # payload re-scanned
    assert _run("sh -c 'pgbench -T 5'") == 2


def test_fail_open_on_bad_input():
    assert _run(None, raw_stdin="not json at all") == 0
    assert _run(None, raw_stdin=json.dumps({"tool_name": "Bash"})) == 0          # no tool_input
    assert _run(None, raw_stdin=json.dumps({"tool_input": {"command": 42}})) == 0  # command not a string


def test_deny_message_points_to_pgtuner_and_escape():
    stdin = json.dumps({"tool_input": {"command": "psql -c 'SELECT 1'"}})
    p = subprocess.run([sys.executable, str(SCRIPT)], input=stdin,
                       capture_output=True, text=True)
    assert p.returncode == 2
    assert "pgtuner --help" in p.stderr and "# allow-raw-psql" in p.stderr


def test_quoted_separators_are_data_not_boundaries():
    """Adversarial review R2-A: the old splitter cut at `;`/`|` inside quoted
    strings, so a segment starting with a quoted-in psql word was denied —
    it even blocked the repo's own sanctioned `pgtuner sql "select 'psql' ..."`."""
    assert _run('echo "use pgtuner; psql is banned"') == 0
    assert _run("pgtuner sql \"select 'psql' from logs where msg like '%; psql next%'\"") == 0
    assert _run("echo 'a | b; psql'") == 0                       # single-quoted ; and |
    assert _run('echo "it\'s; fine" | grep x') == 0              # apostrophe inside double quotes


def test_denies_brace_group_compound():
    """Adversarial review R2-B1: `{ psql; }` used to slip through because
    `{` / `}` were not segmentation boundaries."""
    assert _run("{ psql; }") == 2
    assert _run("{ psql -c 1; }") == 2


def test_denies_eval_payload():
    """Adversarial review R2-B2: eval re-parses its argument as a shell
    command line, so the quoted payload must be rescanned like `bash -c`."""
    assert _run('eval "psql -c \'SELECT 1\'"') == 2
    assert _run("eval 'psql -c 1'") == 2
    assert _run('eval "echo hi; psql -c 1"') == 2                 # compound payload


def test_denies_exec_dash_a_alias():
    """Adversarial review R2-B3: `exec -a NAME` — NAME is a value argument of
    the wrapper, not the command; the scan must skip past it."""
    assert _run("exec -a x psql -c 1") == 2


def test_denies_tracers_and_script_c_payload():
    """Adversarial review R2-B4: strace/ltrace wrap a locally executed
    process; `script -c CMD` (also via bundled -qec) runs CMD locally."""
    assert _run("strace psql -c 'SELECT 1'") == 2
    assert _run("strace -o /tmp/t.txt -e trace=write psql -c 1") == 2
    assert _run("ltrace psql -c 1") == 2
    assert _run("ltrace -o /tmp/l.txt psql -c 1") == 2
    assert _run('script -qec "psql -c 1" /tmp/typescript') == 2
    assert _run('script -c "psql -c 1" /tmp/typescript') == 2


def test_subshell_execution_still_denied():
    """$() / backticks / ( ) run psql in a subshell ON THIS MACHINE — the
    pre-existing deny is pinned here so it cannot regress silently."""
    assert _run("out=$(psql -c 'SELECT 1')") == 2
    assert _run("out=`psql -c 'SELECT 1'`") == 2
    assert _run("( psql -c 1 )") == 2
    assert _run("(psql -c 1)") == 2


def test_declared_boundaries_allowed():
    """Honest edges declared in the script header (out of scope BY DESIGN):
    variable indirection is statically unresolvable; an ssh remote command
    executes on another machine; $-expansion inside double quotes is not
    parsed (sanctioned simplification)."""
    assert _run("P=psql; $P -c 1") == 0
    assert _run("cat q.sql | ssh host psql -f -") == 0
    assert _run('echo "$(psql -c 1)"') == 0


def test_no_false_positive_floor():
    """Regression floor: legitimate day-to-day commands (mentions, sanctioned
    tools, quotes containing separators) must all stay allowed."""
    assert _run("grep -n psql README.md") == 0
    assert _run("echo psql is banned") == 0
    assert _run('pgtuner sql --instance rel19 "SELECT 1"') == 0
    assert _run("pg_dump --dbname=cl1 -f /tmp/dump.sql") == 0
    assert _run("cat setup.sh | grep psql") == 0
    assert _run('db=$(pgtuner lab clone --template bench_tpl) && pgtuner sql --db "$db" "SELECT 1"') == 0
    assert _run("echo $(date) psql") == 0                         # psql is echo's argument, not a command
    assert _run("awk '{print $1}' log.txt") == 0                  # braces/quotes inside quotes are data


def test_denies_leading_redirect_prefixes():
    """Adversarial review R3-D3.1: a redirection token before the command
    made the scan bail out on it as an ordinary command, so the wrapped
    psql was never reached."""
    assert _run("2>&1 psql -c 'SELECT 1'") == 2
    assert _run("< /dev/null psql -c 1") == 2
    assert _run("> out.txt psql -c 1") == 2
    assert _run(">> log.txt psql -c 1") == 2
    assert _run("2>err.txt psql -c 1") == 2                       # glued fd+operator+target
    assert _run("2<> fifo psql -c 1") == 2                        # read-write redirect
    assert _run("</dev/null psql -c 1") == 2                      # fully glued
    assert _run("&> all.log psql -c 1") == 2                      # stdout+stderr to file
    assert _run("2>&1 bash -c 'psql -c 1'") == 2                  # redirect + rescan payload


def test_redirects_on_legal_commands_still_allowed():
    """R3-D3.1 must not over-block: redirections attached to sanctioned
    commands are stripped and the command itself is still examined."""
    assert _run("pgtuner sql 'select 1' > /tmp/out.txt") == 0
    assert _run("grep foo < in.txt") == 0
    assert _run("pgtuner bench --db cl1 > /tmp/b.log 2>&1") == 0
    assert _run("cat in.txt | grep psql > out.txt") == 0
    assert _run("echo a > b; grep psql README.md") == 0           # redirect eats only its own target word


def test_denies_quote_spliced_and_escaped_command_words():
    """Adversarial review R3-D3.2: bash splices adjacent quotes and
    unescapes \\x in unquoted words, so these all execute psql even though
    the bare token never equals "psql"."""
    assert _run("'p'sql --version") == 2
    assert _run('p""sql --version') == 2
    assert _run('ps""ql --version') == 2
    assert _run("\\psql -c 1") == 2
    assert _run("ps\\ql --version") == 2


def test_denies_backslash_newline_continuation():
    """Adversarial review R3-C-F5: `ps\\<LF>ql --version` folds to
    `psql --version` in bash; the hook must fold before scanning."""
    assert _run("ps\\\nql --version") == 2
    assert _run("echo hi && ps\\\nql -c 1") == 2


def test_denies_su_dash_c_payload():
    """Adversarial review R3-C-F6: `su USER -c PAYLOAD` runs the payload
    locally — same treatment as `script -c` (sudo -u was already caught)."""
    assert _run("su postgres -c 'psql -c 1'") == 2
    assert _run("su - postgres -c 'psql -c 1'") == 2              # legacy login dash
    assert _run("su -c 'psql -c 1' postgres") == 2                # payload before the user name
    assert _run("su -c 'pgbench -T 5' postgres") == 2
    assert _run("sudo -u postgres psql -c 'SELECT 1'") == 2       # pinned: sudo path unchanged


def test_heredoc_body_leading_psql_still_denied():
    """Adversarial review R3-C-F8 (declared boundary): a line-leading psql
    inside a heredoc body stays DENIED on purpose — `bash run.sh` would not
    be rescanned, so blocking is the only safe side of that edge."""
    assert _run("cat > run.sh <<'EOF'\npsql --version\nEOF") == 2


def _nested_alt_quotes(n: int) -> str:
    """Properly escaped alternating-quote nesting: every layer really wraps
    the next, so the chain genuinely executes 21+ levels deep and only the
    rescan depth cap stops it. (The old builder spliced raw mixed quotes
    without escaping — bash hits a syntax error at layer two and nothing
    executes; those chains were denied purely by tokenizer-fragment luck,
    which D7-F6's quote-aware words removed.)"""
    cmd = "psql -c 1"
    for i in range(n):
        if i % 2 == 0:
            cmd = "bash -c '" + cmd.replace("'", "'\\''") + "'"
        else:
            cmd = 'bash -c "' + cmd.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return cmd


def _nested_escaped_dquotes(n: int) -> str:
    cmd = "psql"
    for _ in range(n):
        cmd = 'bash -c "' + cmd.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return cmd


def _run_timed(command: str) -> tuple:
    """Same subprocess contract as _run, plus the wall-clock duration — the
    feed for the hardware-normalized ratio budgets below."""
    stdin = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    t = time.monotonic()
    p = subprocess.run([sys.executable, str(SCRIPT)], input=stdin,
                       capture_output=True, text=True)
    return p.returncode, time.monotonic() - t


def _run_timed_min(command: str, repeat: int = 3) -> float:
    """min of `repeat` timed runs of the same command — the contention-robust
    estimator for the ratio budgets (why min and not mean/median: a single
    sample per side compares MISMATCHED conditions under CPU contention —
    see test_deeply_nested_payload_denied_quickly for the B2 evidence)."""
    return min(_run_timed(command)[1] for _ in range(repeat))


def _flat_payload_baseline(size: int) -> str:
    """A same-size FLAT baseline command: ONE `bash -c` payload of plain
    data, no nesting — the linear-scan cost of the same number of bytes
    minus the nesting, i.e. the reference the ratio budgets compare
    against. (A giant `echo <arg>` would NOT work as a baseline: the scan
    correctly stops at the ordinary command word `echo` and never touches
    the giant argument, so it would measure process spawn, not scanning.)"""
    return "bash -c '" + "a" * (size - 10) + "'"


def test_deeply_nested_payload_denied_quickly():
    """Adversarial review R3-D3.3: deeply nested `bash -c "..."` chains
    used to run the hook into a timeout (fail-open) or OOM. They must be
    DENIED, fast — the 22-level re-escaped chain is an 8 MB command line.
    C8-F4 then pinned per-case absolute budgets (<2.0 s per case, <4.0 s
    total) — which measured 1.9 s locally but 2.8–7.3 s on GitHub 2-core
    CI runners, reddening the whole matrix: absolute wall-clock budgets
    are UNSAFE on slow hardware (the lesson of that round, stated
    plainly). Replaced by hardware-normalized checks: every fixture still
    denies; each multi-KB fixture must stay under 3x the time of a
    SAME-SIZE flat baseline (see _flat_payload_baseline — the
    hardware-independent form of "no superlinear blow-up"); one loose
    absolute total (<15 s) survives purely as a pathological-hang
    backstop. The 8 MB fixture is O(1)-rejected by the input-size cap now
    (see test_oversized_input_rejected_in_constant_time), so both sides
    of its ratio are constant-time today — if the cap is ever lifted the
    same ratio re-arms as a real linear-vs-nested scan comparison.
    Review B2 then broke the SINGLE-sample ratio under CPU contention
    (measured: a 30-sample stress run put 2 ratios ≥ 3.0, and the same
    load reddened 1 of 6 real pytest runs — failure shape: the flat
    baseline's one sample lands in a quiet window while the nested side's
    one sample eats contention, i.e. mismatched conditions, not a real
    blow-up). Both sides therefore run 3x and take the min (k stays 3):
    min filters contention spikes, letting each side catch its own quiet
    window so the ratio compares quiet-vs-quiet; taking min on the nested
    side is also safe — a genuinely superlinear scan stays slow in every
    sample, so min cannot manufacture a false pass."""
    t0 = time.monotonic()
    scanned = (                                          # under the 1 MiB size cap: really scanned
        (_nested_alt_quotes(21), "alternating-quote nesting beyond the 20-level cap, 352 KB"),
        (_nested_escaped_dquotes(18), "real bash recursion shape, 512 KB (under the cap)"),
    )
    size_capped = (
        (_nested_escaped_dquotes(22), "real bash recursion shape, 8 MB (O(1) size-cap deny)"),
    )
    quick = (
        ("bash -c 'bash -c \"psql\"'; " * 1000, "linear-size deep chain (1000x2 levels)"),
        ("bash -c " * 60 + "psql", "flat repeated shells"),
    )
    for cmd, note in scanned + size_capped + quick:
        assert _run(cmd) == 2, note                      # R3-D3.3: deny, never time out
    for cmd, note in scanned + size_capped:
        t_nested = _run_timed_min(cmd)                   # min-of-3 each side (B2): ratio vs
        t_flat = _run_timed_min(_flat_payload_baseline(len(cmd)))   # same-size flat baseline
        assert t_nested < 3 * t_flat, note
    assert time.monotonic() - t0 < 15.0                  # pathological-hang backstop ONLY


def test_oversized_input_rejected_in_constant_time():
    """Input-size fast-reject (this round's CI-portability fix): commands
    longer than _MAX_COMMAND_CHARS (1 Mi characters = 8x the Linux
    MAX_ARG_STRLEN of 128 KiB per single argument string — no real command
    line can be that long) are denied fail-closed BEFORE any scanning, so
    multi-MB pathological samples cost O(1) regardless of content (this
    fixture has no psql at all and is still denied — the gate judges size,
    not content). The DETERMINANT of this test is the rejection message
    (the size deny asserted below); the absolute timer is only a
    pathological-hang backstop, not a performance claim — B2's loaded-CPU
    run measured 0.76 s worst-case (a mere 1.3x margin against the old
    1.0 s), so the backstop sits at 3.0 s. The O(1) path itself still
    does no per-character work (spawn + C-speed JSON parse only); the
    exact-cap boundary is still scanned, not size-rejected, and a clean
    command there allows as before."""
    sys.path.insert(0, str(SCRIPT.parent))
    import guard_raw_cli as g
    oversized = "echo '" + "a" * g._MAX_COMMAND_CHARS + "'"    # cap+7 chars (B2 nit: not "one")
    assert len(oversized) > g._MAX_COMMAND_CHARS
    t = time.monotonic()
    stdin = json.dumps({"tool_input": {"command": oversized}})
    p = subprocess.run([sys.executable, str(SCRIPT)], input=stdin,
                       capture_output=True, text=True)
    assert p.returncode == 2 and "pgtuner --help" in p.stderr   # fail-closed, with the pointer
    assert time.monotonic() - t < 3.0                           # hang backstop only (see docstring)
    at_cap = "echo '" + "a" * (g._MAX_COMMAND_CHARS - 7) + "'"  # exactly at the cap
    assert len(at_cap) == g._MAX_COMMAND_CHARS
    assert _run(at_cap) == 0                                   # boundary not size-rejected: scanned, clean


def test_size_cap_edge_pins():
    """Review B2: two size-cap behaviors lived only in comments — pin them.
    (1) The `# allow-raw-psql` escape hatch does NOT apply past the cap:
    the size gate runs before the escape-tail scan, so an oversized
    command with the hatch at its tail is still denied WITH THE SIZE
    MESSAGE (a >1 MiB "command" cannot be executed by the shell anyway —
    the hatch would only ever disarm a scan that never happens).
    (2) A command exactly AT the cap is NOT size-rejected: it walks the
    normal scan path, so a banned word there denies with the ORDINARY
    message, not the size one."""
    sys.path.insert(0, str(SCRIPT.parent))
    import guard_raw_cli as g
    oversized = "echo '" + "a" * g._MAX_COMMAND_CHARS + "' # allow-raw-psql"
    assert len(oversized) > g._MAX_COMMAND_CHARS
    stdin = json.dumps({"tool_input": {"command": oversized}})
    p = subprocess.run([sys.executable, str(SCRIPT)], input=stdin,
                       capture_output=True, text=True)
    assert p.returncode == 2 and g._SIZE_MSG in p.stderr       # size deny; hatch not applied
    at_cap = "psql -c '" + "a" * (g._MAX_COMMAND_CHARS - 10) + "'"   # exactly at the cap
    assert len(at_cap) == g._MAX_COMMAND_CHARS
    stdin = json.dumps({"tool_input": {"command": at_cap}})
    p = subprocess.run([sys.executable, str(SCRIPT)], input=stdin,
                       capture_output=True, text=True)
    assert p.returncode == 2 and g.DENY_MSG in p.stderr        # scan path: ordinary deny
    assert g._SIZE_MSG not in p.stderr


def test_rescan_depth_cap_fails_closed():
    """R3-D3.3 unit contract: the rescan recursion cap raises (deny with
    the dedicated message), it never silently allows past the cap."""
    sys.path.insert(0, str(SCRIPT.parent))
    import guard_raw_cli as g
    assert g.violates("psql --version") is True
    assert g.violates("grep psql foo.txt") is False
    assert g._violates_core("psql", g._MAX_DEPTH)                # at the cap, no recursion: still scans
    assert g._violates_core("bash -c 'echo hi'", g._MAX_DEPTH - 1) is False  # rescan landing exactly AT the cap
    try:
        g._violates_core("bash -c 'psql -c 1'", g._MAX_DEPTH)    # one rescan past the cap: raises
        raised = False
    except g._TooDeep:
        raised = True
    assert raised
    try:
        g._violates_core("psql", g._MAX_DEPTH + 1)                # past the cap, no recursion: raises
        raised = False
    except g._TooDeep:
        raised = True
    assert raised
    stdin = json.dumps({"tool_input": {"command": "bash -c " * 30 + "psql"}})
    p = subprocess.run([sys.executable, str(SCRIPT)], input=stdin,
                       capture_output=True, text=True)
    assert p.returncode == 2 and "pgtuner --help" in p.stderr


def test_denies_compound_keyword_command_positions():
    """Adversarial review R4-C-F2/D-F4: shell compound keywords hold a
    command position without being commands — the scan used to stop at the
    keyword as an "ordinary command", so every command slot behind
    if/then/elif/while/until/for/do/case/coproc/! was blind."""
    assert _run("if psql -c 1; then echo; fi") == 2
    assert _run("if true; then psql -c 1; fi") == 2
    assert _run("if false; then :; elif psql -c 1; fi") == 2
    assert _run("while :; do psql -c 1; done") == 2
    assert _run("until false; do psql -c 1; done") == 2
    assert _run("for f in *.sql; do psql -f $f; done") == 2
    assert _run("select f in *.sql; do psql -f $f; done") == 2     # same family as for
    assert _run("case x in x) psql -c 1;; esac") == 2
    assert _run("coproc psql -c 1") == 2
    assert _run("! psql -c 'SELECT 1'") == 2                       # pipeline negation
    assert _run("time ! psql -c 1") == 2                           # negation behind a wrapper
    assert _run("if ! psql -c 1; then echo; fi") == 2
    assert _run("while ! pgbench -T 5; do :; done") == 2
    assert _run("time for f in *.sql; do psql -f $f; done") == 2   # keyword behind wrapper


def test_compound_keyword_payloads_rescanned():
    """R4-C-F2 inside -c/eval payloads: compound-keyword blindness must not
    come back through a nested shell payload."""
    assert _run("bash -c 'if psql -c 1; then echo; fi'") == 2
    assert _run("eval 'while :; do psql -c 1; done'") == 2
    assert _run("bash -c 'case x in x) psql -c 1;; esac'") == 2


def test_denies_case_clause_positions():
    """R4-C-F2 case specifics: later clauses (after `;;`) and `|`-split
    multi-pattern lists put psql in a command position too; an
    unterminated pattern list carries across segments split at `|`/`$`."""
    assert _run("case x in a) echo;; b) psql -c 1;; esac") == 2     # second clause
    assert _run("case x in sql|pgbench) pgbench -T 5;; esac") == 2  # |-split pattern list


def test_compound_keywords_no_false_positives():
    """R4-C-F2 must not over-block legal compound commands: keywords are
    skipped, the real command word terminates the scan (its psql argument
    stays an argument); case PATTERN words and for WORD-LIST words without
    banned content stay allowed."""
    assert _run("if grep -q psql x; then echo; fi") == 0
    assert _run("if ! grep -q psql x; then echo; fi") == 0          # negated condition
    assert _run("for f in *.md; do grep psql $f; done") == 0
    assert _run("while grep -q psql x; do echo; done") == 0
    assert _run("until grep -q psql x; do echo; done") == 0
    assert _run("case x in x) grep psql f;; esac") == 0
    assert _run("case $c in psql) echo talk;; esac") == 0           # psql IS the pattern
    assert _run("if grep psql x; then echo; elif grep y x; then echo; fi") == 0


def test_for_word_list_banned_word_overdenied():
    """R4 declared over-rejection (safe side): for/in consume no value
    arguments, so a bare banned word inside the word list is denied even
    though bash treats it as data — the escape hatch still applies."""
    assert _run("for x in psql; do echo $x; done") == 2
    assert _run("for x in psql; do echo $x; done # allow-raw-psql") == 0


def test_ssh_local_hosts_scanned():
    """Adversarial review R4-D-F5: the declared "remote side never parses
    locally" boundary does not hold for localhost/127.0.0.1/::1 — psql
    runs on THIS machine, so the scan continues past the host word."""
    assert _run("ssh localhost psql -c 1") == 2
    assert _run("ssh 127.0.0.1 psql -c 1") == 2
    assert _run("ssh ::1 psql -c 1") == 2
    assert _run("ssh -p 2222 localhost psql -c 1") == 2             # options before host
    assert _run("nohup ssh localhost pgbench -T 5") == 2            # behind a wrapper
    assert _run("ssh localhost 'psql -c 1'") == 2                   # quoted remote payload


def test_ssh_remote_boundary_kept():
    """R4-D-F5 counterpart: non-local hosts keep the declared boundary
    (remote commands execute on another machine); local-host ssh stays
    usable for read-only talk."""
    assert _run("ssh otherhost psql -c 1") == 0                     # declared remote boundary
    assert _run("cat q.sql | ssh host psql -f -") == 0              # pinned R2 boundary
    assert _run("ssh localhost 'grep psql x'") == 0                 # mention-only remote payload
    assert _run("ssh localhost grep psql x") == 0


def test_reserved_words_bare_only_case_lookalikes_denied():
    """R5-C5-F1 (MAJOR): the scan used to match _RESERVED against the
    _base()-normalized word, so `./case` entered case-pattern mode and the
    following segment's psql was swallowed as a pattern word. bash only
    recognizes a reserved word as the BARE unquoted word — verified: each
    form below prints `case: command not found` while the NEXT segment
    still executes — so the scan stops at the lookalike (ordinary command)
    and scans the next segment fresh."""
    assert _run("./case; psql -c 1") == 2
    assert _run("'case'; psql -c 1") == 2
    assert _run('"ca"se; psql -c 1') == 2
    assert _run("\\case; psql -c 1") == 2
    assert _run("/usr/bin/case; psql -c 1") == 2
    assert _run("~/case | psql -c 1") == 2                          # pipe right side always runs
    assert _run("./do; psql -c 1") == 2                             # pinned: non-case keywords were already correct
    assert _run("./for; psql -c 1") == 2
    assert _run("case x in x) psql -c 1;; esac") == 2               # bare case unchanged: pattern mode still works
    assert _run("./case; grep psql f") == 0                         # no false positive: neighbor segment is data


def test_quoted_then_is_command_name_not_keyword():
    """R5-C5-F1 companion — assertion set by VERIFIED bash semantics (the
    instruction's arbiter clause): in `"then" psql -c 1` the quoted word is
    the COMMAND NAME — bash prints `then: command not found` and NOTHING
    else executes (psql is a mere argument, exactly like `grep psql f`) —
    so the hook allows it; with a real separator the second segment DOES
    execute psql and is denied."""
    assert _run('"then" psql -c 1') == 0
    assert _run('"then"; psql -c 1') == 2
    assert _run("'do' psql -c 1") == 0


def test_ssh_equivalent_local_host_words_denied():
    """R5-D5-F5: host-word equivalence — case (LOCALHOST), trailing dot
    (localhost. / 127.0.0.1.), IPv6 brackets ([::1]), 0.0.0.0 and 127.*
    shorthand IPv4 (127.1 == 127.0.0.1) all mean "runs on THIS machine"."""
    assert _run("ssh LOCALHOST psql -c 1") == 2
    assert _run("ssh localhost. psql -c 1") == 2
    assert _run("ssh 127.0.0.1. psql -c 1") == 2
    assert _run("ssh 127.1 psql -c 1") == 2
    assert _run("ssh [::1] psql -c 1") == 2
    assert _run("ssh 0.0.0.0 psql -c 1") == 2
    assert _run("ssh otherhost psql -c 1") == 0                     # remote boundary kept


def test_ssh_numeric_loopback_forms_denied():
    """C6-F1/D6-F3 (MAJOR): OpenSSH resolves the host word via getaddrinfo,
    whose inet_aton(3) numeric parser accepts integer / hex / octal /
    part-count-shorthand IPv4 and full-form IPv6. Every form below was
    VERIFIED with a probe script (getaddrinfo +, where ambiguous, a real
    connect test against a local listener) to resolve to 127.0.0.1 / ::1 —
    so the "remote" command runs on THIS machine and the scan must not
    stop at the host word."""
    assert _run("ssh 2130706433 psql -c 1") == 2        # decimal u32 0x7F000001
    assert _run("ssh 0x7f000001 psql -c 1") == 2        # hex u32
    assert _run("ssh 0X7F000001 psql -c 1") == 2        # uppercase hex prefix
    assert _run("ssh 0177.0.0.1 psql -c 1") == 2        # octal first octet
    assert _run("ssh 0x7f.1 psql -c 1") == 2            # hex + 2-part shorthand
    assert _run("ssh 0x7f.0.0.1 psql -c 1") == 2        # hex + 4-part mix
    assert _run("ssh 0177.1 psql -c 1") == 2            # octal 2-part (probe-confirmed)
    assert _run("ssh 0x7f.0.1 psql -c 1") == 2          # hex 3-part (probe-confirmed)
    assert _run("ssh 0:0:0:0:0:0:0:1 psql -c 1") == 2   # full-form IPv6 loopback
    assert _run("ssh ::0.0.0.1 psql -c 1") == 2         # embedded IPv4 == ::1 (probe-confirmed)  # hygiene:allow (parser fixture)
    assert _run("ssh ::ffff:127.0.0.1 psql -c 1") == 2  # IPv4-mapped loopback (probe: connects locally)
    assert _run("ssh 0:0:0:0:0:ffff:127.0.0.1 psql -c 1") == 2  # mapped, full-written
    assert _run("ssh 0177.0.0.1 'psql -c 1'") == 2      # quoted remote payload form
    assert _run("nohup ssh 2130706433 psql -c 1") == 2  # behind a wrapper


def test_ssh_numeric_nonlocal_hosts_allowed():
    """C6-F1 counterpart (probe-verified NON-loopback resolutions): numeric
    forms that resolve off this machine keep the declared remote boundary.
    0x7f → 0.0.0.127 (hygiene:allow — fixture; connect times out — first
    octet 0, not 127); 0x08080808 → 8.8.8.8 and 010.0.0.1 → 8.0.0.1 (hygiene:allow —
    fixtures); the deprecated
    IPv4-COMPATIBLE 0:0:0:0:0:0:127.0.0.1 is ::7f00:1 — a DIFFERENT
    address than ::1, connect network-unreachable. /etc/hosts loopback
    aliases stay allowed too: the hook does no name resolution (declared
    boundary, see script docstring)."""
    assert _run("ssh 192.168.1.5 psql -c 1") == 0       # plain LAN IPv4  # hygiene:allow (parser fixture)
    assert _run("ssh 0x08080808 psql -c 1") == 0        # hex → 8.8.8.8  # hygiene:allow (fixture)
    assert _run("ssh 010.0.0.1 psql -c 1") == 0         # octal → 8.0.0.1  # hygiene:allow (fixture)
    assert _run("ssh 0x7f psql -c 1") == 0              # → 0.0.0.127, connect times out  # hygiene:allow (fixture)
    assert _run("ssh 0:0:0:0:0:0:127.0.0.1 psql -c 1") == 0  # ::7f00:1 ≠ ::1
    assert _run("ssh otherhost psql -c 1") == 0         # pinned remote boundary
    assert _run("ssh ip6-localhost psql -c 1") == 0     # declared: no name resolution


def test_find_exec_command_word_denied():
    """R5-D5-F6: `find ... -exec/-execdir CMD ...` executes CMD on THIS
    machine once per match — the first word after the predicate is
    banned-checked (paths resolve too, per the R3-D3.2 word rules)."""
    assert _run("find . -exec psql \\;") == 2
    assert _run("find . -exec pgbench -T 5 \\;") == 2
    assert _run("find /tmp -name x -execdir psql -c 1 \\;") == 2
    assert _run("find . -exec /usr/bin/psql -c 1 \\;") == 2
    assert _run("find . -exec psql {} +") == 2                      # + terminator form
    assert _run("find . '-exec' psql \\;") == 2                     # quote-spliced predicate


def test_find_exec_grep_mention_allowed():
    """R5-D5-F6 no-false-positive side: only the FIRST word after -exec is
    judged — `-exec grep psql {}` keeps grep's psql argument legal; the
    pinned pipe form must keep working."""
    assert _run("find . -name x -exec grep psql {} \\;") == 0
    assert _run("find . -name x -exec grep -q psql {} +") == 0
    assert _run("find . -name '*.sql'") == 0                        # find without -exec
    assert _run("find . | xargs psql") == 2                         # pinned pipe form


def test_find_exec_shell_c_payload_denied():
    """C6-F4: the first word after -exec/-execdir, when it is a bash/sh
    family shell / eval / script / su (the _C_PAYLOAD family), gets the
    same -c option tracking and payload rescan as the wrapper path — find
    executes that command ON THIS MACHINE once per match, so a psql in
    its -c payload runs locally."""
    assert _run("find . -exec sh -c 'psql -c 1' \\;") == 2
    assert _run("find . -exec bash -c 'psql -c 1' \\;") == 2
    assert _run("find /tmp -name x -execdir sh -c 'pgbench -T 5' \\;") == 2
    assert _run("find . -exec sh -c 'psql -c 1' +") == 2            # + terminator form
    assert _run('find . -exec sh -c "psql -c 1" \\;') == 2          # double-quoted payload
    assert _run("find . -exec sh -c psql \\;") == 2                 # unquoted payload word
    assert _run("find . -exec eval 'psql -c 1' \\;") == 2           # eval: first arg is the payload
    assert _run("find . -exec su postgres -c 'psql -c 1' \\;") == 2  # su USER -c payload
    assert _run("find . -exec script -qec 'psql -c 1' /dev/null \\;") == 2  # script -qec
    assert _run("find . -exec sh -c 'echo hi' \\; -exec psql \\;") == 2     # clean payload: later clause still checked


def test_find_exec_shell_clean_payload_allowed():
    """C6-F4 no-false-positive side: clean shell payloads stay allowed, the
    pinned `-exec grep psql {}` form and the generic-interpreter declared
    boundary (python3 -c) are untouched."""
    assert _run("find . -exec sh -c 'grep psql f' \\;") == 0
    assert _run("find . -exec sh -c 'echo done' \\;") == 0
    assert _run("find . -exec bash -c 'echo psql is banned' \\;") == 0  # mention-only payload
    assert _run("find . -exec python3 -c 'print(1)' \\;") == 0      # declared interpreter boundary
    assert _run("find . -name x -exec grep psql {} \\;") == 0       # pinned
    assert _run("find . -exec grep psql {} \\;") == 0               # pinned red line


def test_coproc_name_word_skipped():
    """R5-D5-F7: `coproc [NAME] cmd ...` — the word right after coproc,
    when not a bare reserved word, holds no command slot: skip it and keep
    scanning. (bash 5.2 only takes a NAME before compound commands —
    verified — so denying `coproc XX psql` is the safe-direction
    over-deny, same family as the for-list one; `coproc XX { psql; }` is
    exact.)"""
    assert _run("coproc XX psql -c 1") == 2
    assert _run("coproc psql -c 1") == 2                            # no-NAME form stays denied
    assert _run("coproc XX { psql -c 1; }") == 2
    assert _run("coproc XX { pgbench -T 5; }") == 2
    assert _run("time coproc XX psql -c 1") == 2                    # behind a wrapper
    assert _run("coproc XX grep psql f") == 0                       # NAME + legal command


def test_generic_interpreter_payloads_declared_boundary():
    """R5-D5-F8 (declared boundary): only bash/sh-family -c, eval, script
    and su payloads are rescanned; generic interpreter payloads
    (python3 -c / perl -e) are opaque strings and stay unrescanned — the
    escape hatch and these boundary declarations are part of the contract."""
    assert _run('python3 -c "subprocess.run([\'psql\', \'--version\'])"') == 0
    assert _run("perl -e 'system(\"psql --version\")'") == 0
    assert _run('bash -c "psql -c 1"') == 2                         # rescanned family unchanged


def test_env_assignment_quoted_space_prefix_denied():
    """D7-F6 (MAJOR): the old space-split tokenizer turned `FOO='1 2' psql
    -c 1` into the fragments `FOO='1` + `2'`; the scan stopped on `2'` as
    an ordinary command and the wrapped psql really executed. Quote-aware
    word spans keep each quoted word whole, and the FOO=... assignment
    prefix skip keeps working on the whole word."""
    assert _run("FOO='1 2' psql -c 1") == 2
    assert _run("env VAR='a b' psql -c 'SELECT 1'") == 2
    assert _run('BAR="x y" pgbench --version') == 2


def test_env_assignment_quoted_space_no_false_positives():
    """D7-F6 no-false-positive side: legitimate assignment prefixes (plain
    or with quoted-space values) must not disturb the legal command behind
    them — pgtuner is not banned, grep keeps psql a mere argument."""
    assert _run("PGPASSWORD=x pgtuner sql 'select 1'") == 0
    assert _run("FOO='a b' grep psql f") == 0


def test_ssh_dash_o_exec_option_values_denied():
    """D7-F5 (MAJOR): ssh's ProxyCommand=/LocalCommand= option values are
    run through the LOCAL /bin/sh -c (review-verified on this box: a
    ProxyCommand marker file landed), so the post-= payload is rescanned —
    separate `-o KEY=V` form, glued `-oKEY=V` form (plain and quoted) and
    a nested shell payload included."""
    assert _run("ssh -o ProxyCommand='psql -c 1' host exit") == 2
    assert _run("ssh -oProxyCommand=psql host exit") == 2
    assert _run("ssh -oProxyCommand='psql -c 1' host exit") == 2
    assert _run("ssh -o LocalCommand='psql -c 1' host") == 2
    assert _run("ssh -o ProxyCommand=\"sh -c 'psql -c 1'\" host exit") == 2


def test_ssh_dash_o_plain_options_still_allowed():
    """D7-F5 no-false-positive side: ProxyJump= only names jump hosts (no
    command executes) and plain options + a non-local host keep the
    declared remote boundary — the "remote" command runs elsewhere."""
    assert _run("ssh -o ProxyJump=jumper host psql -c 1") == 0     # jump hosts don't execute
    assert _run("ssh -o ServerAliveInterval=5 host psql -c 1") == 0  # plain option + remote command


def test_find_bare_brace_placeholder_keeps_exec_state():
    """C7-F1 (MAJOR): `{` was a segment separator, so the bare `{}` —
    find's most common placeholder spelling, never a brace expansion
    without comma/range — cut `find . -exec grep x {} \\; -exec psql \\;`
    in two; the -exec state is segment-local, so the second clause
    escaped the check. `{}` is word material now; brace GROUPS still
    split (pinned deny), and the single clean -exec clause stays allowed."""
    assert _run("find . -exec grep x {} \\; -exec psql \\;") == 2
    assert _run("find . -name {} -exec psql \\;") == 2
    assert _run("find . -exec grep x {} \\; -exec sh -c 'psql' \\;") == 2
    assert _run("find . -exec grep psql {} \\;") == 0      # pinned: single clean -exec clause
    assert _run("{ psql; }") == 2                           # pinned: brace groups still split


def test_ssh_user_prefix_local_hosts_denied():
    """C7-F3: `ssh user@127.0.0.1 psql` used to pass — the user@ prefix
    hid the loopback host from the local-host test. The prefix is
    stripped before the locality check; non-local user@ targets keep the
    declared remote boundary."""
    assert _run("ssh user@127.0.0.1 psql") == 2
    assert _run("ssh root@localhost psql") == 2
    assert _run("ssh user@[::1] psql") == 2
    assert _run("ssh user@192.168.1.5 psql") == 0  # hygiene:allow (parser fixture, same as the R5 one)
    assert _run("ssh user@otherhost psql -c 1") == 0


def test_payload_env_assignment_words_denied():
    """C8-F1 (MAJOR): a -c-style payload word that itself contains `=` was
    swallowed whole by the assignment-prefix check BEFORE the payload
    rescan could run, and bash really executes it (review-verified:
    `bash -c "PGPASSWORD=x psql -c 1"` runs psql). The payload word now
    falls through to the rescan, whose own assignment skip then sees
    psql in command position. Eleven-form review matrix + prefix case."""
    assert _run("bash -c 'PGPASSWORD=x psql -c 1'") == 2              # single-quoted payload
    assert _run('bash -c "PGPASSWORD=x psql -c 1"') == 2              # double-quoted payload
    assert _run("eval 'PGPASSWORD=x psql -c 1'") == 2                 # eval payload
    assert _run("su postgres -c 'PGPASSWORD=x psql -c 1'") == 2       # su USER -c payload
    assert _run("su -c 'PGPASSWORD=x psql -c 1' postgres") == 2       # su -c PAYLOAD USER
    assert _run('script -qec "PGPASSWORD=x psql -c 1" /dev/null') == 2  # script -qec
    assert _run("ssh localhost 'PGPASSWORD=x psql -c 1'") == 2        # local-host ssh payload
    assert _run("find . -exec bash -c 'PGPASSWORD=x psql -c 1' \\;") == 2  # find -exec'd shell
    assert _run("cat f | xargs -I{} bash -c 'PGPASSWORD=x psql -c 1'") == 2  # behind xargs
    assert _run('bash -c "bash -c \'PGPASSWORD=x psql -c 1\'"') == 2  # nested -c payloads
    assert _run('bash -c "PGOPTIONS=\'-c x\' psql -c 1"') == 2        # quoted = value inside payload
    assert _run('PGOPTIONS=y bash -c "PGPASSWORD=x psql -c 1"') == 2  # real prefix + = payload


def test_payload_env_assignment_no_false_positives():
    """C8-F1 no-false-positive side: a legit assignment prefix INSIDE the
    payload is the recursion's business — `A=1 grep psql f` keeps psql a
    mere grep argument. eval joins ALL its arguments into one command
    string, so an eval `=`-word is rescanned but the scan CONTINUES (both
    joins below really run psql — bash-verified); the unquoted
    `bash -c FOO=bar psql` does NOT run psql (psql is $0 there —
    bash-verified: silent, no command-not-found) and is allowed."""
    assert _run('bash -c "A=1 grep psql f"') == 0
    assert _run("bash -c 'A=1 pgtuner sql \"select 1\"'") == 0
    assert _run("eval 'A=1 grep psql f'") == 0
    assert _run("eval 'A=1' psql -c 1") == 2                 # joins to `A=1 psql -c 1`
    assert _run("eval FOO=bar psql -c 1") == 2               # join approximation keeps the deny
    assert _run("bash -c FOO=bar psql -c 1") == 0            # bash truth: psql is $0, never executes


def test_ssh_dash_o_whole_value_quoted_denied():
    """C8-F2 (MAJOR): the ORDINARY spelling quotes the whole -o value; the
    key-prefix match now runs on the dequoted word, so single/double-
    quoted ProxyCommand/LocalCommand values are denied like the glued
    form (OpenSSH executes them through the local /bin/sh — review-
    verified with a marker file). Non-exec keys and plain options keep
    the remote boundary."""
    assert _run("ssh -o 'ProxyCommand=psql -c 1' host exit") == 2
    assert _run('ssh -o "ProxyCommand=psql -c 1" host exit') == 2
    assert _run("ssh -o 'LocalCommand=psql -c 1' host") == 2
    assert _run('ssh -o "LocalCommand=\'psql -c 1\'" host') == 2
    assert _run("ssh -o 'ProxyJump=jumper' host psql -c 1") == 0       # jump hosts don't execute
    assert _run("ssh -o 'ServerAliveInterval=5' host psql -c 1") == 0  # plain quoted option + remote


def test_ansi_c_quoted_command_words_denied():
    """D8-F2 (MAJOR): $'...' ANSI-C quoting is expanded statically — bash
    really executes the decoded word (verified: `$'\\x70rintf' hi` and the
    octal/unicode/spliced variants all ran), so escapes in command
    position, decoded paths and decoded -c payloads are denied."""
    assert _run("$'\\x70sql' -c 1") == 2                    # \x70 hex = 'p'
    assert _run("$'\\160sql' -c 1") == 2                    # octal 0160 = 'p'
    assert _run("$'\\u0070sql' -c 1") == 2                  # \u0070 = 'p'
    assert _run("ps$'q'l -c 1") == 2                        # splice mid-word
    assert _run("$'ps'ql -c 1") == 2                        # splice tail
    assert _run('$"psql" -c 1') == 2                        # locale quote, dquote semantics
    assert _run("$'/usr/bin/psql' -c 1") == 2               # decoded path -> basename
    assert _run("bash -c $'psql -c 1'") == 2                # decoded -c payload
    assert _run("bash -c $'PGPASSWORD=x psql -c 1'") == 2   # decoded payload containing =
    assert _run("echo ok | $'psql' -c 1") == 2              # pipe right side
    assert _run("find . -exec $'psql' \\;") == 2            # find -exec command word


def test_ansi_c_quoting_no_false_positives():
    """D8-F2 no-false-positive side: ANSI-C words in ARGUMENT position and
    payloads without banned content stay allowed (unknown escapes keep
    their backslash exactly like bash — verified: echo $'a\\qb' prints
    a\\qb); a $' word that cannot decode (unterminated span) is DENIED
    fail-closed — bash would reject the line and the hook must not guess."""
    assert _run("echo $'line1\\nline2'") == 0
    assert _run("awk $'{print $1}' log.txt") == 0           # awk program as one ANSI-C word
    assert _run("grep $'psql' file.txt") == 0               # ANSI mention as an argument
    assert _run("printf '%s\\n' $'psql is banned'") == 0    # pure data
    assert _run("echo $'a;psql'") == 0                      # decoded ; stays word data, not a separator
    assert _run("echo $'a\\qb'") == 0                       # unknown escape: backslash kept, data
    assert _run("x=$'psql'") == 0                           # assignment value, nothing executes
    assert _run("echo hi; $'psql") == 2                     # unterminated $' — fail-closed deny
    assert _run("bash -c $'psql -c 1") == 2                 # unterminated inside a payload too


def test_container_executors_declared_boundary():
    """C8-F3 (declared boundary, pinned as part of the docstring
    contract): container executors run their payload in a container
    namespace — off this machine's pgtuner ledger surface, same class
    as the ssh remote boundary — so they are allowed by design (anything
    touching THIS machine's ledger must still route through pgtuner)."""
    assert _run("docker exec pgbox psql -c 1") == 0
    assert _run("podman exec pgbox psql -c 1") == 0
    assert _run("kubectl exec pgpod -- psql -c 1") == 0
    assert _run("docker exec pgbox bash -c 'psql -c 1'") == 0  # inside the container, same boundary


def test_ssh_local_join_tail_words_denied():
    """C9-F1 (MAJOR, U-batch regression): ssh concatenates the remote
    command argv words with spaces into ONE command string for the
    remote shell — for a local host that is eval-on-this-machine. The
    old scan intercepted a fully-quoted FIRST word, rescanned just it
    and returned, so every tail word was dropped: `ssh localhost 'A=1'
    psql -c 1` passed while the joined `A=1 psql -c 1` really ran psql
    (review-reproduced). The post-host words are now joined and rescanned
    as a whole; option words after the host are pulled out first (glibc
    getopt permutes them — probe-verified with `ssh -G localhost
    -oPort=X`) and -o values keep their payload check."""
    assert _run("ssh localhost 'A=1' psql -c 1") == 2
    assert _run("ssh localhost 'echo hi;' psql -c 1") == 2
    assert _run("ssh user@127.0.0.1 'A=1' psql") == 2
    assert _run("nohup ssh localhost 'A=1' psql -c 1") == 2       # behind a wrapper
    assert _run("ssh -p 2222 localhost 'echo hi;' psql -c 1") == 2  # options before host
    assert _run('ssh localhost "psql"\' -c 1\'') == 2             # spliced-word variant: one argv word `psql -c 1`
    assert _run("ssh localhost -oProxyCommand=psql x") == 2       # post-host glued -o (getopt permutation)
    assert _run("ssh localhost -o ProxyCommand=psql x") == 2      # post-host separate -o


def test_ssh_local_join_clean_tails_allowed():
    """C9-F1 no-false-positive side: a joined clean remote command stays
    allowed (`A=1 true` / `echo hi; true` execute nothing banned —
    bash-exact), the pinned single-word and bare-word forms keep their
    verdicts, and a non-local host keeps the declared remote boundary
    for the SAME shapes that are denied on localhost."""
    assert _run("ssh localhost 'A=1' 'true'") == 0
    assert _run("ssh localhost 'echo hi;' 'true'") == 0
    assert _run("ssh localhost 'psql -c 1'") == 2                 # pinned: quoted remote payload
    assert _run("ssh localhost psql -c 1") == 2                   # pinned: bare words
    assert _run("ssh localhost 'grep psql x'") == 0               # pinned: mention-only payload
    assert _run("ssh localhost grep psql x") == 0                 # pinned
    assert _run("ssh otherhost 'A=1' psql -c 1") == 0             # remote boundary kept for the join shape


def test_eval_joins_all_arguments_denied():
    """C9-F2 (MAJOR): eval joins ALL its arguments into one command
    string and re-parses it — the old approximation rescanned only the
    first payload word and returned, so tail words were dropped while
    bash really executed them (bash-verified probes: `eval 'echo hi;'
    'printf ...'` runs BOTH halves; `eval 'true' '&&' ...` joins the
    operator in). The whole join is now the payload."""
    assert _run("eval 'echo hi;' 'psql -c 1'") == 2
    assert _run("eval 'true' '&&' psql -c 1") == 2
    assert _run("eval 'true' '&&' 'pgbench -T 5'") == 2
    assert _run("eval 'X=2' 'psql --version'") == 2               # assignment word + tail command


def test_eval_join_correct_forms_kept():
    """C9-F2 regression pins: the three known-correct eval shapes fall
    out of join-then-rescan naturally and must not flip — `eval 'A=1'
    psql -c 1` joins to `A=1 psql -c 1` (psql runs → deny), while
    `eval 'A=1' 'true'` joins to `A=1 true` and `eval : psql -c 1`
    joins to `: psql -c 1` (psql a mere argument of `:` — nothing
    executes; bash-verified)."""
    assert _run("eval 'A=1' psql -c 1") == 2                      # pinned deny
    assert _run("eval 'A=1' 'true'") == 0                         # pinned allow
    assert _run("eval : psql -c 1") == 0                          # pinned allow
    assert _run("eval 'A=1 grep psql f'") == 0                    # pinned mention-only payload
    assert _run("eval 'psql -c 1'") == 2                          # single-word payload unchanged


def test_ansi_control_escape_nul_folding_denied():
    """C9-F3 (MAJOR): bash decodes \\cX to chr(ord(X) & 0x1F); X=@ and
    X=` both mask to NUL, and a NUL in a word TRUNCATES it there
    (bash/execve C-string semantics — bash-verified: `$'psql\\c@'`
    really runs psql, `$'p\\0sql'` is the word `p`). The hook's _ansi_
    unescape dropped NULs in place instead, so `$'psql\\c@'` scanned as
    `psql\\c@` and passed while bash ran psql."""
    assert _run("$'psql\\c@' -c 1") == 2                          # \c@ -> NUL -> word truncates to psql
    assert _run("$'psql\\c`' -c 1") == 2                          # \c` (backtick) -> NUL, same folding
    assert _run("bash -c $'psql\\c@'") == 2                       # decoded -c payload
    assert _run("timeout 5 $'psql\\c@'") == 2                     # behind a wrapper
    assert _run("$'pgbench\\c@' -T 5") == 2                       # pgbench variant


def test_ansi_nul_truncation_contrast_allowed():
    """C9-F3 contrast, asserted per REAL bash semantics: a mid-word NUL
    truncates the word at that point, so `$'p\\0sql'` is the command
    `p` — NOT psql (bash-verified: prints `p`) — and is allowed; the
    word after truncation is not a banned one and nothing else runs.
    The old drop-NUL-in-place behavior reassembled `psql` and denied
    (mid-word divergence fixed)."""
    assert _run("$'p\\0sql' -c 1") == 0                           # word truncates to `p`: not banned
    assert _run("$'p\\x00sql' -c 1") == 0                         # hex NUL, same truncation
    assert _run("echo $'line1\\nline2'") == 0                      # pinned: non-NUL ANSI stays data


def test_env_split_string_payload_denied():
    """R9-F5 (MAJOR): GNU env -S/--split-string splits the value on
    whitespace and EXECUTES the first word (probe-verified on this box:
    `env -S 'printf %s x' MARKER`-shaped forms run), so the dequoted
    value is a bash -c-style payload and is rescanned."""
    assert _run("env -S 'psql -c 1'") == 2
    assert _run("env -S 'A=1 psql -c 1'") == 2                    # assignment inside the split string
    assert _run("env --split-string 'psql --version'") == 2       # long option form
    assert _run("env -S'psql -c 1'") == 2                         # glued form


def test_env_split_string_no_false_positive():
    """R9-F5 no-false-positive side: a clean -S payload stays allowed,
    and the pinned env forms (assignment prefix, plain env wrap) keep
    their verdicts."""
    assert _run("env -S 'grep psql f'") == 0                      # mention-only split string
    assert _run("env -S 'echo done'") == 0
    assert _run("env PGPASSWORD=x psql -c 'SELECT 1'") == 2       # pinned assignment-prefix deny
    assert _run("env -u FOO grep psql f") == 0                    # -u value not a payload


def test_ssh_dash_o_ansi_escape_payloads_denied():
    """R9-F6 (MAJOR, conditional on sh→bash): the local sh re-parses
    ssh -o values, and ANSI-C escapes survive the OUTER shell only if
    double quotes keep \\x literal (bash dquote rule, probe-verified).
    Two fixes close it: inside "..." only \\$ `\\" \\\\ drop their
    backslash, and the -o payload rescan ANSI-decodes $' shells
    (fail-closed) before rescanning — `ProxyCommand=$'\\x70sql' -c 1`
    expands to `psql -c 1` under bash-as-sh (verified shape); the
    `$'psql -c 1'` whole-shell form is denied on the same safe side."""
    assert _run('ssh -o "ProxyCommand=$\'\\x70sql\' -c 1" host') == 2
    assert _run("ssh -o 'LocalCommand=$'\\''psql -c 1'\\''' host") == 2


def test_ssh_dash_o_ansi_fix_no_regressions():
    """R9-F6 no-regression side: the pre-existing whole-value / glued /
    quoted-inner -o forms keep their denies, and plain options with a
    remote host keep the declared boundary (jump hosts do not execute)."""
    assert _run("ssh -o ProxyCommand='psql -c 1' host exit") == 2
    assert _run("ssh -oProxyCommand=psql host exit") == 2
    assert _run("ssh -o 'ProxyCommand=psql -c 1' host exit") == 2
    assert _run('ssh -o "LocalCommand=\'psql -c 1\'" host') == 2
    assert _run("ssh -o 'ProxyJump=jumper' host psql -c 1") == 0   # jump hosts don't execute
    assert _run("ssh -o 'ServerAliveInterval=5' host psql -c 1") == 0  # plain option + remote boundary


def test_ssh_join_options_stop_at_command_word():
    """C10-F1 (MAJOR): OpenSSH parses post-destination options only UNTIL
    the first command word — probe-verified on this box: `ssh -G localhost
    env -S` prints its config rc=0 (the trailing -S after the command word
    `env` is remote-command material) while `ssh -G -S` errors rc=255. The
    join loop used to pull `-` words out of the join indiscriminately, so
    in `ssh localhost 'env' '-S' 'psql -c 1'` the `-S` swallowed
    'psql -c 1' as its ControlPath value and the stripped join `env`
    passed while the remote side really executed `env -S psql -c 1`. The
    option zone now ends at the first command word: everything after it —
    `-`-prefixed words included — is joined and rescanned, so the same
    shape denies via the env -S payload path, `ssh localhost 'psql' -v -c
    1` stays denied (`-v -c 1` are psql's arguments, self-consistent) and
    a legal remote env -S grep stays allowed."""
    assert _run("ssh localhost 'env' '-S' 'psql -c 1'") == 2    # join = env -S psql -c 1 -> env -S payload
    assert _run("ssh localhost 'psql' -v -c 1") == 2           # post-command-word options stay in the join
    assert _run("ssh -oPort=2222 localhost 'psql -c 1'") == 2  # pinned: PRE-host glued option still parsed
    assert _run("ssh localhost 'env' '-S' 'grep psql f'") == 0  # legal remote: env -S executes `grep psql f`
    assert _run("ssh localhost 'env' '-S' 'sh' 'psql'") == 2   # operands land in sh's -c payload slot


def test_ansi_control_multibyte_first_byte_denied():
    """C10-F2 (MAJOR): bash decodes \\cX by masking the FIRST BYTE of X in
    the command's byte string, not its code point — bash-verified:
    $'psql\\cࠁ' (U+0801, UTF-8 bytes E0 A0 81) masks E0 & 0x1F = 0 → NUL →
    the word truncates to `psql` and really executes it (printf|od probe:
    70 73 71 6c). The old ord() rule gave 0x801 & 0x1F = 1, kept a \\x01
    tail and allowed the word. ASCII-domain \\c@ / \\c` pins unchanged."""
    assert _run("$'psql\\cࠁ' -c 1") == 2          # E0 & 0x1F = 0 -> NUL -> word truncates to psql
    assert _run("$'p\\cࠁ' -c 1") == 0             # truncates to `p`: not banned (contrast, bash-exact)
    assert _run("bash -c $'psql\\cࠁ'") == 2       # decoded -c payload, same first-byte masking
    assert _run("timeout 5 $'pgbench\\cࠁ'") == 2  # pgbench variant behind a wrapper
    assert _run("$'psql\\c@' -c 1") == 2          # pinned ASCII NUL fold (C9-F3 family)


def test_watch_joined_sh_payload_denied():
    """R10-F2 (MAJOR): procps watch does NOT exec its command arguments —
    it space-joins them and hands the string to /bin/sh -c (probe-verified
    on this box: a `watch 'echo x > MARKER'` marker file landed), so the
    quoted whole payload `watch 'psql -c 1'` really runs psql every
    interval. watch now follows eval's join rule: after the leading options
    (-n/--interval style, values included) the command words are joined and
    the join is rescanned. Five shapes, including the unquoted form that
    was already denied (pinned through the same rescan)."""
    assert _run("watch 'psql -c 1'") == 2               # quoted whole payload -> sh -c 'psql -c 1'
    assert _run("watch -n.5 psql\\ -c\\ 1") == 2        # glued interval + escaped-space single argv word
    assert _run("watch bash -c 'psql -c 1'") == 2       # nested shell inside the join
    assert _run("watch 'bash -c \"psql -c 1\"'") == 2   # nested shell as ONE quoted payload word
    assert _run("watch psql -c 1") == 2                 # pinned: unquoted form stays denied
    assert _run("timeout 5 watch 'psql -c 1'") == 2     # behind a wrapper


def test_watch_clean_payloads_allowed():
    """R10-F2 no-false-positive side: a clean join stays allowed — watch's
    -n/--interval values are skipped (glued and separate spellings), and
    the mention-only payload resolves exactly like the standalone grep
    form (psql stays a mere grep argument inside the joined string)."""
    assert _run("watch 'grep psql f'") == 0             # mention-only join
    assert _run("watch -n1 grep psql f") == 0           # glued interval + bare legal command words
    assert _run("watch -n 1 'grep psql f'") == 0        # separate -n value + quoted payload
    assert _run("watch date") == 0                      # plain clean command


def test_parallel_joined_payload_same_rule():
    """R10-F2 companion: GNU parallel is the same join-to-shell family.
    parallel is NOT installed on this box — the rule is pinned lexically,
    mirroring watch's implementation (see the script docstring)."""
    assert _run("parallel 'psql -c 1'") == 2            # joined payload denied by the same rule
    assert _run("parallel 'grep psql f'") == 0          # clean join allowed by the same rule


def test_env_split_string_operands_joined():
    """C10-F1 companion (R9-F5 refinement): GNU env appends the operands
    AFTER the -S value to the argv of the command the split string
    produced — `env -S grep psql f` executes `grep psql f`, and
    `env -S 'sh -c' 'psql -c 1'` really executes sh -c psql (the operand
    lands in the -c payload slot). The -S payload is therefore the value
    word PLUS every remaining word of the segment, joined and rescanned —
    which is also what makes the flattened ssh-join shape judge exactly
    like the quoted standalone one."""
    assert _run("env -S 'sh -c' 'psql -c 1'") == 2      # operand becomes sh's -c payload
    assert _run("env -S A=1 psql -c 1") == 2            # all-assignment value: first operand is the command
    assert _run("env -S grep psql f") == 0              # operands appended to grep's argv: mention only
    assert _run("env -S 'psql -c 1'") == 2              # pinned R9-F5 single-word form


def test_fold_skips_ansi_c_span_bash_compared():
    """C10-N2: inside $'...' bash ANSI-C quoting KEEPS a \\<newline> pair
    (both characters — bash-verified below via a live od probe), so
    _fold_continuations must skip $'...' spans like single quotes instead
    of folding the pair away (the fold could only ever cause a
    conservative deny, but it was semantically wrong). Outside quotes the
    fold keeps its bash line-continuation meaning (pinned R3-C-F5)."""
    sys.path.insert(0, str(SCRIPT.parent))
    import guard_raw_cli as g
    span = "$'a\\\nb'"                          # $' span containing backslash+newline
    assert g._fold_continuations(span) == span  # inside $'...': kept verbatim, like bash
    assert g._fold_continuations("ps\\\nql --version") == "psql --version"  # outside: folded (pinned)
    # live bash comparison: the real shell keeps both characters in the word
    p = subprocess.run(["bash", "-c", "printf %s $'a\\\nb' | od -An -tx1"],
                       capture_output=True, text=True)
    assert p.stdout.split() == ["61", "5c", "0a", "62"]   # a \ LF b — pair kept, not folded
    assert _run("echo $'a\\\nb'") == 0           # verdict matches bash: echo runs, nothing banned


def test_find_exec_join_tools_denied():
    """R11-C-F1 (MAJOR): find executes the -exec/-execdir clause argv on
    THIS machine — and when the clause's command word is watch/parallel
    (_JOIN_EXEC) or env, that argv is turned into ONE command string
    (watch/parallel space-join into /bin/sh -c; env -S splits+executes
    the value, operands appended). The old exec_cmd branch only
    payload-tracked _C_PAYLOAD shells, so all these passed while psql
    really ran (review-reproduced). The command word plus every clause
    word up to the \\;/+ terminator is joined and rescanned."""
    assert _run("find . -exec watch 'psql -c 1' \\;") == 2
    assert _run("find . -exec parallel 'psql -c 1' \\;") == 2
    assert _run('find /tmp -name x -execdir watch "psql -c 1" \\;') == 2  # -execdir variant
    assert _run("find . -execdir parallel 'pgbench -T 5' \\;") == 2
    assert _run("find . -exec env -S 'psql -c 1' \\;") == 2
    assert _run("find . -execdir env -S 'psql --version' \\;") == 2
    assert _run("find . -exec env -S 'sh -c' 'psql -c 1' \\;") == 2   # operands join into the payload
    assert _run("find . -exec env PGPASSWORD=x psql -c 1 \\;") == 2   # env assignment prefix in the clause
    assert _run("find . -exec watch -n 1 psql -c 1 \\;") == 2         # watch option inside the clause
    assert _run("find . -exec watch psql -c 1 \\;") == 2              # unquoted clause words


def test_find_exec_join_tools_clean_allowed():
    """R11-C-F1 no-false-positive side: clean join clauses stay allowed —
    the rescan sees psql as a mere argument inside the joined string, the
    clean -S split string resolves like the standalone env -S form, and
    the pinned first-word-only and grep forms keep their verdicts."""
    assert _run("find . -exec watch 'grep psql f' \\;") == 0
    assert _run("find . -exec env -S 'grep psql f' \\;") == 0
    assert _run("find . -execdir watch -n1 grep psql {} \\;") == 0
    assert _run("find . -exec grep psql {} \\;") == 0                 # pinned red line
    assert _run("find . -name x -exec grep psql {} \\;") == 0         # pinned


def test_ssh_sudo_value_option_zones_denied():
    """R11-C-F2 (MAJOR): ssh's -c/-m and sudo's -g/-p consume a VALUE
    argument — in both ssh zones the value word used to escape the check
    (post-host join zone: phantom first remote command word masking
    psql; host zone: value mistaken for the host word, misjudged remote)
    and sudo's value word stopped the scan as an "ordinary command",
    while psql really executed on this machine (review-reproduced)."""
    assert _run("ssh localhost -c aes128-ctr psql -c 1") == 2   # join zone
    assert _run("ssh -c aes128-ctr localhost psql -c 1") == 2   # host zone
    assert _run("ssh localhost -m hmac-sha2-256 psql -c 1") == 2
    assert _run("ssh -m hmac-sha2-256 localhost psql -c 1") == 2
    assert _run("sudo -g postgres psql -c 1") == 2
    assert _run("sudo -p '[x]' psql -c 1") == 2
    assert _run("sudo -C 3 psql -c 1") == 2


def test_ssh_sudo_value_option_clean_allowed():
    """R11-C-F2 no-false-positive side: a consumed value option must not
    disturb the legal shapes — the same cipher/group spellings with a
    clean remote/sudo command stay allowed, and a non-local host keeps
    the declared remote boundary for the SAME shape that denies on
    localhost."""
    assert _run("ssh localhost -c aes128-ctr grep psql f") == 0
    assert _run("ssh -c aes128-ctr localhost grep psql f") == 0
    assert _run("ssh otherhost -c aes128-ctr psql -c 1") == 0    # remote boundary kept
    assert _run("sudo -g postgres grep psql f") == 0
    assert _run("sudo -u postgres pgtuner sql 'select 1'") == 0  # pinned -u path unchanged


def test_herestring_fed_shells_denied():
    """R11-D-F1① (MAJOR): a herestring feeds the command's stdin — for a
    shell-family command word that string is a SCRIPT that really
    executes (`bash <<< 'psql -c 1'`, `bash -s <<< …`; the heredoc sibling
    `bash <<EOF` was already covered by the body line-scan, so this was
    an implementation gap, not a declared boundary). Glued operator,
    unquoted payload, wrapper chains and the redirect-first form
    included; the payload is dequoted and rescanned."""
    assert _run("bash <<< 'psql -c 1'") == 2
    assert _run("bash -s <<< 'psql -c 1'") == 2
    assert _run("bash <<<'psql -c 1'") == 2                 # glued operator+payload word
    assert _run("bash <<< psql") == 2                       # unquoted payload word
    assert _run("sh <<< 'pgbench -T 5'") == 2
    assert _run("timeout 5 bash <<< 'psql -c 1'") == 2      # behind a wrapper
    assert _run("env bash <<< 'psql -c 1'") == 2
    assert _run("nohup bash <<< 'psql -c 1'") == 2
    assert _run("<<< 'psql -c 1' bash") == 2                # redirect-first: command word after
    assert _run("bash <<< 'psql -c 1' extra") == 2          # trailing $0 word ends the scan early
    assert _run("echo x | bash <<< 'psql -c 1'") == 2       # herestring beats the pipe


def test_herestring_clean_controls_allowed():
    """R11-D-F1① no-false-positive side: a herestring on a NON-shell
    command is input data (grep pattern source, cat copy), a clean stdin
    script resolves like the standalone form, and `bash -c PAYLOAD`
    keeps its own authoritative rescan (the -c payload branch returns
    before stdin is ever considered — bash ignores stdin under -c)."""
    assert _run("grep psql f <<< x") == 0
    assert _run("cat <<< 'grep psql f'") == 0
    assert _run("bash run.sh <<< x") == 0                   # script file: declared unknowable
    assert _run("bash <<< 'grep psql f'") == 0              # clean stdin script
    assert _run("bash -c 'echo hi' <<< 'psql -c 1'") == 0   # -c payload authoritative: stdin ignored


def test_pipe_fed_bare_shells_denied():
    """R11-D-F1② (MAJOR): a bare shell as the right side of a pipe reads
    the producer's OUTPUT as a script and really executes it —
    `echo 'psql -c 1' | bash`, `echo psql | bash -s`, `printf … | bash`
    and the wrapper chains all passed before (review-reproduced). The
    adjacent-left segment's argument tail / quoted words are rescanned;
    `… | xargs bash -c` denied too (xargs appends the input line into the
    -c slot — trailing armed -c with no payload word)."""
    assert _run("echo 'psql -c 1' | bash") == 2
    assert _run("echo 'psql -c 1' | sh") == 2
    assert _run("echo psql | bash -s") == 2
    assert _run("printf 'psql -c 1\\n' | bash") == 2
    assert _run("printf '%s\\n' 'psql -c 1' | bash") == 2
    assert _run("echo 'psql -c 1' | timeout 5 bash") == 2    # behind wrappers
    assert _run("echo 'psql -c 1' | env bash") == 2
    assert _run("echo 'psql -c 1' | nohup bash") == 2
    assert _run("echo 'psql -c 1' | xargs bash -c") == 2     # xargs appends into the -c payload slot


def test_pipe_fed_bare_shells_clean_allowed():
    """R11-D-F1② no-false-positive side: only banned-execution hits deny
    — `echo 'grep psql f' | bash` stays allowed (the rescan sees psql as
    a mere grep argument), a bare single-word producer has no candidates,
    and file-content producers keep the `bash run.sh` honesty class
    (declared unknowable)."""
    assert _run("echo 'grep psql f' | bash") == 0
    assert _run("echo 'grep psql f' | bash -s") == 0
    assert _run("echo hi | bash") == 0
    assert _run("date | bash") == 0                          # single-word producer: no candidates
    assert _run("cat q.sql | bash") == 0                     # file contents: declared boundary
    assert _run("grep -q psql f | bash") == 0                # quiet grep prints nothing


def test_systemd_run_gdb_busybox_ash_denied():
    """R11-D-F2/F3/F4: systemd-run (transient unit on THIS machine) and
    gdb (--args debugs the program locally) are strace-class wrappers;
    ash joins the shell family and busybox dispatches its first word to
    the applet — `busybox sh -c 'psql'` reaches the shell branch. All
    four shapes passed before (review-reproduced); pinned lexically."""
    assert _run("systemd-run psql -c 1") == 2
    assert _run("systemd-run --wait psql -c 1") == 2
    assert _run("gdb --args psql -c 1") == 2
    assert _run("gdb --args pgbench -T 5") == 2
    assert _run("ash -c 'psql'") == 2
    assert _run("busybox sh -c 'psql'") == 2
    assert _run("busybox ash -c 'pgbench -T 5'") == 2


def test_systemd_run_gdb_busybox_ash_clean_allowed():
    """R11-D-F2/F3/F4 no-false-positive side: the new wrappers/shells
    with clean payloads keep psql a mere argument."""
    assert _run("systemd-run grep psql f") == 0
    assert _run("gdb --args grep psql f") == 0
    assert _run("ash -c 'grep psql f'") == 0
    assert _run("busybox sh -c 'grep psql f'") == 0


def test_ansi_backslash_newline_pair_kept():
    """R11-C-F3 (NIT): inside $'...' bash KEEPS a backslash-newline pair
    (the C10-N2 od probe: 61 5c 0a 62) — _ANSI_SIMPLE decoded \\<LF> to
    "", which reassembled `$'ps\\<LF>ql'` into the command psql and
    over-denied. The pair now survives decoding: the word is NOT psql
    (bash: command not found) and the command is allowed. Inside a -c
    payload the pair reaches the INNER shell as raw text, which folds it
    as a line continuation (bash-exact) — denied there."""
    assert _run("$'ps\\\nql' -c 1") == 0                # pair kept: word != psql, nothing runs
    assert _run("$'psql' -c 1") == 2                    # plain $' decoding unchanged (pinned)
    assert _run("bash -c $'ps\\\nql'") == 2              # inner shell folds the pair -> psql (bash-exact)
