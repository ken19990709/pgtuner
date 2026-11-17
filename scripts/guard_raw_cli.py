#!/usr/bin/env python3
r"""PreToolUse gatekeeper: blocks raw psql/pgbench in Bash tool invocations
(mechanical enforcement of AGENTS.md iron rule 1).

Entry contract (ZCode/Claude Code hooks): event JSON arrives on stdin, read
tool_input.command; exit 0 = allow, exit 2 = block (the stderr reason goes back
to the model). Any parsing exception always allows (fail-open — the gatekeeper
script itself must never break the session). Escape hatch: a trailing
`# allow-raw-psql` comment at the very end of the command line allows it (for
psql-variant benchmarks / special operations; the benchmark task brief already
informs the agent under test). An occurrence anywhere else in the command is
plain string data (`echo "# allow-raw-psql" > f && psql ...`) and does NOT
bypass the gate.

Detection criterion: only a token in an "executable position" counts
(psql/pgbench or their full paths); lexical mentions don't —
`grep psql README.md` and `echo psql` are allowed. Executable position means:
the first real token of a compound-command segment, where shell
compound-command keywords (and case-clause pattern words) ahead of it are
transparent — see the R4 paragraph below. Segmentation is
quote-aware: `;`, `|`, `&`, newline, `(`, `{`, `}`, `$` and backtick
separate segments ONLY outside quoting contexts ('...' fully literal;
"..." literal except backslash escapes) — a `;` or `psql` inside quotes is
plain data, never a segment boundary. After segmentation, leading
VAR=value assignments are skipped, then wrapper commands (time/sudo/nice/
nohup/env/command/exec/timeout/xargs/stdbuf/setsid/watch/ssh/strace/
ltrace/find/systemd-run/gdb/busybox) together with their leading options,
numeric and value-option
arguments (`timeout 30 psql`, `stdbuf -oL psql`, `exec -a x psql`,
`strace -o t.txt psql`, `find . | xargs psql`). find (R5-D5-F6) gets a
special case: the first word after an `-exec`/`-execdir` predicate is a
command find executes ON THIS MACHINE once per match — it is
banned-checked (`find . -exec psql \;` denied, quote-spliced `'-exec'`
too), while `-exec grep psql {} \;` stays allowed (grep is the command,
psql its argument; only the first word is judged — declared conservative
scope); when that first word is a bash/sh-family shell, eval, script or
su, its -c-style payload is rescanned exactly like the wrapper path
(C6-F4, see the C6 paragraph). `bash -c "..."` / `sh -c '...'` payloads,
`eval "..."` (its first argument IS a command string) and `script -c CMD`
(value of its -c / -qec option) execute locally and get rescanned with
these same rules, so `bash -c "psql -c 'SELECT 1'"`, `eval "psql ..."` and
`script -qec "psql ..."` are blocked, and `su USER -c PAYLOAD` /
`su -c PAYLOAD USER` joins that family (R3-C-F6). `{ psql; }` brace
groups split into psql-leading segments; a trailing `)` is stripped from
the leading token, so `(psql)` and `$(psql ...)` / `` `psql ...` ``
substitutions are caught too. ssh is covered for local wrapper chains
only (e.g. `nohup ssh ...`): its remote command never executes on this
machine, so it is not payload-rescanned.

Hardening (adversarial review R3): redirection tokens positioned before
the command (`2>&1 psql ...`, `< /dev/null psql ...`, `> out.txt psql
...`, glued forms like `2>err.txt`, `>>log`, `2<>fifo`) are stripped
before segmentation — a leading redirection must not hide the command
word (process substitutions `<(cmd)` / `>(cmd)` are left for the `(`
segmentation, which already rescans their payload). Backslash-newline
continuations are folded away first, so `ps\<LF>ql --version` scans as
`psql --version` (R3-C-F5). The command word is normalized the way an
unquoted bash word resolves: adjacent quotes are spliced and `\<char>`
unescaped to a fixed point, so 'p'sql / p""sql / ps\ql / \psql — and
re-escaped words from nested `bash -c "bash -c \"...\""` chains — all
resolve to their real command name (R3-D3.2). Nested -c/eval payload
rescanning is capped at _MAX_DEPTH (20) levels; anything deeper is
DENIED fail-closed with a dedicated message (R3-D3.3) — the hook must
never time out into a fail-open.

Compound-keyword command positions (adversarial review R4-C-F2/D-F4): the
shell reserved words if/then/elif/else/while/until/for/do/case/coproc/select,
the pure lexical words in/esac/fi/done and a bare `!` negation word sit in a
command position WITHOUT being commands — the scan skips them and keeps
examining the next command-position word instead of stopping, so
`if psql -c 1; then echo; fi`, `if true; then psql -c 1; fi`,
`while :; do psql -c 1; done`, `for f in *.sql; do psql -f $f; done`,
`elif psql ...`, `! psql -c 'SELECT 1'`, `coproc psql -c 1` and
`time ! psql -c 1` are all denied. `case` additionally opens a pattern
list: every word up to and including the `)`-suffixed pattern word is
skipped without banned-checking (an unterminated list carries across
`|`/`$`-split segments), and a command-position `)`-suffixed word in a
segment that did not start inside a $()-substitution is likewise treated
as a clause opener — so `case x in x) psql -c 1;; esac` and later clauses
(`a) :;; b) psql ...`) are denied, while `case $c in psql) echo talk;; esac`
(the banned word is the PATTERN) and `echo $(date) psql` (substitution
glue, psql is echo's argument) stay allowed. Skip-set keywords consume no
value arguments: a bare psql/pgbench word inside a `for x in ...` word
list is (over-)denied — safe direction, the `# allow-raw-psql` hatch still
applies. ssh (R4-D-F5): when ssh's first host word is
localhost/127.0.0.1/::1 the "remote" command runs on THIS machine, so the
scan continues past the host word; any other host keeps the declared
remote boundary.

R5 refinements: reserved words are recognized as BARE unquoted words only
(C5-F1) — bash never treats `./case`, `'case'`, `"ca"se`, `\case`,
`~/case` or `/usr/bin/case` as the `case` keyword: they are ordinary
command words, the scan stops there and the next `;`/`|`-split segment is
scanned fresh, so `./case; psql ...` is denied (the R4 code matched
_RESERVED against the normalized word and let pattern mode swallow the
psql segment), while `"then" psql -c 1` stays allowed — verified bash
semantics: the quoted word is the COMMAND NAME (`then: command not found`,
verified), psql is a mere argument and nothing executes; `"then"; psql
...` does execute psql and is denied. coproc (D5-F7): the word after it,
when not a bare reserved word, is skipped as the coprocess NAME
(`coproc XX psql -c 1` denied; bash 5.2 only takes a NAME before compound
commands, so this is a safe-direction over-deny like the for-list one —
`coproc XX { psql -c 1; }` is exact). ssh host equivalence (D5-F5): the
host word is normalized (lowercased, trailing dot stripped, IPv6 brackets
removed) before the local test and any `127.*` IPv4 form counts as
loopback (127.1 == 127.0.0.1 in inet_aton shorthand) — LOCALHOST /
localhost. / 127.0.0.1. / 127.1 / [::1] / 0.0.0.0 all continue the local
scan; every other host keeps the declared remote boundary.

C6 refinements: ssh host words (C6-F1/D6-F3) additionally go through the
same numeric parsers OpenSSH uses — every form below was verified against
getaddrinfo and (where ambiguous) a real connect probe, not assumed. Any
inet_aton(3)-parseable IPv4 whose first octet is 127 keeps the local
scan: single-u32 decimal (`2130706433`), hex (`0x7f000001`, either case),
octal (`0177.0.0.1`) and part-count shorthands (`0x7f.1`, `0x7f.0.1`,
`0177.1`) all resolve to 127.0.0.1. ipaddress()-parseable loopbacks do
too: full-form IPv6 `0:0:0:0:0:0:0:1` and embedded-IPv4 `::0.0.0.1` — hygiene:allow
(ssh host-word parser fixtures; both == ::1) — and the
IPv4-mapped `::ffff:127.0.0.1` (not .is_loopback
itself, but its ipv4_mapped side is 127.0.0.1 and a connect there
reaches this machine). Measured NON-local and kept remote: `0x7f`
(→ 0.0.0.127, connect times out — first octet 0; hygiene:allow —
fixture), `0x08080808` (→ 8.8.8.8, hygiene:allow — fixture) and the
deprecated IPv4-COMPATIBLE `0:0:0:0:0:0:127.0.0.1` (::7f00:1,
network-unreachable).
find (C6-F4): the first word after -exec/-execdir, when it is a
bash/sh-family shell, eval, script or su (the _C_PAYLOAD family), gets
the SAME -c option tracking and payload rescan as the wrapper path —
`find . -exec sh -c 'psql -c 1' \;` executes psql locally and is denied
(a clean payload does not end the segment scan: later `-exec psql \;`
clauses stay checked) — while `-exec python3 -c ...` keeps the
generic-interpreter declared boundary.

R7 refinements: the words of a segment are tokenized with the
quote-aware `_scan_words` walk (D7-F6) — the old space-split regex
treated `FOO='1 2' psql -c 1` as the fragments `FOO='1` + `2'` and the
scan stopped on `2'` as an ordinary command, so assignment prefixes
with quoted spaces (`env VAR='a b' psql`, `BAR="x y" pgbench`) let the
wrapped banned binary execute for real; each quoted word now stays
whole and the FOO=... prefix skip keeps its meaning on the whole word
(`FOO='a b' grep psql f` stays allowed). ssh `-o` options whose value
the LOCAL side executes (D7-F5) — ProxyCommand= and LocalCommand= run
via this machine's /bin/sh -c (review-verified: a marker file landed),
ProxyJump= only names jump hosts but rescanning its value is harmless —
get the post-= payload rescanned one level deeper, in the separate form
(`ssh -o ProxyCommand='psql -c 1' host exit`) and the glued form
(`-oProxyCommand=psql`); a non-local host keeps the declared remote
boundary, so plain options + remote command (`ssh -o
ServerAliveInterval=5 host psql -c 1`) stay allowed. A bare `{}` (C7-F1)
is find's placeholder — its most common spelling, and without a comma
or range never a brace expansion — so it is no longer a segment
separator (`find . -exec grep x {} \; -exec psql \;` used to lose the
segment-local -exec state at the `{}` cut and the second clause escaped
the check); the grouping syntax `{ cmd; }` still splits and `{ psql; }`
stays denied. ssh host words with a user@ prefix (C7-F3) have the
prefix stripped before the locality test (`ssh user@127.0.0.1 psql` denied,
`ssh root@localhost psql` denied, and `ssh user@192.168.1.5 psql` — hygiene:allow
(parser fixture) — allowed).

R8/D8 refinements: a -c-style payload word that itself contains `=`
(C8-F1) is no longer swallowed by the assignment-prefix check — the
payload word falls through to the rescan, and the RECURSION's own
assignment skip handles prefixes inside the payload (there `X=1 psql`
puts psql in command position and it is checked), so `bash -c
"PGPASSWORD=x psql -c 1"` and its eval/su/script/ssh-localhost/
find -exec/xargs -I{}/nested siblings are denied while `bash -c "A=1
grep psql f"` stays allowed; eval joins ALL its arguments into one
command string (C9-F2: the JOIN itself is now the payload — see the R9
paragraph), so `eval 'A=1' psql -c 1` joins to `A=1 psql -c 1` and is
denied (bash-verified). ssh -o values are key-matched on the DEQUOTED
word (C8-F2), so the whole-value quoted spellings `ssh -o
'ProxyCommand=psql -c 1' host` / `ssh -o "LocalCommand=..."` are denied
like the glued form (OpenSSH executes the quoted value locally —
review-verified). $'...' ANSI-C and $"..." locale quoting (D8-F2) is
expanded statically: \xXX, \ooo, \uXXXX, \UXXXXXXXX and the usual
simple escapes decode INSIDE the word (the $ and its quote are word
material, never separators), and the decoded word joins the
command-position/basename judgement — `$'\x70sql' -c 1`, `ps$'q'l -c
1`, `bash -c $'psql -c 1'` are denied (bash really runs the expanded
word — verified) while `echo $'line1\nline2'` and `awk $'...'` payloads
stay allowed; a $' word that fails to decode is DENIED fail-closed.

R9/C9 refinements: a local-host ssh's remote command (C9-F1) is
JOIN-then-rescanned — ssh concatenates the post-host argv words with
spaces into ONE command string for the remote shell, so
`ssh localhost 'A=1' psql -c 1`, `ssh localhost 'echo hi;' psql -c 1`
and `ssh user@127.0.0.1 'A=1' psql` are denied (the old scan stopped
after a fully-quoted FIRST word and dropped every tail word); option
words after the host are pulled out of the join first, because glibc
getopt PERMUTES them (probe-verified with `ssh -G localhost -oPort=X`)
and -o values keep their payload check. eval (C9-F2) joins ALL its
arguments into one command string that is rescanned as a whole, so
`eval 'echo hi;' 'psql -c 1'` and `eval 'true' '&&' psql -c 1` are
denied while `eval 'A=1' 'true'` and `eval : psql -c 1` stay allowed
(join + recursion = bash-exact, all four bash-verified). ANSI-C \cX
(C9-F3) decodes to chr(ord(X) & 0x1F), and any decoded NUL (\c@, \c`,
\x00, \000, \u0000) TRUNCATES the word at that point — bash/execve
C-string semantics, probe-verified: `$'psql\c@'` runs psql (denied),
`$'p\0sql'` is the word `p` (allowed — not banned, nothing else runs).
env -S/--split-string (R9-F5) values are split and executed by env
itself (probe-verified), so the dequoted value is payload-rescanned —
`env -S 'psql -c 1'` and `env -S 'A=1 psql -c 1'` denied, `env -S
'grep psql f'` allowed. Inside double quotes (R9-F6) only \$ \` \" \\
drop their backslash (bash rule, probe-verified), so `"ProxyCommand=
$'\x70sql' -c 1"` keeps \x70sql alive for the -o rescan, which now also
ANSI-decodes $' shells in the value (fail-closed) before rescanning
— `ssh -o "ProxyCommand=$'\x70sql' -c 1" host` denied; on sh→bash
systems the local shell really expands that payload (conditional MAJOR
— denied regardless, safe side).

R10/C10 refinements: the local-host ssh join (C9-F1) pulls post-host
option words out of the join only UNTIL the first command word (C10-F1)
— OpenSSH stops option parsing there (probe-verified: `ssh -G localhost
env -S` prints its config rc=0 while `ssh -G -S` errors rc=255; every
word after the first command word, `-`-prefixed included, is remote-
command material), so `ssh localhost 'env' '-S' 'psql -c 1'` joins to
`env -S psql -c 1` and is denied (the old loop let `-S` swallow
'psql -c 1' as its ControlPath value and the stripped join `env` passed
while the remote side ran psql via env -S), while the legal `ssh
localhost 'env' '-S' 'grep psql f'` stays allowed — env -S (R9-F5)
additionally JOINs the post-value operands into its payload (GNU env
appends them to the executed argv: `env -S grep psql f` runs `grep psql
f`, and `env -S 'sh -c' 'psql -c 1'` really runs sh -c psql), so the
flattened join shape is judged exactly like the quoted standalone one.
ANSI-C \cX masks the
FIRST BYTE of X in the command's UTF-8 encoding, not its code point
(C10-F2) — bash-verified: `$'psql\cࠁ'` (U+0801, bytes E0 A0 81) masks
E0 & 0x1F = 0 → NUL → truncates to `psql` and really runs it; the old
ord() rule kept a \x01 tail and allowed the word. watch/parallel
(R10-F2) join their command arguments with spaces and hand the string
to /bin/sh -c — watch probe-verified on this box (marker file landed),
parallel the same documented family but NOT installed here (lexical
rule pinned by tests) — so after their leading options the command
words get eval's join-then-rescan: `watch 'psql -c 1'`, `watch -n.5
psql\ -c\ 1`, nested bash -c shapes and `parallel 'psql -c 1'` are
denied; the unquoted `watch psql -c 1` keeps its deny through the same
rescan, and `watch -n1 grep psql f` stays allowed. _fold_continuations
skips $'...' spans (C10-N2): inside ANSI-C quoting a \<LF> pair keeps
    BOTH characters (bash-verified, see the function) — folding it was only
    ever a conservative deny, but semantically wrong.

R11 refinements: the join/exec semantics of R10-F2 and R9-F5 propagate
into find -exec clauses (R11-C-F1): when the first word after
-exec/-execdir is watch/parallel (_JOIN_EXEC) or env, find executes the
clause argv ON THIS MACHINE and those tools turn it into ONE command
string — the command word plus every clause word up to the \;/+ terminator
is space-joined and rescanned, so `find . -exec watch 'psql -c 1' \;`,
`-exec parallel 'psql -c 1' \;`, `-exec env -S 'psql -c 1' \;` and the
-execdir variants are denied (review-reproduced: the exec_cmd branch only
payload-tracked _C_PAYLOAD shells, so these passed while watch's /bin/sh
-c really ran psql), while clean clauses (`-exec watch 'grep psql f' \;`)
stay allowed. ssh/sudo value-option tables (R11-C-F2): ssh's
-c/-m/-Q/-O/-w and sudo's -g/-p/-C/-D/-r/-t consume a value argument in
BOTH ssh zones — `ssh localhost -c aes128-ctr psql -c 1` (post-host join
zone: the cipher value was taken for a phantom first remote command word)
and `ssh -c aes128-ctr localhost psql -c 1` (host zone: the value was
mistaken for the host word and misjudged remote), likewise `sudo -g
postgres psql -c 1` / `sudo -p '[x]' psql -c 1`, really execute on this
machine and are denied; the legal remote shapes (`ssh localhost -c
aes128-ctr grep psql f`) stay allowed. stdin-fed bare shells (R11-D-F1):
a herestring (`bash <<< 'psql -c 1'`, `bash -s <<< …`, glued `<<<'…'`,
behind timeout/env/nohup, redirect-first `<<< payload bash` — the
heredoc sibling `bash <<EOF` was already covered by the body line-scan,
so this was an implementation gap, not a declared boundary) and a pipe
into a bare shell (`echo 'psql -c 1' | bash`, `echo psql | bash -s`,
`printf … | bash`, behind wrappers, `… | xargs bash -c` where xargs
appends the input line into the -c slot) really execute their payload on
this machine: the dequoted herestring payload and the pipe-left
producer's argument tail / quoted words are rescanned (stdin-fed bare
shells rescanned), while clean controls (`echo 'grep psql f' | bash`,
`grep psql f <<< x`, `cat <<< 'grep psql f'`, `bash -c PAYLOAD …` with
its own authoritative rescan) stay allowed. systemd-run and gdb join the
wrapper set and ash/busybox the shell reach (R11-D-F2/F3/F4:
`systemd-run psql -c 1`, `gdb --args psql -c 1`, `ash -c 'psql'`,
`busybox sh -c 'psql'` — same family as strace/bash). Inside $'...' a
backslash-newline pair keeps BOTH characters (R11-C-F3, the C10-N2 probe
applied to decoding: `printf %s $'a\<LF>b'` emits a \ LF b) — the pair is
no longer decoded away, so `$'ps\<LF>ql'` is NOT the command psql (bash:
command not found) and is allowed instead of over-denied.

Declared boundaries (known, honest out-of-scope — allowed by design):
variable indirection (`P=psql; $P -c 1`) is statically unresolvable;
remote segments (`| ssh host psql ...` for non-local hosts) execute on
another machine; container executors (`docker exec c psql ...`,
`podman exec`, `kubectl exec pod -- psql ...`) run their payload in a
container namespace — off this machine's pgtuner ledger surface, same
class as the ssh remote boundary (C8-F3); $-expansion inside double
quotes (`echo "$(psql ...)"`)
is not parsed; the hook never resolves host NAMES, so machine aliases
that /etc/hosts maps to loopback (`ip6-localhost`, `ip6-loopback`) keep
the declared remote boundary; generic interpreter payloads
(`python3 -c "..."`, `perl -e '...'`, awk programs) are not rescanned —
only bash/sh-family -c, eval, script and su payloads carry a shell
command string that runs locally (R5-D5-F8; the escape hatch and these
boundary declarations are
part of the contract); a heredoc body is scanned line-by-line like
ordinary segments, so a line-leading psql inside `cat > run.sh <<'EOF'`
is DENIED on purpose (R3-C-F8) — `bash run.sh` would not be rescanned,
so blocking is the only safe side of that edge. Known over-rejections
(safe side): banned words in a `for x in ...` word list; the word after
`coproc` (taken as the coprocess NAME); a `-exec` first word that bash
actually parses as an earlier predicate's value (`find . -name -exec
psql` has no -exec clause at all). For a local-host ssh the remote
command is the space-JOIN of the post-host argv words, rescanned as one
command string (C9-F1); deeper quoting inside it follows the same rules
recursively. A pipe into a bare shell is judged from the ADJACENT left
segment only (R11-D-F1②): `printf … | cat | bash` looks one hop left,
sees a bare cat with no rescanable words and keeps the declared boundary
(producer output beyond echo/printf argv shape is statically unknowable,
same honesty class as `bash run.sh`).
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import struct
import sys

ESCAPE = "# allow-raw-psql"
# Escape hatch, but only in the one place bash would parse it as a comment:
# the very end of the command line (whitespace tolerated before the tag and
# at line end). Mid-command occurrences are data, not comments.
_ESCAPE_TAIL = re.compile(r"#\s*allow-raw-psql\s*$")

_BANNED = {"psql", "pgbench"}
# Wrapper words that may precede the real command; their leading options,
# numeric arguments and value-option arguments are skipped before the first
# real token is examined. find (R5-D5-F6) additionally gets its -exec/
# -execdir first-word command check — see the find branch in
# _segment_violates.
_WRAPPERS = {"time", "sudo", "nice", "nohup", "env", "command", "exec",
             "timeout", "xargs", "stdbuf", "setsid", "watch", "ssh",
             "strace", "ltrace", "script", "eval", "su", "find",
             "parallel",
             # R11-D-F2/F3: systemd-run (transient unit runs the argv on
             # THIS machine) and gdb (--args debugs the program locally) —
             # same wrapper family as strace; review-reproduced that
             # `systemd-run psql -c 1` / `gdb --args psql -c 1` passed.
             "systemd-run", "gdb",
             # R11-D-F4 companion: busybox dispatches its first word as the
             # applet — `busybox sh -c 'psql'` reaches the shell branch.
             "busybox"}
# Shells whose -c payload executes locally and therefore gets rescanned.
# R11-D-F4: ash joins the family (`ash -c 'psql'` runs psql — lexical
# pin, same treatment as dash).
_SHELLS = {"bash", "sh", "dash", "zsh", "ksh", "ash"}
# eval behaves like an implicit `shell -c`: its first argument IS the
# command string, so the payload flag starts armed. script joins the
# shells in "an option containing c (-c/-qec/...) carries a command";
# su does too (`su postgres -c 'psql ...'` runs the payload locally).
_EVAL = {"eval"}
_C_PAYLOAD = _SHELLS | _EVAL | {"script", "su"}
# Rescan recursion cap for nested -c/eval payloads: deeper nesting is
# denied fail-closed (a hook timeout would be fail-open — banned).
_MAX_DEPTH = 20
# Shell compound-command keywords (R4-C-F2/D-F4): they occupy a command
# position WITHOUT being a command — skip the word and keep scanning the
# next command-position word (previously the first token being a keyword
# made the scan bail out with "ordinary command", blinding the hook to
# if/then/while/until/for/case/elif/!/coproc positions). The pure lexical
# words in/esac/fi/done never execute either and join the set. These words
# consume no value arguments: after for/select the whole `NAME in WORD...`
# header is scanned in word-list mode (a bare psql/pgbench word inside the
# list is over-denied — safe direction, escape hatch still applies, see the
# module docstring); `case` opens the pattern-skip mode described there.
# R5-C5-F1: matched as the BARE word only (tok == base) — bash never treats
# `./case`, `'case'`, `"ca"se` or `\case` as a keyword, so those fall to the
# ordinary-command branch and the scan stops there; `coproc` (R5-D5-F7)
# additionally skips one non-reserved NAME word ahead.
_RESERVED = {"if", "then", "elif", "else", "while", "until", "for", "do",
             "case", "coproc", "select", "in", "esac", "fi", "done"}
# ssh host words that mean "the remote command runs on THIS machine"
# (R4-D-F5): the scan does NOT stop at the host word for these; any other
# host keeps the declared boundary (remote side never parses locally).
# R5-D5-F5: compared AFTER equivalence normalization (case, trailing dot,
# IPv6 brackets) plus any 127.* IPv4 form — see _ssh_is_local.
_SSH_LOCAL = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
# R10-F2: tools that do NOT exec their command arguments directly — they
# space-JOIN them and hand the resulting string to /bin/sh -c, so the whole
# command tail is ONE command string (eval's join semantics). procps watch:
# probe-verified on this box (a `watch 'echo x > MARKER'` marker landed);
# GNU parallel: same documented family, NOT installed on this box — the
# lexical rule is pinned by tests instead of a live probe (see docstring).
# Like eval they fire on the first NON-option command word: leading options
# are consumed by the option branch (value options via skip_value), and —
# same reasoning as ssh C10-F1 — every word after the first command word is
# command material, `-`-prefixed ones included.
_JOIN_EXEC = {"watch", "parallel"}
# Best-effort: options of the pending wrapper/shell that consume the NEXT
# token as their value, so that token must not be mistaken for the command
# (sudo -u postgres psql, timeout --signal KILL 30 psql, bash -o pipefail -c,
# exec -a x psql, strace -o t.txt psql).
_VALUE_OPTS = {
    "sudo": {"-u", "--user",
             # R11-C-F2: -g/-p/-C/-D/-r/-t all take a VALUE argument —
             # without the entry the value word was mistaken for the
             # command (`sudo -g postgres psql` stopped the scan at
             # `postgres`; `sudo -p '[x]' psql` at `[x]`) while sudo
             # really ran psql (review-reproduced).
             "-g", "-p", "-C", "-D", "-r", "-t"},
    # R9-F5: -S/--split-string carry a command string env itself splits
    # and executes (GNU env -S 'psql -c 1' really runs psql — verified),
    # so besides consuming the next word their value is payload-rescanned
    # (env_s_value flag in _segment_violates).
    "env": {"-u", "--unset", "-S", "--split-string"},
    "timeout": {"-k", "--kill-after", "-s", "--signal"},
    "nice": {"-n", "--adjustment"},
    "xargs": {"-d", "-D", "-E", "-e", "-I", "-n", "-L", "-l", "-P", "-s",
              "--arg-file", "--delimiter", "--max-args", "--max-chars",
              "--max-lines", "--max-procs"},
    "watch": {"-n", "--interval"},
    # R10-F2 companion: GNU parallel's value options (best-effort, same
    # honesty as the strace set above) — a value word misread as the first
    # command word would fire the join one word early and the rescan could
    # under-deny (`parallel -j 2 psql -c 1`), so the common value-taking
    # families are listed; glued forms (-j2, --jobs=2) need no entry.
    "parallel": {"-j", "--jobs", "-n", "--max-args", "-N", "-L", "--max-lines",
                 "-I", "--replace", "-d", "--delimiter", "-P", "--max-procs",
                 "-s", "--max-chars", "--blocksize", "--timeout", "--retries",
                 "--delay", "-S", "--sshlogin", "--workdir", "--tmpdir",
                 "--results", "--files", "--header", "--tagstring", "--nice"},
    "ssh": {"-b", "-D", "-e", "-F", "-I", "-i", "-J", "-L", "-l", "-o",
            "-p", "-R", "-S", "-W",
            # R11-C-F2: cipher (-c), MAC (-m), query (-Q), control
            # command (-O) and tunnel (-w) each consume a value in BOTH
            # ssh zones — pre-host the value was taken for the HOST word
            # (`ssh -c aes128-ctr localhost psql -c 1` misjudged remote)
            # and in the post-host join zone it became a phantom first
            # remote command word that masked psql (`ssh localhost -c
            # aes128-ctr psql -c 1`); both really run psql here.
            "-c", "-m", "-Q", "-O", "-w"},
    "exec": {"-a", "--arg"},
    "strace": {"-b", "-e", "-E", "-o", "-p", "-P", "-s", "-S", "-u",
               "--output", "--expr", "--sort", "--strings", "--username"},
    "ltrace": {"-e", "-F", "-o", "-p", "-s", "-u", "-w", "-x"},
    "script": {"-t", "--timing"},
    "su": {"-s", "--shell"},
    **{sh: {"-o", "--option"} for sh in _SHELLS},
}
# Compound-command segmentation separators: ; | & newline backtick $ ( { }
# — but ONLY where they sit outside every quoting context (see
# _split_segments). { and } make `{ psql; }` yield a psql-leading segment.
# A closing paren is NOT a separator (in `echo $(date) psql` the trailing
# words are echo's arguments, not commands); the glue it leaves behind
# (`psql)` from `(psql)` / `$(psql)`) is stripped in _base instead.
_SEPARATORS = frozenset(";|&\n`$({}")
# Word terminators for redirect stripping (_scan_words): like _SEPARATORS
# but WITHOUT & — `2>&1` must stay one word there, or its & would leak
# into segmentation as a bogus background operator.
_WORD_BREAK = frozenset(";|\n`$({}")
# Word terminators INSIDE an already-split segment (C7-F1): after
# _split_segments ran, the only unquoted braces that can survive in a
# segment are the fused bare `{}` placeholder, so { and } are NOT word
# breaks there — `xargs -I{}` stays ONE word (glued option+placeholder,
# matching the old `re.finditer(r"\S+")` behavior) and a standalone `{}`
# surfaces as its own word in find-argument position.
_SEG_WORD_BREAK = _WORD_BREAK - {"{", "}"}
# A redirection operator at the start of an (unquoted) word: optional fd
# number, then > >> < << <<< <> &> &>> (longest first).
_REDIRECT_OP = re.compile(r"\d*(?:&>>|&>|>>|<>|<<<|<<|>|<)")
# D7-F6 companion — shell-semantics dequoting of a payload word: adjacent
# quote segments SPLICE (`'A'\''B'` is the word A'B), so extracting the
# payload by matching one quoted pair (as the old first-word extraction
# did, stopping at the first closing quote) lost the spliced tail.
# _DQ_SPAN matches a double-quoted body up to its closing quote — a
# C-speed regex so the 8 MB nested-payload regression fixture stays fast.
_DQ_SPAN = re.compile(r'[^"\\]*(?:\\.[^"\\]*)*"', re.S)
# R9-F6①: bash double-quote backslash semantics — ONLY \$ \` \" \\ (plus
# the backslash-newline continuation, already folded away upstream by
# _fold_continuations) drop their backslash inside "..."; every other \x
# keeps BOTH characters (bash-verified: v="a\x70b" prints a\x70b). The
# old blanket unescape turned `"ProxyCommand=$'\x70sql' -c 1"` into
# $'x70sql' — the ANSI payload vanished and the -o rescan saw a clean
# word. C-speed template sub keeps the multi-MB dquote fixtures fast.
_DQ_ESC = re.compile(r'\\([$`"\\])')
# D8-F2: $'...' ANSI-C quoting escapes (bash manual, "ANSI-C Quoting").
# \cX has its own branch in _ansi_unescape (C9-F3) and is deliberately
# not in this map; an unrecognized escape keeps its backslash (verified:
# echo $'a\qb' prints a\qb), which the later quote/backslash
# normalization of _base then approximates — safe side. R11-C-F3: a
# backslash-NEWLINE pair is deliberately NOT in the map either — inside
# ANSI-C quoting bash keeps BOTH characters (the C10-N2 probe: `printf %s
# $'a\<LF>b'` emits a \ LF b), so decoding it to "" reassembled `$'ps\<LF>
# ql'` into the command psql and over-denied; the pair now falls to the
# unknown-escape branch and survives verbatim.
_ANSI_SIMPLE = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f",
                "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\",
                "'": "'", '"': '"', "?": "?"}
_HEX = frozenset("0123456789abcdefABCDEF")
_OCT = frozenset("01234567")
# Fast path for _violates_core: a command containing NONE of these
# separator characters anywhere (inside quotes or not) segments into
# exactly one piece, so the segmentation walk can be skipped — deep
# nested -c payloads (the multi-MB DoS fixtures) are all-quotes text and
# hit this path every nesting level.
_SEP_ANY = re.compile(r"[;|&\n`$({}]")

DENY_MSG = (
    "裸 psql/pgbench 被本仓 hook 拒(铁律 1:计时证据只认 pgtuner 账本,psql \\timing "
    "受网络与终端影响不可作证据)。\n"
    "改用:只读探看 `pgtuner sql \"SELECT ...\"` / 计时 `pgtuner rebench --queries-file q.sql`"
    "(utility 语句→script 单遍;纯 SELECT→取中位;--setup 出前后三段) / 负载 TPS "
    "`pgtuner bench` / plan `pgtuner planbench`;场景路由看 `pgtuner --help`。\n"
    "确需裸命令(psql-variant 基准等):在命令行尾加 `# allow-raw-psql` 放行。"
)
_DEEP_MSG = (
    "命令的 shell -c/eval 载荷嵌套超过 20 层,超出本 hook 的解析上限,拒绝执行"
    "(防深嵌套 DoS,fail-closed:宁可拒绝也不超时放行)。\n"
    "请展平命令(去掉多层 bash -c/eval 包装)再试;计时请走 pgtuner 通道"
    "(见 `pgtuner --help`);确需放行:命令行尾加 `# allow-raw-psql`。"
)


class _TooDeep(Exception):
    """Nested -c/eval payload rescanning exceeded _MAX_DEPTH.

    Raised, not returned: the caller must DENY (fail-closed) — a hook
    timeout/fail-open on hostile nesting is exactly the R3-D3.3 bug.
    """


def _base(tok: str):
    # Normalize an unquoted bash word to its command name (R3-D3.2): bash
    # splices adjacent quotes and unescapes \x, so 'p'sql / p""sql / ps\ql /
    # \psql all resolve to psql. Stripping every quote and backslash
    # character is the fixed point of (strip quotes; unescape \x) — it also
    # makes re-escaped payload words from nested `bash -c "bash -c \"...\""`
    # chains (\"bash, \\"bash, ...) resolve correctly. Then a trailing `)`
    # run (`psql)` from `(psql)` / `$(psql)` — a closing paren is
    # deliberately not a segmentation separator, see _SEPARATORS) and the
    # directory part go away.
    # D8-F2: $'...' (ANSI-C, $"..." locale) spans decode FIRST — bash
    # really executes `$'\x70sql' -c 1` as psql (verified) — and a span
    # that fails to decode returns None: the caller denies fail-closed
    # instead of judging the raw escape text.
    if "$'" in tok or '$"' in tok:
        dec = _ansi_decode(tok)
        if dec is None:
            return None
        tok = dec
    for ch in "'\"\\":
        tok = tok.replace(ch, "")
    return tok.rstrip(")").rsplit("/", 1)[-1]


def _ssh_is_local(host: str) -> bool:
    # R5-D5-F5: ssh host-word equivalence normalization — LOCALHOST,
    # `localhost.` (trailing-dot FQDN form) and `[::1]` (bracketed IPv6)
    # name the same machine as the R4 local set. Any IPv4 form starting
    # with 127. is loopback (inet_aton shorthand: 127.1 == 127.0.0.1);
    # over-blocking other 127.* targets is the safe direction.
    # C6-F1/D6-F3: OpenSSH hands the host word to getaddrinfo, whose numeric
    # parser is inet_aton(3) — it accepts single-u32 decimal (2130706433),
    # hex (0x7f000001, case-insensitive), octal (0177.0.0.1) and part-count
    # shorthands (0x7f.1, 0x7f.0.1, 0177.1), ALL measured resolving to
    # 127.0.0.1. Any such form whose first octet is 127 means "runs on THIS
    # machine". Measured NON-local despite weird looks: 0x7f → 0.0.0.127 (hygiene:allow —
    # ssh host-word parser fixture; connect to it times out, first octet
    # 0), 0x08080808 → 8.8.8.8 (hygiene:allow — fixture).
    # Forms inet_aton rejects go to ipaddress: full-form IPv6 loopback
    # (0:0:0:0:0:0:0:1) and embedded-IPv4 ::0.0.0.1 (hygiene:allow —
    # parser fixture; == ::1) are loopback;
    # ::ffff:127.0.0.1 is not .is_loopback itself but its ipv4_mapped side
    # is 127.0.0.1 and a connect there measured reaching a local listener —
    # local too. The deprecated IPv4-COMPATIBLE 0:0:0:0:0:0:127.0.0.1
    # (::7f00:1) measured network-unreachable — remote. Machine-name aliases
    # that /etc/hosts maps to loopback (ip6-localhost) are a declared remote
    # boundary: this hook never resolves names.
    # C7-F3: an optional user@ prefix (`user@127.0.0.1`, `root@localhost`)
    # is stripped first — the host part alone decides locality (previously
    # the prefix hid the loopback target and `ssh user@127.0.0.1 psql`
    # slipped through as "remote").
    h = host.rsplit("@", 1)[-1].lower().rstrip(".")
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if h in _SSH_LOCAL or h.startswith("127."):
        return True
    try:
        packed = socket.inet_aton(h)                  # numeric-only, no lookup
    except OSError:
        packed = None
    if packed is not None:
        return (socket.ntohl(struct.unpack("=I", packed)[0]) >> 24) == 127
    try:
        addr = ipaddress.ip_address(h)
    except ValueError:
        return False                                  # unparseable word: declared remote boundary
    if addr.is_loopback:
        return True
    mapped = getattr(addr, "ipv4_mapped", None)       # ::ffff:127.0.0.1 → 127.0.0.1
    return mapped is not None and mapped.is_loopback


def _dq_unescape(body: str) -> str:
    # R9-F6①: unescape ONE double-quoted body the way bash does — only
    # \$ \` \" \\ lose the backslash; any other \x keeps both characters
    # (the backslash-newline continuation is gone before this point:
    # _fold_continuations already folded it). C-speed template sub so the
    # multi-MB nested-dquote DoS fixture stays fast.
    if "\\" not in body:
        return body
    return _DQ_ESC.sub(r"\1", body)


def _dequote(word: str) -> str:
    # Shell-semantics dequoting of ONE word (D7-F6): '...' is literal,
    # "..." unescapes only the bash-special set (_dq_unescape), an
    # unquoted \x unescapes, and adjacent segments splice — `'A'\''B'`
    # yields A'B. An unterminated quote extends to end of word
    # (fail-open on data, same as _split_segments/_scan_words). Fast path
    # for quote-free words; quoted bodies are handled by C-speed regex/find
    # so the multi-MB nested-payload fixture stays quick.
    if "'" not in word and '"' not in word and "\\" not in word:
        return word
    out = []
    i, n = 0, len(word)
    while i < n:
        c = word[i]
        if c == "'":
            j = word.find("'", i + 1)
            j = n if j == -1 else j
            out.append(word[i + 1:j])
            i = j + 1
        elif c == '"':
            m = _DQ_SPAN.match(word, i + 1)
            if m:
                out.append(_dq_unescape(word[i + 1:m.end() - 1]))
                i = m.end()
            else:                                       # unterminated: to end of word
                out.append(_dq_unescape(word[i + 1:]))
                i = n
        elif c == "\\" and i + 1 < n:
            out.append(word[i + 1])
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _ansi_unescape(body: str):
    # D8-F2: decode the escape sequences of ONE $'...' body (\xXX hex,
    # \ooo octal, \uXXXX / \UXXXXXXXX unicode + the _ANSI_SIMPLE map).
    # Returns None only for an out-of-range \u/\U code point — bash refuses
    # that whole word, so the caller denies fail-closed. Unknown escapes
    # keep their backslash (verified bash behavior). C9-F3: \cX decodes to
    # chr(ord(X) & 0x1F) (bash manual), and a decoded NUL — \c@ / \c`,
    # \x00, \000, \u0000 — TRUNCATES the word at that point: bash builds
    # the word as a C string, so the NUL and everything after it are
    # dropped (bash-verified: $'psql\c@' runs psql, $'p\0sql' is the word
    # `p`, NOT psql). The old drop-NUL-in-place reassembled banned words
    # (`p\0sql` → psql); now the decoded prefix is returned as-is. Trailing
    # lone backslash stays; trailing lone \c keeps both characters too
    # (bash-verified: $'a\c' prints a\c).
    out = []
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        if i + 1 >= n:
            out.append("\\")
            break
        e = body[i + 1]
        if e in _ANSI_SIMPLE:
            out.append(_ANSI_SIMPLE[e])
            i += 2
        elif e == "c":
            # C9-F3: \cX control escape. X=@ (0x40) and X=` (0x60) both
            # mask to 0 → NUL → truncate; other X give chr(ord(X) & 0x1F).
            # C10-F2: bash masks the FIRST BYTE of X in the command's byte
            # string, not its code point — bash-verified: $'psql\cࠁ'
            # (U+0801, UTF-8 E0 A0 81) truncates to `psql` (E0 & 0x1F = 0
            # → NUL) and really executes it, while the old ord() rule gave
            # 0x801 & 0x1F = 1, kept a \x01 tail and allowed the word.
            # Single fix point: every caller (_base via _ansi_decode, the
            # _scan_words dequote feed, _opt_payload_violates) funnels
            # through here. Divergence of the byte rule vs the code-point
            # rule exists only for non-ASCII X and can only push toward
            # deny/truncate (never reassemble a banned word bash would
            # not build) — harmless.
            if i + 2 >= n:
                out.append("\\c")                    # lone trailing \c: literal
                i += 2
                continue
            v = body[i + 2].encode("utf-8")[0] & 0x1F
            if v:
                out.append(chr(v))
            else:
                return "".join(out)                  # NUL: word ends here
            i += 3
        elif e == "x":
            j = i + 2
            while j < n and j < i + 4 and body[j] in _HEX:
                j += 1
            if j == i + 2:
                out.append("\\x")                    # \x without digits: literal
                i = 2 + i
            else:
                v = int(body[i + 2:j], 16)
                if v:
                    out.append(chr(v))
                else:
                    return "".join(out)              # \x00 NUL: word ends here
                i = j
        elif e in _OCT:
            j = i + 1
            while j < n and j < i + 4 and body[j] in _OCT:
                j += 1
            v = int(body[i + 1:j], 8) & 0xFF
            if v:
                out.append(chr(v))
            else:
                return "".join(out)                  # \0 NUL: word ends here
            i = j
        elif e in ("u", "U"):
            width = 4 if e == "u" else 8
            j = i + 2
            while j < n and j < i + 2 + width and body[j] in _HEX:
                j += 1
            if j == i + 2:
                out.append("\\" + e)                 # no digits: literal
                i += 2
            else:
                v = int(body[i + 2:j], 16)
                if v > 0x10FFFF:
                    return None
                if v:
                    out.append(chr(v))
                else:
                    return "".join(out)              # \u0000 NUL: word ends here
                i = j
        else:
            out.append("\\" + e)                     # unknown escape: keep both
            i += 2
    return "".join(out)


def _ansi_decode(word: str):
    # D8-F2: statically expand the $'...' (ANSI-C) and $"..." (locale)
    # spans of ONE word — bash resolves `$'\x70sql'` to the word `psql` and
    # runs it (verified), and `$"..."` with double-quote semantics. Other
    # characters (including plain quotes, spliced around the spans) pass
    # through untouched for _base's normalization. Returns None when a $'
    # span is unterminated or its body fails to decode: bash would reject
    # that line, and the caller DENIES fail-closed rather than guess.
    if "$'" not in word and '$"' not in word:
        return word
    out = []
    i, n = 0, len(word)
    while i < n:
        if word[i] == "$" and i + 1 < n and word[i + 1] == "'":
            j = i + 2
            while j < n:
                if word[j] == "\\" and j + 1 < n:
                    j += 2                           # \' cannot close the span
                elif word[j] == "'":
                    break
                else:
                    j += 1
            if j >= n:
                return None                          # unterminated $' — deny
            body = _ansi_unescape(word[i + 2:j])
            if body is None:
                return None                          # bad \u/\U escape — deny
            out.append(body)
            i = j + 1
        elif word[i] == "$" and i + 1 < n and word[i + 1] == '"':
            m = _DQ_SPAN.match(word, i + 2)
            if m:
                out.append(_dq_unescape(word[i + 2:m.end() - 1]))
                i = m.end()
            else:                                    # unterminated $": data to end
                out.append(word[i + 2:])
                i = n
        else:
            out.append(word[i])
            i += 1
    return "".join(out)


# D7-F5: ssh -o options whose value THIS machine executes. ProxyCommand and
# LocalCommand are run through the local /bin/sh -c (review-verified on this
# box: a ProxyCommand marker file landed on disk); ProxyJump only names jump
# hosts — no command executes — but rescanning its value is harmless.
# ssh_config keys are case-insensitive, hence the lower() compare.
_SSH_EXEC_KEYS = ("proxycommand=", "localcommand=", "proxyjump=")


def _ssh_opt_payload(word: str):
    # The KEY=VALUE text of an ssh -o argument (the glued tail after `-o`
    # or the separate value word). Returns the post-= payload when the key
    # is one of _SSH_EXEC_KEYS, else None (ordinary option value).
    low = word.lower()
    for key in _SSH_EXEC_KEYS:
        if low.startswith(key):
            return word[len(key):]
    return None


def _opt_payload_violates(payload: str, depth: int) -> bool:
    # Strip ONE enclosing quote pair (`ProxyCommand='psql -c 1'`), then
    # rescan the value with the full local rules: the local sh -c parses
    # exactly that string, so quoted-space payloads resolve correctly.
    # R9-F6②: a $'...' ANSI-C shell in the value is decoded BEFORE the
    # rescan — the LOCAL sh re-parses the option value, and on sh→bash
    # systems `$'\x70sql' -c 1` really expands to `psql -c 1` (form 1
    # verified; form 2 `$'psql -c 1'` is one shell word there but is
    # denied all the same — fail-closed safe side). A span that cannot
    # decode denies fail-closed, mirroring _base's D8-F2 contract.
    # R9-F6③: the KEY= prefix was already stripped by _ssh_opt_payload
    # before this runs, and the recursion's own assignment check only
    # skips LEADING assignments (scanning the remainder, which is exactly
    # the sh semantics), so the rescan can never be short-circuited by a
    # KEY=-looking word.
    if len(payload) >= 2 and payload[0] in "'\"" and payload[-1] == payload[0]:
        payload = payload[1:-1]
    if "$'" in payload:
        dec = _ansi_decode(payload)
        if dec is None:
            return True                               # undecodable $' span — deny
        payload = dec
    return _violates_core(payload, depth + 1)


def _split_segments(command: str) -> list:
    # Quote-aware compound-command segmentation (review R2-A: the old regex
    # splitter cut at `;`/`|` inside quoted strings and denied commands like
    # `echo "use pgtuner; psql is banned"`). Bash-style state walk:
    #   '...'  fully literal (no escapes inside — bash semantics);
    #   "..."  literal except backslash escapes; $-expansion inside is NOT
    #          parsed (declared simplification, see module docstring);
    #   \X     outside quotes escapes the next character;
    #   ` $ ( { } ; | & newline  separate segments only when unquoted
    # (an unquoted backtick is a separator: its content is a nested command
    # list that gets scanned as its own segments). An unterminated quote
    # simply extends to end of input — fail-open. Each segment is returned
    # as (text, prev_sep) where prev_sep is the separator character that
    # OPENED it (None for the first): a segment starting inside a
    # $()-substitution begins with command content whose trailing `)` is
    # substitution glue (`date)` in `echo $(date) psql`), not a case
    # pattern — the clause detection in _segment_violates needs to know.
    segments, buf = [], []
    prev = None
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch == "'":
            j = command.find("'", i + 1)
            j = n if j == -1 else j
            buf.append(command[i:j + 1])
            i = j + 1
        elif ch == '"':
            m = _DQ_SPAN.match(command, i + 1)        # C-speed body+close scan
            j = (m.end() - 1) if m else n            # (8 MB nested-payload fixtures)
            buf.append(command[i:j + 1])
            i = j + 1
        elif ch == "\\":
            buf.append(command[i:i + 2])
            i += 2
        elif ch == "{" and command[i + 1:i + 2] == "}":
            # C7-F1: a BARE `{}` is find's placeholder — by far its most
            # common spelling — and with no comma / range it is never a
            # brace expansion: keep it as word material instead of letting
            # `{` and `}` each split the segment (which made the segment-
            # local -exec state vanish, so `find . -exec grep x {} \;
            # -exec psql \;` lost its second clause to the check). The
            # grouping syntax `{ cmd; }` keeps splitting — a brace followed
            # by anything but `}` still opens a segment (`{ psql; }`
            # stays denied, pinned).
            buf.append("{}")
            i += 2
        elif ch == "$" and command[i + 1:i + 2] in ("'", '"'):
            # D8-F2: $'...' (ANSI-C) and $"..." (locale) quoting — the $
            # prefix makes this part of the CURRENT word, never a
            # substitution opener, so it is NOT a segment separator: the
            # whole span (escape-aware, `\'` cannot close it) stays inside
            # this segment as ordinary word material for _scan_words/_base
            # to decode (`$'\x70sql' -c 1` and `ps$'q'l -c 1` both run
            # psql — verified). $ followed by anything else ( `(`, `{`, a
            # name … ) keeps the R2 substitution semantics.
            if command[i + 1] == "'":
                j = i + 2
                while j < n:
                    if command[j] == "\\" and j + 1 < n:
                        j += 2
                    elif command[j] == "'":
                        break
                    else:
                        j += 1
                end = (j + 1) if j < n else n         # unterminated: to end
            else:
                m = _DQ_SPAN.match(command, i + 2)
                end = m.end() if m else n
            buf.append(command[i:end])
            i = end
        elif ch in _SEPARATORS:
            segments.append(("".join(buf), prev))
            prev = ch
            buf = []
            i += 1
        else:
            buf.append(ch)
            i += 1
    segments.append(("".join(buf), prev))
    return segments


def _fold_continuations(command: str) -> str:
    # `ps\<LF>ql --version` is `psql --version` to bash (R3-C-F5): fold
    # away backslash-newline OUTSIDE single quotes (inside '...' it is
    # literal data). Double-quoted "\<LF>" is also folded by bash, but
    # keeping it literal there is the conservative no-op for data.
    # C10-N2: inside $'...' (ANSI-C) a \\<LF> pair KEEPS BOTH characters —
    # bash-verified: `printf %s $'a\<LF>b'` emits a \ LF b (61 5c 0a 62) —
    # so the span is skipped like '...' (folding it could only ever cause
    # a conservative deny, but it was semantically wrong). $"..." keeps
    # dquote semantics (bash folds there), so it is NOT skipped.
    if "\\\n" not in command:
        return command
    out = []
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch == "$" and command[i + 1:i + 2] == "'":
            # C10-N2: copy the whole $'...' span verbatim (escape-aware —
            # \' cannot close it, same walk as _split_segments).
            j = i + 2
            while j < n:
                if command[j] == "\\" and j + 1 < n:
                    j += 2
                elif command[j] == "'":
                    break
                else:
                    j += 1
            end = (j + 1) if j < n else n         # unterminated: to end
            out.append(command[i:end])
            i = end
        elif ch == "'":
            j = command.find("'", i + 1)
            j = n if j == -1 else j
            out.append(command[i:j + 1])
            i = j + 1
        elif ch == "\\" and command[i + 1:i + 2] == "\n":
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _scan_words(command: str, word_break=None, dequote=False):
    # Quote-aware word spans: words end at whitespace or at an unquoted
    # break character; quoting and \x never end a word. An unterminated
    # quote simply extends to end of input. Used by _strip_redirects with
    # _WORD_BREAK and, since D7-F6, by _segment_violates with
    # _SEG_WORD_BREAK: the old `re.finditer(r"\S+")` tokenizer there split
    # on spaces INSIDE quotes, so `FOO='1 2' psql -c 1` surfaced as the
    # fragments `FOO='1` + `2'` and the scan stopped on `2'` as an
    # ordinary command — the wrapped psql really executed. The quote-aware
    # walk keeps each quoted word whole; the FOO=... assignment-prefix
    # skip keeps working on the whole word (env VAR='a b' psql ... too).
    # dequote=True additionally yields the shell-dequoted word text
    # (spliced segments, same semantics as _dequote) so payload rescans
    # do not re-walk a multi-MB quoted body a second time.
    br = _WORD_BREAK if word_break is None else word_break
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch == "$" and command[i + 1:i + 2] in ("'", '"'):
            pass               # D8-F2: $' / $" starts a word (see inner walk)
        elif ch.isspace() or ch in br:
            i += 1
            continue
        start = j = i
        parts = [] if dequote else None
        run = i                 # start of the pending ordinary-text run
        while j < n:
            c = command[j]
            if c == "'":
                k = command.find("'", j + 1)
                k = n if k == -1 else k
                if parts is not None:
                    parts.append(command[run:j])
                    parts.append(command[j + 1:k])
                    run = k + 1
                j = k + 1
            elif c == '"':
                m = _DQ_SPAN.match(command, j + 1)    # C-speed body+close scan
                end = m.end() if m else n             # (8 MB nested-payload fixtures)
                if parts is not None:
                    parts.append(command[run:j])
                    body = command[j + 1:end - 1] if m else command[j + 1:end]
                    parts.append(_dq_unescape(body))  # R9-F6①: bash dquote set only
                    run = end
                j = end
            elif c == "$" and command[j + 1:j + 2] in ("'", '"'):
                # D8-F2: $'...' / $"..." quoting is word MATERIAL (bash
                # never splits there) — consume the whole span escape-aware
                # so its quotes do not end the word, and feed the DECODED
                # body into w_tok (payload rescans of `bash -c $'psql -c
                # 1'` then see the real command string).
                if command[j + 1] == "'":
                    k = j + 2
                    while k < n:
                        if command[k] == "\\" and k + 1 < n:
                            k += 2                    # \' cannot close the span
                        elif command[k] == "'":
                            break
                        else:
                            k += 1
                    closed = k < n
                    kend = (k + 1) if closed else n
                    if parts is not None:
                        parts.append(command[run:j])
                        body = command[j + 2:k] if closed else command[j + 2:]
                        if closed and body:
                            dec = _ansi_unescape(body)
                            parts.append(body if dec is None else dec)
                        else:
                            parts.append(body)        # unterminated: raw tail
                        run = kend
                    j = kend
                else:
                    m = _DQ_SPAN.match(command, j + 2)
                    kend = m.end() if m else n
                    if parts is not None:
                        parts.append(command[run:j])
                        span = command[j + 2:kend - 1] if m else command[j + 2:kend]
                        parts.append(_dq_unescape(span))  # R9-F6①: dquote set only
                        run = kend
                    j = kend
            elif c == "\\":
                if parts is not None:
                    parts.append(command[run:j])
                    if j + 1 < n:
                        parts.append(command[j + 1])
                    run = min(j + 2, n)
                j += 2
            elif c.isspace() or c in br:
                break
            else:
                j += 1
        e = min(j, n)
        if parts is not None:
            parts.append(command[run:e])
            yield start, e, "".join(parts)
        else:
            yield start, e
        i = j


def _strip_redirects(command: str) -> str:
    # R3-D3.1: a redirection before the command word used to make the scan
    # bail out on the redirect token as an "ordinary command" (`2>&1 psql
    # ...`, `< /dev/null psql ...`, `> out.txt psql ...`). Strip redirect
    # words outside quotes BEFORE segmentation: a self-contained one
    # (>/dev/null, 2>&1, <<'EOF') goes wholesale; a bare operator (>, 2>,
    # >>, <, <>) also swallows the following target word when only blanks
    # separate them. Process substitutions (<(cmd) / >(cmd)) are left
    # alone — the `(` segmentation already rescans their payload. Words
    # are only ever removed, never re-spliced, so quoting/newlines in the
    # surviving text (heredoc bodies!) keep their original shape.
    if "<" not in command and ">" not in command:
        return command
    words = list(_scan_words(command))
    kill = []
    for idx, (s, e) in enumerate(words):
        m = _REDIRECT_OP.match(command, s, e)
        if not m:
            continue
        if command[s:s + 3] == "<<<":
            # R11-D-F1①: a herestring word (bare `<<<` or glued `<<<X`)
            # SURVIVES stripping — its payload is a stdin SCRIPT for the
            # segment command, intercepted in _segment_violates when that
            # command is a shell (`bash <<< 'psql -c 1'` used to strip
            # down to a bare `bash` and pass while bash really ran psql).
            # fd-prefixed forms (`2<<<x`) still strip like before.
            continue
        if m.end() < e:                                # operator glued to its target
            if command[m.end()] == "(":
                continue                               # process substitution, not a redirect
            kill.append((s, e))
        elif idx + 1 < len(words):                     # bare operator + blanks + target
            s2, e2 = words[idx + 1]
            gap = command[e:s2]
            if command[s2:s2 + 1] != "(" and (gap == "" or not gap.strip()):
                kill.append((s, e2))
                continue
            kill.append((s, e))                        # dangling operator: drop it alone
        else:
            kill.append((s, e))
    if not kill:
        return command
    out, prev = [], 0
    for s, e in kill:
        out.append(command[prev:s])
        prev = max(prev, e)
    out.append(command[prev:])
    return "".join(out)


def _pipe_left_violates(left: str, depth: int) -> bool:
    # R11-D-F1②: a bare shell as the right side of a pipe reads the LEFT
    # command's OUTPUT as a script and really executes it (`echo 'psql -c
    # 1' | bash`, `echo psql | bash -s` — review-reproduced). Producer
    # output is statically unknowable in general (cat/grep), so the
    # safe-side approximation rescans: the producer's argument tail
    # (words[1:] — the echo/printf output shape, catches `echo psql |
    # bash -s`), each quoted word alone (catches `printf '%s\n' 'psql -c
    # 1' | bash`) and the join of all quoted words. Every deny path is a
    # banned-execution hit, so `echo 'grep psql f' | bash` stays allowed
    # (the rescan sees psql as a mere grep argument); a bare single-word
    # producer (`date | bash`) has no candidates and stays allowed.
    words = list(_scan_words(left, _SEG_WORD_BREAK, dequote=True))
    if len(words) < 2:
        return False
    decs = [w for _, _, w in words]
    quoted = [decs[i] for i, (s, e, _w) in enumerate(words)
              if "'" in left[s:e] or '"' in left[s:e]]
    cands = [" ".join(decs[1:])]
    cands.extend(quoted)
    if len(quoted) > 1:
        cands.append(" ".join(quoted))
    return any(_violates_core(c, depth + 1) for c in cands)


def _violates_core(command: str, depth: int = 0) -> bool:
    if depth > _MAX_DEPTH:
        raise _TooDeep                                  # deny, never time out (R3-D3.3)
    command = _strip_redirects(_fold_continuations(command))
    if not _SEP_ANY.search(command):
        # no separator character anywhere (quotes included): the whole
        # command is ONE segment — skip the segmentation walk (hot path
        # for deeply nested -c payloads, see _SEP_ANY)
        return _segment_violates(command, depth)[0]
    case_pattern = False         # inside an unterminated `case ... in` pattern list
    pipe_left = None             # R11-D-F1②: segment immediately left of a `|`-opened one
    for seg, prev_sep in _split_segments(command):
        if not seg.strip():
            continue
        hit, case_pattern = _segment_violates(
            seg, depth, prev_sep, case_pattern,
            pipe_left if prev_sep == "|" else None)
        if hit:
            return True
        pipe_left = seg
    return False


def _segment_violates(segment: str, depth: int = 0, prev_sep: str | None = None,
                      case_pattern: bool = False,
                      pipe_left: str | None = None) -> tuple:
    # Returns (violates, case_pattern): an unterminated `case ... in` pattern
    # list must carry across segments split at `|` (alternative patterns) or
    # `$` (case subject expansion) — the `)` word that closes it can live in
    # a later segment. pipe_left (R11-D-F1②) is the segment immediately left
    # of this one when this one opened with `|` — a bare shell here reads
    # that producer's output as a script (see _stdin_fed_violates).
    prefix = ""          # pending wrapper/shell whose option args may follow
    c_flag = False       # pending shell saw a -c style option: next real token starts its payload
    skip_value = False   # next token is the value of the previous option
    ssh_o_value = False  # that option is ssh's -o: its value word may carry a locally executed command (D7-F5)
    env_s_value = False  # that option is env's -S/--split-string: env splits+executes the value (R9-F5)
    su_user = False      # pending su expects its (first) bare argument: the user name
    ssh_host = False     # pending ssh already consumed its first bare argument: the host
    for_list = False     # inside a `for NAME in WORD...` header: words are values, not commands
    exec_cmd = False     # pending `find -exec/-execdir`: next word is the command find runs (R5-D5-F6)
    exec_args = False    # past an -exec command word: plain args until the `;`/`+` terminator
    exec_shell = None    # C6-F4: that command word was a _C_PAYLOAD member (sh/eval/script/su)
    exec_c = False       # C6-F4: it saw a -c style option — next bare word starts its payload
    exec_su_user = False  # C6-F4: it is su — first bare word is the user name
    exec_join = None     # R11-C-F1: that command word was watch/parallel/env — the clause joins into ONE command string
    exec_join_words = []  # R11-C-F1: the collected clause (command word first)
    coproc_name = False  # pending `coproc`: next bare non-reserved word is the NAME (R5-D5-F7)
    hs_payloads = []     # R11-D-F1①: (dequoted herestring payload, fed_a_shell) pairs
    hs_skip_next = False  # the word after a bare `<<<` is its payload

    def _stdin_fed_violates() -> bool:
        # R11-D-F1 exit check, shared by the ordinary-word stop and the
        # segment end: a bare shell — no -c payload was CONSUMED (the
        # payload branch returns before either exit; a trailing armed
        # c_flag means the payload word itself is missing and comes from
        # stdin/xargs-append, deny-safe) — executes what stdin feeds it
        # ON THIS MACHINE: herestring payloads recorded in this segment,
        # and (as a pipe's right side) the left producer's words.
        if hs_payloads and (prefix in _SHELLS
                            or any(fed for _p, fed in hs_payloads)):
            return any(_violates_core(p, depth + 1) for p, _f in hs_payloads)
        if prev_sep == "|" and pipe_left is not None and prefix in _SHELLS:
            return _pipe_left_violates(pipe_left, depth)
        return False

    # The generator is kept in a name so the ssh/eval join branches (C9-F1/
    # C9-F2) can drain the REMAINING words from inside the loop body — the
    # payload is the join of every word that follows, not just this one.
    words = _scan_words(segment, _SEG_WORD_BREAK, dequote=True)
    for w_start, w_end, w_tok in words:
        tok = segment[w_start:w_end]
        if hs_skip_next:
            hs_skip_next = False                    # payload word of a bare `<<<`
            hs_payloads.append((w_tok, prefix in _SHELLS and not c_flag))
            continue
        if exec_join is not None and exec_args:
            # R11-C-F1: collect EVERY clause word (options, numerics,
            # assignments included) — the join must be the whole argv find
            # passes to watch/parallel/env, not just the bare words the
            # branches below would keep.
            exec_join_words.append(w_tok)
        if case_pattern:
            if tok.endswith(")"):
                case_pattern = False                  # `x)` closes the pattern list; commands resume
            continue                                  # pattern words never execute: skip w/o banned check
        if for_list:
            if _base(tok) in _BANNED:
                return True, case_pattern             # declared over-rejection (R4 note 2): `for`/`in`
                                                     # consume no value args, so a bare banned word in
                                                     # the word list is denied — safe direction
            continue                                  # word-list words never terminate the scan
        if skip_value:
            skip_value = False
            if ssh_o_value:
                # D7-F5: this word is the value of `ssh -o KEY=VALUE`; an
                # execute-valued key (ProxyCommand=/LocalCommand=/ProxyJump=)
                # has its payload rescanned with the local rules — the value
                # runs through THIS machine's /bin/sh -c.
                ssh_o_value = False
                # C8-F2: match the key prefix on the DEQUOTED value — the
                # normal spelling quotes the whole value (`ssh -o
                # 'ProxyCommand=psql -c 1' host`), and OpenSSH executes it
                # anyway (review-verified with a marker file), but the raw
                # token starts with a quote so `proxycommand=` never
                # matched and the payload was never rescanned.
                payload = _ssh_opt_payload(w_tok)
                if payload is not None and _opt_payload_violates(payload, depth):
                    return True, case_pattern
            if env_s_value:
                # R9-F5: this word is the value of `env -S`/`--split-string`;
                # GNU env splits that string on whitespace and EXECUTES the
                # first word with the rest as arguments (probe-verified:
                # `env -S 'printf x %s' MARKER`-shaped forms run), so the
                # dequoted value is a bash -c-style payload and is rescanned.
                # C10-F1 companion: the operands AFTER the -S value are
                # APPENDED to that executed argv (`env -S grep psql f`
                # executes `grep psql f`; `env -S 'sh -c' 'psql -c 1'`
                # executes sh -c psql — the operand lands in the -c payload
                # slot), so the payload is the value word PLUS every
                # remaining word of the segment, joined and rescanned as
                # one command string — exactly eval's join. Safe side: a
                # shell operator inside a tail word (`;` etc.) is re-parsed
                # by the rescan while env treats it as literal argv — that
                # can only over-deny, never miss.
                env_s_value = False
                joined = " ".join([w_tok] + [w for _, _, w in words])
                if _violates_core(joined, depth + 1):
                    return True, case_pattern
            continue
        if tok.startswith("<<<"):
            # R11-D-F1①: herestring — the operator word survives redirect
            # stripping (see _strip_redirects). The glued form carries its
            # payload in the same word; the bare form takes the NEXT word.
            # The dequoted payload is a stdin SCRIPT for the segment's
            # command: recorded with whether a shell WITHOUT a -c payload
            # holds command position right now; the rescan fires at the
            # segment exits (_stdin_fed_violates) for a shell now or
            # earlier (`<<< p bash` redirect-first form included).
            if len(tok) > 3:
                hs_payloads.append((_dequote(tok[3:]),
                                    prefix in _SHELLS and not c_flag))
            else:
                hs_skip_next = True
            continue
        if prefix == "su":
            if tok == "-":
                continue                              # `su - -c ...` legacy login dash
            if su_user and not tok.startswith("-") and not c_flag:
                su_user = False                       # `su postgres -c ...`: user name argument.
                continue                              # D7-F6 companion: an option word (`su -c
                                                     # 'payload' user`) is NOT the user name, and
                                                     # once -c armed c_flag the next word is the
                                                     # payload — neither may be swallowed here
                                                     # (the old space-split tokenizer let the
                                                     # `'psql` fragment hit _BANNED by accident
                                                     # instead).
        if prefix == "eval":
            # C9-F2: eval joins ALL its arguments with spaces into ONE
            # command string and re-parses it — bash-verified:
            # `eval 'echo hi;' 'printf %s x'` runs BOTH halves, and
            # `eval 'true' '&&' ...` joins the operator in too. The old
            # approximation (rescan the first `=`-word, then return on the
            # first payload word) dropped every tail word, so those forms
            # passed while bash really executed. The join is the dequoted
            # argv exactly as bash concatenates it; the recursion's own
            # rules (assignment skip, compound keywords, nested -c) then
            # apply to the string as a whole. Fires on the FIRST argument
            # word whatever it looks like (`=`, `-x`, quoted — eval has no
            # options) and returns: the whole rest IS the payload.
            return _violates_core(
                " ".join([w_tok] + [w for _, _, w in words]), depth + 1), False
        if prefix in _JOIN_EXEC and not (w_tok.startswith("-") and len(w_tok) > 1):
            # R10-F2: watch/parallel do not exec their command arguments —
            # they space-JOIN them and hand the string to /bin/sh -c (watch:
            # probe-verified, marker file landed; parallel: same family,
            # lexical pin, not installed here), so the whole tail is ONE
            # command string and gets eval's join-then-rescan treatment.
            # Fires on the first NON-option word only (getopt sees the
            # dequoted argv, hence w_tok): leading options were consumed by
            # the option branch above (value options via skip_value), and
            # like ssh's C10-F1 fix every word after the first command word
            # is command material — `watch 'psql -c 1'` joins to `psql -c 1`
            # and is denied, the previously-denied unquoted `watch psql -c
            # 1` keeps its deny through the same rescan, and clean joins
            # (`watch -n1 grep psql f`) stay allowed.
            return _violates_core(
                " ".join([w_tok] + [w for _, _, w in words]), depth + 1), False
        if "=" in tok and not tok.startswith("-") and not (
                (prefix in _C_PAYLOAD and c_flag and prefix != "eval")
                or (exec_args and exec_shell in _C_PAYLOAD and exec_c)):
            # environment-assignment prefix (FOO=bar / env PGOPTIONS=..).
            # C8-F1: EXCEPT while a -c-style payload word is pending — the
            # word after `bash -c` / `su USER -c` / `script -qec` (or after
            # an -exec'd _C_PAYLOAD shell's -c) IS a command string even
            # when it contains `=`, so it must fall through to the payload
            # rescan below instead of being eaten here as an assignment.
            # The recursion's own assignment skip then handles prefixes
            # INSIDE the payload: in `bash -c "PGPASSWORD=x psql -c 1"`
            # the inner scan sees PGPASSWORD=x as a genuine assignment
            # prefix and psql in command position, and denies it. eval is
            # excluded (handled just above) because its payload is the
            # JOIN of all arguments, not this one word.
            continue
        if prefix and tok.startswith("-") and len(tok) > 1:
            if prefix == "command" and tok in ("-v", "-V"):
                return False, case_pattern            # `command -v NAME` only looks names up, never executes
            if tok in _VALUE_OPTS.get(prefix, ()):
                skip_value = True
                ssh_o_value = prefix == "ssh" and tok == "-o"
                env_s_value = prefix == "env" and tok in ("-S", "--split-string")
            if prefix == "ssh" and tok.startswith("-o") and len(tok) > 2:
                # D7-F5: glued form `-oProxyCommand=psql` — the option and
                # its KEY=VALUE tail are one word (quote-aware: also
                # `-oProxyCommand='psql -c 1'`); execute-valued keys get
                # their payload rescanned, everything else is a plain
                # option that consumes no separate value word. C8-F2: the
                # key match uses the DEQUOTED tail (`-o'ProxyCommand=...'`
                # glued with a quoted value word resolves the same way).
                payload = _ssh_opt_payload(w_tok[2:])
                if payload is not None and _opt_payload_violates(payload, depth):
                    return True, case_pattern
            if prefix == "env" and ((tok.startswith("-S") and len(tok) > 2)
                                    or tok.startswith("--split-string=")):
                # R9-F5 glued form (`env -S'psql -c 1'`, `env
                # --split-string='psql -c 1'`): the option and its value
                # are one word, so the dequoted tail is the payload env
                # splits and executes — rescan it. `-S` exact (len 2) and
                # bare `--split-string` go the separate-value route above.
                tail = w_tok[2:] if tok.startswith("-S") else w_tok[len("--split-string="):]
                if tail and _violates_core(tail, depth + 1):
                    return True, case_pattern
            if prefix in _C_PAYLOAD and "c" in tok:
                c_flag = True                         # -c / -lc / -qec ... (payload follows)
            if exec_args and exec_shell in _C_PAYLOAD and "c" in tok:
                exec_c = True                         # C6-F4: -exec'd shell's -c (payload follows)
            if prefix == "find" and tok in ("-exec", "-execdir"):
                exec_cmd = True                       # R5-D5-F6: next word is a command find executes
            continue                                  # wrapper/shell option (-oL, -I{}, -k, -euo ...)
        base = _base(tok)
        if base is None:
            return True, case_pattern                 # D8-F2 fail-closed: undecodable $' word —
                                                     # bash would reject the line; deny, never guess
        if prefix == "ssh":
            # Before the numeric-argument skip below: a bare first ssh
            # argument is the HOST (`ssh 127.0.0.1 ...`), not `timeout 30`.
            if not ssh_host:
                ssh_host = True                       # first bare argument of ssh = the host word
                if not _ssh_is_local(base):
                    return False, case_pattern        # any other host: declared remote boundary
                # R4-D-F5 + R5-D5-F5: local host — the "remote" command runs
                # on THIS machine. C9-F1: ssh JOINS the command argv words
                # with spaces into ONE command string and hands it to the
                # remote shell (local host ⇒ eval-on-this-machine), so every
                # remaining word of the segment is collected (dequoted),
                # joined and rescanned as a whole. The old code rescanned
                # only a fully-quoted FIRST word and returned early, so all
                # tail words were dropped: `ssh localhost 'A=1' psql -c 1`
                # and `ssh localhost 'echo hi;' psql -c 1` passed while the
                # joined `A=1 psql -c 1` / `echo hi; psql -c 1` really ran
                # psql (U-batch regression, review-reproduced).
                rest = [w for _, _, w in words]       # drain: the argv after the host
                if not rest:
                    continue                          # bare `ssh localhost`: interactive shell only
                cmd_words = []
                k = 0
                while k < len(rest):
                    w = rest[k]
                    if not cmd_words:
                        # C10-F1: the option zone ends at the FIRST command
                        # word — OpenSSH parses post-destination options only
                        # until then (probe-verified: `ssh -G localhost env
                        # -S` prints config rc=0, while `ssh -G -S` errors
                        # rc=255 — the trailing -S after the command word
                        # `env` is remote-command material, not an ssh
                        # option). Everything from the first non-option word
                        # on — `-`-prefixed words included — is joined into
                        # the remote command string; the old loop pulled
                        # `-` words out indiscriminately, so `ssh localhost
                        # 'env' '-S' 'psql -c 1'` let `-S` swallow
                        # 'psql -c 1' as its ControlPath value and the
                        # stripped join `env` looked clean while the remote
                        # side really executed `env -S psql -c 1`.
                        if w == "--":
                            k += 1
                            break                     # getopt terminator: the rest is all command
                        if w.startswith("-") and len(w) > 1:
                            # glibc getopt PERMUTES options found after the
                            # destination word (probe-verified: `ssh -G
                            # localhost -oPort=2222` resolves the option), so
                            # option words BEFORE the command are pulled out
                            # of the join — and -o values keep the
                            # D7-F5/C8-F2/R9-F6 payload checks. Unknown
                            # -words are dropped from the join: real ssh
                            # errors out on them, nothing executes.
                            if w in _VALUE_OPTS["ssh"]:
                                k += 1
                                if w == "-o" and k < len(rest):
                                    payload = _ssh_opt_payload(rest[k])
                                    if payload is not None and _opt_payload_violates(payload, depth):
                                        return True, case_pattern
                            elif w.startswith("-o") and len(w) > 2:
                                payload = _ssh_opt_payload(w[2:])
                                if payload is not None and _opt_payload_violates(payload, depth):
                                    return True, case_pattern
                            k += 1
                            continue
                    cmd_words.append(w)
                    k += 1
                cmd_words.extend(rest[k:])
                if cmd_words:
                    return _violates_core(" ".join(cmd_words), depth + 1), False
                continue                              # options only: no remote command at all
        if prefix and tok[0].isdigit():
            continue                                  # numeric wrapper argument (timeout 30, nice 5)
        if prefix == "find":
            # R5-D5-F6: find's operands/predicate values never hold a shell
            # command position — except the FIRST word after -exec/-execdir,
            # which find itself executes once per match. Only that word is
            # judged (declared conservative scope, see module docstring).
            # C6-F4: when that word is a _C_PAYLOAD member (bash/sh family,
            # eval, script, su), the SAME -c option tracking and payload
            # rescan as the wrapper path applies — `find . -exec sh -c
            # 'psql -c 1' \;` runs psql on THIS machine once per match.
            # Generic interpreters (`-exec python3 -c ...`) keep the R5-D5-F8
            # declared boundary. A clean rescan does not return early:
            # later `-exec psql \;` clauses in the same find stay checked.
            if exec_cmd:
                exec_cmd = False
                if base in _BANNED:
                    return True, case_pattern         # `find . -exec psql \;`: psql runs per match
                exec_args = True                      # not banned: it is the -exec command; its args follow
                exec_shell = base if base in _C_PAYLOAD else None
                exec_c = base in _EVAL                # eval: first argument IS the payload
                exec_su_user = base == "su"           # su: first bare argument is the user name
                # R11-C-F1: a _JOIN_EXEC member (watch/parallel) or env as
                # the -exec command word does not run the clause argv
                # directly — watch/parallel space-JOIN it into ONE /bin/sh
                # -c string and env -S splits+executes the value (R9-F5
                # operands included), so the command word plus every clause
                # word up to the terminator is joined and rescanned; the
                # recursion reapplies the exact wrapper-path semantics,
                # which the old first-word-only tracking never saw
                # (`find . -exec watch 'psql -c 1' \;` passed while find
                # really ran psql via watch's sh -c — review-reproduced).
                if base in _JOIN_EXEC or base == "env":
                    exec_join, exec_join_words = base, [w_tok]
                else:
                    exec_join, exec_join_words = None, []
            elif exec_args:
                if exec_c:
                    exec_c = False                    # -c payload of the -exec'd shell: runs locally,
                    if _violates_core(w_tok, depth + 1):
                        return True, case_pattern     # rescan it with the same rules, one level deeper
                elif exec_su_user:
                    exec_su_user = False              # `su postgres -c ...`: user name argument
                elif base in (";", "+"):
                    exec_args = False                 # \; / ';' / ";" / + closes the -exec clause
                    exec_shell = None
                    if exec_join is not None:         # R11-C-F1: fire the join rescan at the terminator
                        exec_join_words.pop()         # (drop the just-collected terminator word)
                        joined = " ".join(exec_join_words)
                        exec_join, exec_join_words = None, []
                        if _violates_core(joined, depth + 1):
                            return True, case_pattern
            elif base in ("-exec", "-execdir"):
                exec_cmd = True                       # quote-spliced predicate ('-exec'): same rule
            continue                                  # plain find operand (path / predicate value)
        if tok == "!":
            continue                                  # pipeline-negation reserved word (R4-C-F2); only
                                                     # bare `!` in command position is reachable here —
                                                     # `!word` (history expansion) or an argument `!` sits
                                                     # behind a real command word, where the scan stopped
        if base in _WRAPPERS:
            prefix, c_flag = base, base in _EVAL      # eval: first arg IS the payload
            su_user = base == "su"
            ssh_host = False                          # a nested wrapper re-arms its own argument slots
            ssh_o_value = False                       # ...and drops any pending ssh -o value state (D7-F5)
            exec_cmd = exec_args = False
            continue
        if base in _BANNED:
            return True, case_pattern
        if base in _SHELLS:
            prefix, c_flag = base, False              # scan its options; -c payload handled below
            exec_cmd = exec_args = False
            continue
        if prefix in _C_PAYLOAD and c_flag:
            # First word after `shell ... -c` / `eval` / `script -c` /
            # `su USER -c`: rescan the whole payload with the same rules
            # (compound commands included), one level deeper.
            return _violates_core(w_tok, depth + 1), False
        if coproc_name:
            # R5-D5-F7: `coproc [NAME] cmd ...` — a non-reserved word right
            # after coproc holds no command slot, skip it and keep scanning
            # (`coproc XX psql` denied; safe-direction over-deny — bash 5.2
            # only takes a NAME before compound commands, see docstring).
            coproc_name = False
            if not (base in _RESERVED and tok == base):
                continue
        if base in _RESERVED and tok == base:
            # R5-C5-F1: bash recognizes a reserved word only as the BARE
            # unquoted word itself — `./case`, `'case'`, `"ca"se`, `\case`,
            # `/usr/bin/case` are ordinary command words: the scan stops
            # there (return below) and the NEXT `;`/`|`-split segment is
            # scanned fresh, instead of entering pattern-skip mode on the
            # normalized form and swallowing the psql segment.
            if base == "case":
                case_pattern = True                   # skip subject + patterns up to the `)` word
            elif base in ("for", "select"):
                for_list = True                       # `for NAME in WORD...` header: word-list mode
            elif base == "coproc":
                coproc_name = True                    # next non-reserved word may be the NAME
            continue                                  # keyword holds a command slot; keep scanning (R4-C-F2)
        if tok.endswith(")") and prev_sep not in ("$", "("):
            continue                                  # case-clause pattern word (`b) psql` after `;;`);
                                                     # NOT for $()-substitution segments (`date)` in
                                                     # `echo $(date) psql` — later words stay arguments)
        if _stdin_fed_violates():
            return True, case_pattern                 # R11-D-F1: stdin script executes (e.g. `bash <<< p x`)
        return False, case_pattern                    # ordinary command; later tokens are just arguments
    if (exec_join is not None and exec_args
            and _violates_core(" ".join(exec_join_words), depth + 1)):
        return True, case_pattern                     # R11-C-F1: unterminated join clause — safe side
    if _stdin_fed_violates():
        return True, case_pattern                     # R11-D-F1: bare shell at segment end, stdin-fed
    return False, case_pattern


def _deny_reason(command: str):
    if _ESCAPE_TAIL.search(command):
        return None
    try:
        return DENY_MSG if _violates_core(command) else None
    except _TooDeep:
        return _DEEP_MSG


def violates(command: str) -> bool:
    return _deny_reason(command) is not None


def main() -> int:
    try:
        data = json.load(sys.stdin)
        command = ((data or {}).get("tool_input") or {}).get("command")
        if not isinstance(command, str):
            return 0
        reason = _deny_reason(command)
        if reason is None:
            return 0
        print(reason, file=sys.stderr)
        return 2
    except Exception:                                  # noqa: BLE001 -- fail-open
        return 0


if __name__ == "__main__":
    sys.exit(main())
