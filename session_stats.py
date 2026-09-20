#!/usr/bin/env python3
# Copyright 2026 Jim Zucker
# SPDX-License-Identifier: Apache-2.0
"""Summarize Claude Code session transcripts for this repo.

Reports token usage, prompt/turn counts, and active vs. idle time by parsing
the JSONL transcript(s) Claude Code stores under
``~/.claude/projects/<encoded-repo-path>/``.

Portable: the repo is resolved from the current directory (git toplevel), so a
single central copy — ``~/.claude/skills/stats/session_stats.py``, used by the
``stats`` skill — serves every repo. Run it with the target repo as the CWD. A
legacy per-repo ``scripts/session_stats.py`` copy still works unchanged.

Scope: the repo's own dir plus each session's ``subagents/*.jsonl`` and any
pre-rename ALIAS_PROJECT_DIRS (e.g. iyield -> TrueYield), so the totals cover the
whole project history, not just the current path.

Usage:
    python3 scripts/session_stats.py            # all transcripts for this repo
    python3 scripts/session_stats.py FILE.jsonl # a specific transcript
    python3 scripts/session_stats.py --also DIR # fold in another project dir
    python3 scripts/session_stats.py --idle 10  # idle gap threshold in minutes
    python3 scripts/session_stats.py --md       # write the Markdown report
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import pathlib
import re
import os
import subprocess
import sys

# Gaps longer than this (minutes) count as idle, not active work.
DEFAULT_IDLE_MIN = 5

# Prior encoded project-dir names to fold in alongside this repo's own dir, so
# the stats span the whole project history rather than just the current path.
# Set this only when a repo was renamed mid-build (Claude Code keys transcripts
# by path, so pre-rename sessions live under the old encoded name); leave empty
# ([]) for a repo that kept its name. Override at runtime with --also NAME.
ALIAS_PROJECT_DIRS = []

# Claude Opus 5 / Opus 4.8 standard API rates, USD per million tokens. Source:
# Anthropic's official pricing docs, platform.claude.com/docs/en/about-claude/
# pricing (re-verified 2026-09-20). Opus 5 is priced identically to 4.8 — the
# whole Opus 4.5-5 line is $5/$25 with $6.25/$0.50 cache, so these constants
# need no change when moving between them. Opus dropped 3x from the old 4.x
# $15/$75 starting with 4.5. The total is dominated by the cache-read rate, so that
# one matters most. Override with --rate-* for other models (e.g. Sonnet 4.6
# is $3/$15, cache read $0.30; Haiku 4.5 is $1/$5, cache read $0.10).
RATES = {
    "output": 25.0,      # base output
    "input": 5.0,        # base (uncached) input
    "cache_write": 6.25, # 5-min cache write = 1.25x input (1h write is $10)
    "cache_read": 0.50,  # cache hit/refresh = 0.10x input
}

# Loaded cost of one engineer-day, USD — used only for the ROI comparison.
DAY_RATE = 800


def repo_root() -> str:
    """The repo the stats are *for* — resolved from the current directory, not
    from where this script lives, so one central copy (e.g. under
    ~/.claude/skills/stats/) serves every repo. Falls back to the script's own
    parent for a legacy in-repo ``scripts/session_stats.py`` install run from
    somewhere else entirely."""
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        if top:
            return os.path.abspath(top)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    here = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if os.path.basename(os.path.dirname(os.path.abspath(__file__))) == "scripts":
        return here
    return os.path.abspath(os.getcwd())


def project_name() -> str:
    return os.path.basename(repo_root())


def transcript_dir_for(repo_path: str) -> str:
    """Claude Code encodes the repo path by replacing '/' with '-'."""
    encoded = repo_path.replace("/", "-")
    return os.path.expanduser(f"~/.claude/projects/{encoded}")


def transcripts_in(d: str) -> list[str]:
    """Every transcript under one project dir: the top-level session files plus
    the per-session ``subagents/*.jsonl`` (sub-agent runs count toward the
    build's tokens/time too)."""
    return (glob.glob(os.path.join(d, "*.jsonl"))
            + glob.glob(os.path.join(d, "*", "subagents", "*.jsonl")))


def encode_alias(value: str) -> str:
    """Normalize one --also value to an encoded project-dir name.

    Accepts whichever form is handiest, because the encoded form starts with
    '-' and argparse rejects that as a missing argument unless it is written
    --also=VALUE:

      iyield                     bare name, resolved beside this repo
      ~/code/GitHub/iyield       a path (also absolute, or a relative one)
      -Users-me-code-iyield      an already-encoded dir name
    """
    v = value.strip()
    if v.startswith("-"):
        return v                                    # already encoded
    v = os.path.expanduser(v)
    if os.path.sep in v:                            # a path
        return os.path.abspath(v).replace("/", "-")
    sibling = os.path.join(os.path.dirname(repo_root()), v)
    return os.path.abspath(sibling).replace("/", "-")


# ---------------------------------------------------------------- baseline --
# Transcripts are local files with no guarantee of permanence: they can be
# pruned, lost with a machine, or orphaned by renaming the repo. A baseline is
# a small JSON file COMMITTED TO THE REPO that freezes totals already observed,
# so a later run reports the whole build even when the underlying transcripts
# are gone. Live transcripts that the baseline already covers are skipped, so
# nothing is double counted.
BASELINE_NAME = os.path.join("docs", "session-stats-baseline.json")


def baseline_path(repo: str, explicit: str | None) -> str:
    return explicit or os.path.join(repo, BASELINE_NAME)


def load_baseline(path: str) -> dict | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as fh:
            b = json.load(fh)
    except (OSError, ValueError) as e:
        sys.exit(f"Baseline {path} is unreadable: {e}")
    if "covers_through" not in b:
        sys.exit(f"Baseline {path} has no 'covers_through' — refusing to guess.")
    return b


def baseline_cutoff(b: dict):
    """The baseline's cutoff as an aware datetime.

    A cutoff imported from a Markdown report has no UTC offset (the report
    prints a local wall-clock time), while transcript timestamps are aware —
    comparing the two raises. Assume local time for a naive value.
    """
    t = dt.datetime.fromisoformat(b["covers_through"])
    if t.tzinfo is None:
        t = t.astimezone()
    return t


def save_baseline(path: str, b: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(b, fh, indent=2, sort_keys=True)
        fh.write("\n")


def _num(text: str, pattern: str, default=0) -> int:
    m = re.search(pattern, text)
    return int(m.group(1).replace(",", "")) if m else default


def import_baseline_from_md(md_path: str, project: str) -> dict:
    """Seed a baseline from an already-committed SESSION_STATS.md.

    This is the rescue path: the committed report may be the only surviving
    record of a build whose transcripts are gone.
    """
    text = pathlib.Path(md_path).read_text()
    end = re.search(r"\| Session end \| ([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:]+)", text)
    start = re.search(r"\| Session start \| ([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:]+)", text)
    if not end:
        sys.exit(f"Could not find a 'Session end' row in {md_path}; "
                 "cannot tell what period this report covers.")

    def secs(label: str) -> int:
        m = re.search(label + r" \| (?:~)?(?:(\d+)h )?(?:(\d+)m )?(?:(\d+)s)?", text)
        if not m:
            return 0
        h, mi, se = (int(g or 0) for g in m.groups())
        return h * 3600 + mi * 60 + se

    return {
        "note": ("Frozen totals for the period below, imported from a committed "
                 "report. Live transcripts ending at or before 'covers_through' "
                 "are skipped so nothing is counted twice."),
        "project": project,
        "source": os.path.relpath(md_path),
        "imported_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "span_start": start.group(1) if start else None,
        "covers_through": end.group(1).replace(" ", "T"),
        "records": _num(text, r"\(([\d,]+) records"),
        "output_tokens": _num(text, r"\| Output \(generated\) \| ([\d,]+)"),
        "input_tokens": _num(text, r"\| Input \(uncached\) \| ([\d,]+)"),
        "cache_creation_input_tokens": _num(text, r"\| Cache write \| ([\d,]+)"),
        "cache_read_input_tokens": _num(text, r"\| Cache read \| ([\d,]+)"),
        "prompts": _num(text, r"\*\*~?([\d,]+) prompts you typed"),
        "assistant_turns": _num(text, r"\*\*([\d,]+) assistant turns\*\*"),
        "active_seconds": secs(r"\| Active \(gaps <5m\)"),
        "claude_seconds": secs(r"Claude working"),
        "user_seconds": secs(r"User prompting"),
        "idle_seconds": secs(r"\| Idle \(excluded\)"),
    }


def find_transcripts(args_paths: list[str], also: list[str]) -> list[str]:
    if args_paths:
        return args_paths
    base = os.path.dirname(transcript_dir_for(repo_root()))  # ~/.claude/projects
    dirs = [transcript_dir_for(repo_root())]
    dirs += [os.path.join(base, encode_alias(a))
             for a in (*ALIAS_PROJECT_DIRS, *also)]
    files: list[str] = []
    per_dir: list[tuple[str, int]] = []
    for d in dirs:
        found = transcripts_in(d)
        per_dir.append((d, len(found)))
        files.extend(found)
    files = sorted(set(files))
    if not files:
        # Say which dirs were searched and why each came up empty, rather than
        # naming only the first — an empty dir and a misspelled one look the
        # same from the caller's side otherwise.
        report = "\n".join(
            f"  {n:>4} transcripts  {d}"
            + ("" if os.path.isdir(d) else "   (no such dir)")
            for d, n in per_dir
        )
        sys.exit(
            "No transcripts found. Searched:\n" + report
            + "\n\nEncoded dir names are the repo path with '/' -> '-'; "
              "list what exists with:  ls ~/.claude/projects/"
        )
    return files


def load(path: str) -> list[dict]:
    rows = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def parse_ts(r: dict):
    t = r.get("timestamp")
    if not t:
        return None
    try:
        return dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_human_prompt(r: dict) -> bool:
    """A message the user actually typed (not a tool result / slash-command)."""
    if r.get("type") != "user" or r.get("isMeta"):
        return False
    content = r.get("message", {}).get("content")
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                return False
        text = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    elif isinstance(content, str):
        text = content
    else:
        return False
    if not text.strip():
        return False
    markers = ("<local-command", "<command-name>", "command-message")
    if any(m in text for m in markers):
        return False
    if "Caveat:" in text and len(text) < 400:
        return False
    return True


def hms(td: dt.timedelta) -> str:
    s = int(td.total_seconds())
    return f"{s // 3600}h {s % 3600 // 60}m {s % 60}s"


def costs(outp, inp, cc, cr, rates):
    """Return per-category USD cost plus the no-cache counterfactual."""
    c = {
        "output": outp / 1e6 * rates["output"],
        "input": inp / 1e6 * rates["input"],
        "cache_write": cc / 1e6 * rates["cache_write"],
        "cache_read": cr / 1e6 * rates["cache_read"],
    }
    c["total"] = sum(c.values())
    # If cache reads had been billed as normal input tokens.
    c["nocache_reads"] = cr / 1e6 * rates["input"]
    c["cache_savings"] = c["nocache_reads"] - c["cache_read"]
    return c


def write_markdown(path, project, files, n_rows, outp, inp, cc, cr, total,
                   prompts, turns, events, active, claude, user, idle, idle_min,
                   n_user_records, rates) -> None:
    """Write the full narrative report — the story, not just the tables."""
    cr_m = cr / 1e6
    out_m = outp / 1e6
    total_m = total / 1e6
    c = costs(outp, inp, cc, cr, rates)
    lines = [
        "# Build session story",
        "",
        f"The real numbers behind building **{project}**, pulled from this "
        f"project's Claude Code session transcript ({n_rows:,} records spanning "
        "the whole build). Regenerate with `python3 ~/.claude/skills/stats/session_stats.py --md` (run from the repo root), or the /stats slash command.",
        "",
        "## Tokens",
        "",
        "| Category | Tokens |",
        "| --- | ---: |",
        f"| Output (generated) | {outp:,} |",
        f"| Input (uncached) | {inp:,} |",
        f"| Cache write | {cc:,} |",
        f"| Cache read | {cr:,} |",
        f"| **Grand total** | **~{total_m:.1f} M** |",
        "",
        f"The total is dominated by **cache reads (~{cr_m:.0f} M)** — that's the "
        "growing conversation/codebase context being re-read on each of "
        f"{turns:,} assistant turns, billed at the cheap cached rate, not "
        f"{cr_m:.0f} M of fresh work. The number that reflects actual **produced "
        f"content is ~{out_m:.2f} M output tokens**.",
        "",
        "## Prompts",
        "",
        f"- **~{prompts} prompts you typed** (out of {n_user_records:,} raw "
        '"user" records — the rest are tool results, slash-command stdout, and '
        "system reminders).",
        f"- **{turns:,} assistant turns** in response (each prompt fans out into "
        "many tool-call/reasoning steps).",
    ]
    if events:
        start = events[0].astimezone()
        end = events[-1].astimezone()
        idle_h = idle.total_seconds() / 3600
        lines += [
            "",
            "## Time",
            "",
            f"Session ran **{start:%b %-d %-I:%M %p} → {end:%b %-d %-I:%M %p %Z}** "
            f"wall-clock, but that includes **~{idle_h:.0f} h of idle gaps** "
            f"(overnight, breaks). Counting only stretches with <{idle_min:g}-min "
            "gaps between events:",
            "",
            f"- **Active session: ~{hms(active)}**",
            f"  - **Claude working** (tool calls, builds, tests, writing): "
            f"~{hms(claude)}",
            f"  - **You prompting / reading**: ~{hms(user)}",
            "",
            "| Metric | Value |",
            "| --- | --- |",
            f"| Session start | {start:%Y-%m-%d %H:%M %Z} |",
            f"| Session end | {end:%Y-%m-%d %H:%M %Z} |",
            f"| Active (gaps <{idle_min:g}m) | {hms(active)} |",
            f"| &nbsp;&nbsp;Claude working | {hms(claude)} |",
            f"| &nbsp;&nbsp;User prompting | {hms(user)} |",
            f"| Idle (excluded) | {hms(idle)} |",
        ]
    lines += [
        "",
        "## Cost",
        "",
        "What this would cost on the **metered Claude API**, at Anthropic's "
        "official **Opus 5 / Opus 4.8** rates (USD per million tokens — input $5, "
        "output $25, 5-min cache write $6.25, cache read $0.50; "
        "[source](https://platform.claude.com/docs/en/about-claude/pricing), "
        "verified 2026-06). The total scales linearly with the cache-read rate, "
        "which dominates; override with `--rate-*` for other models.",
        "",
        "| Token type | Rate / M | Tokens | Cost |",
        "| --- | ---: | ---: | ---: |",
        f"| Output | ${rates['output']:.2f} | {outp:,} | ${c['output']:,.2f} |",
        f"| Input (uncached) | ${rates['input']:.2f} | {inp:,} | "
        f"${c['input']:,.2f} |",
        f"| Cache write | ${rates['cache_write']:.2f} | {cc:,} | "
        f"${c['cache_write']:,.2f} |",
        f"| Cache read | ${rates['cache_read']:.2f} | {cr:,} | "
        f"${c['cache_read']:,.2f} |",
        f"| **Total** | | | **≈ ${c['total']:,.0f}** |",
        "",
        f"**Prompt caching saved ~${c['cache_savings']:,.0f}.** Those "
        f"{cr_m:.0f} M cache reads, if billed as normal input tokens "
        f"(${rates['input']:.0f}/M), would have been **~${c['nocache_reads']:,.0f}** "
        f"instead of **${c['cache_read']:,.0f}** — a "
        f"{rates['input'] / rates['cache_read']:.0f}× discount, and the single "
        "biggest cost lever.",
        "",
        "**At work this usually isn't metered API.** Most teams run Claude Code "
        "on a flat **subscription** (Max ~$100–200/mo), where this build is "
        f"effectively included; the ~${c['total']:,.0f} above is the equivalent "
        "à-la-carte value, useful for ROI math but not what most orgs pay.",
        "",
        "**ROI framing.** A project of this scope — built from scratch with "
        "tests and CI — is realistically **3–10 engineer-days**. At "
        f"~${DAY_RATE:,}/loaded-day that's **${3 * DAY_RATE:,}–${10 * DAY_RATE:,}** "
        f"of labor, so even the metered ~${c['total']:,.0f} (or a month of "
        f"subscription) is roughly **{3 * DAY_RATE / c['total']:.0f}–"
        f"{10 * DAY_RATE / c['total']:.0f}× cheaper** than the equivalent "
        "hands-on time.",
        "",
        "## Caveats",
        "",
        f"- All from **{len(files)} transcript "
        f"file{'s' if len(files) != 1 else ''}**, which cover the whole "
        "project — both the pre-compaction work and the continued sessions — not "
        "just any single feature.",
        '- **"User prompting" time is approximated** as the gap before each of '
        "your messages (composing + reading my output), so it bundles your "
        "think-time with reading time.",
        "- **Token counts come straight from the `usage` field** on each "
        "assistant message; **timing from event timestamps**.",
        "- **Costs use Anthropic's official Opus 5 / Opus 4.8 rates** "
        "(identical for both; [pricing docs](https://platform.claude.com/docs/en/about-claude/pricing), "
        "verified 2026-09-20); the labor comparison is a rough industry figure, "
        "not a measured one.",
        "",
    ]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("\n".join(lines))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="*", help="transcript .jsonl file(s)")
    ap.add_argument("--idle", type=float, default=DEFAULT_IDLE_MIN,
                    help=f"idle gap threshold in minutes (default {DEFAULT_IDLE_MIN})")
    ap.add_argument("--md", nargs="?", const="", default=None,
                    metavar="PATH",
                    help="write a Markdown report (default <repo>/docs/SESSION_STATS.md)")
    ap.add_argument("--also", action="append", default=[], metavar="REPO",
                    help="extra encoded project-dir name(s) to fold in, beyond "
                         "this repo's own and ALIAS_PROJECT_DIRS (repeatable)")
    ap.add_argument("--baseline", metavar="PATH", default=None,
                    help=f"baseline JSON to fold in (default <repo>/{BASELINE_NAME})")
    ap.add_argument("--no-baseline", action="store_true",
                    help="ignore the baseline; report only live transcripts")
    ap.add_argument("--import-baseline", metavar="REPORT.md", default=None,
                    help="one-time: seed the baseline from a committed "
                         "SESSION_STATS.md, then exit")
    ap.add_argument("--update-baseline", action="store_true",
                    help="fold everything seen now into the baseline and "
                         "advance its cutoff, then exit (commit the result)")
    ap.add_argument("--force", action="store_true",
                    help="allow --md to overwrite a report covering more than "
                         "this run does")
    ap.add_argument("--rate-output", type=float, default=RATES["output"])
    ap.add_argument("--rate-input", type=float, default=RATES["input"])
    ap.add_argument("--rate-cache-write", type=float, default=RATES["cache_write"])
    ap.add_argument("--rate-cache-read", type=float, default=RATES["cache_read"])
    args = ap.parse_args()
    rates = {
        "output": args.rate_output,
        "input": args.rate_input,
        "cache_write": args.rate_cache_write,
        "cache_read": args.rate_cache_read,
    }

    bpath = baseline_path(repo_root(), args.baseline)

    if args.import_baseline:
        b = import_baseline_from_md(args.import_baseline, project_name())
        save_baseline(bpath, b)
        print(f"Wrote baseline {bpath}")
        print(f"  covers through {b['covers_through']} — "
              f"{b['records']:,} records, {b['output_tokens']:,} output tokens")
        print("  Commit this file; it survives the transcripts.")
        return

    base = None if args.no_baseline else load_baseline(bpath)
    cutoff = baseline_cutoff(base) if base else None

    files = find_transcripts(args.paths, args.also)
    rows: list[dict] = []
    skipped = 0
    for p in files:
        frows = load(p)
        if cutoff is not None and frows:
            last = max((t for t in (parse_ts(r) for r in frows) if t), default=None)
            if last is not None and last <= cutoff:
                skipped += 1        # already inside the baseline period
                continue
        rows.extend(frows)

    inp = outp = cc = cr = 0
    assistant_turns = 0
    for r in rows:
        if r.get("type") != "assistant":
            continue
        assistant_turns += 1
        u = r.get("message", {}).get("usage") or {}
        inp += u.get("input_tokens", 0)
        outp += u.get("output_tokens", 0)
        cc += u.get("cache_creation_input_tokens", 0)
        cr += u.get("cache_read_input_tokens", 0)

    prompts = sum(1 for r in rows if is_human_prompt(r))

    events = sorted((t for t in (parse_ts(r) for r in rows) if t))
    idle_gap = dt.timedelta(minutes=args.idle)
    active = claude = user = idle = dt.timedelta()
    typed = [(parse_ts(r), r.get("type")) for r in rows if parse_ts(r)]
    typed.sort(key=lambda x: x[0])
    for (t0, _), (t1, ty1) in zip(typed, typed[1:]):
        gap = t1 - t0
        if gap > idle_gap:
            idle += gap
        else:
            active += gap
            if ty1 == "user":
                user += gap
            else:
                claude += gap

    live_records = len(rows)
    if base:
        outp += base.get("output_tokens", 0)
        inp += base.get("input_tokens", 0)
        cc += base.get("cache_creation_input_tokens", 0)
        cr += base.get("cache_read_input_tokens", 0)
        prompts += base.get("prompts", 0)
        assistant_turns += base.get("assistant_turns", 0)
        active += dt.timedelta(seconds=base.get("active_seconds", 0))
        claude += dt.timedelta(seconds=base.get("claude_seconds", 0))
        user += dt.timedelta(seconds=base.get("user_seconds", 0))
        idle += dt.timedelta(seconds=base.get("idle_seconds", 0))
        if base.get("span_start"):
            try:
                events = [dt.datetime.fromisoformat(
                    base["span_start"].replace(" ", "T"))] + events
            except ValueError:
                pass

    total = inp + outp + cc + cr
    n_records = live_records + (base.get("records", 0) if base else 0)

    if args.update_baseline:
        newest = max(events) if events else None
        merged = dict(base or {})
        merged.update({
            "note": ("Frozen totals for the period below. Live transcripts "
                     "ending at or before 'covers_through' are skipped so "
                     "nothing is counted twice."),
            "project": project_name(),
            "updated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "covers_through": (newest or dt.datetime.now().astimezone()).isoformat(),
            "span_start": (merged.get("span_start")
                           or (min(events).isoformat() if events else None)),
            "records": n_records,
            "output_tokens": outp, "input_tokens": inp,
            "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr,
            "prompts": prompts, "assistant_turns": assistant_turns,
            "active_seconds": int(active.total_seconds()),
            "claude_seconds": int(claude.total_seconds()),
            "user_seconds": int(user.total_seconds()),
            "idle_seconds": int(idle.total_seconds()),
        })
        save_baseline(bpath, merged)
        print(f"Updated baseline {bpath}")
        print(f"  now covers through {merged['covers_through']} — "
              f"{n_records:,} records, {outp:,} output tokens")
        print("  Commit this file; it survives the transcripts.")
        return

    if args.md == "":
        args.md = os.path.join(repo_root(), "docs", "SESSION_STATS.md")
    if args.md is not None:
        # Never let a thinner run silently replace a fuller record: the report
        # may be the last surviving evidence of transcripts that are now gone.
        if os.path.isfile(args.md) and not args.force:
            prev = _num(pathlib.Path(args.md).read_text(), r"\(([\d,]+) records")
            if prev > n_records:
                sys.exit(
                    f"Refusing to overwrite {args.md}.\n"
                    f"  It records {prev:,} records; this run sees only "
                    f"{n_records:,}.\n"
                    f"  Transcripts have probably been pruned. Preserve the "
                    f"existing numbers with:\n"
                    f"      python3 {os.path.relpath(__file__)} "
                    f"--import-baseline {os.path.relpath(args.md)}\n"
                    f"  then re-run --md. Use --force to overwrite anyway."
                )
        n_user_records = sum(1 for r in rows if r.get("type") == "user")
        write_markdown(args.md, project_name(), files, n_records, outp, inp, cc,
                       cr, total, prompts, assistant_turns, events, active,
                       claude, user, idle, args.idle, n_user_records, rates)
        print(f"Wrote {args.md}")
        return

    # --- friendly summary (identical layout in every repo; relay verbatim) ---
    def mag(n: int) -> str:
        """3 significant figures with a B/M/K suffix, e.g. 6,229,729 -> '6.23M'."""
        for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
            if n >= div:
                v = n / div
                dp = 0 if v >= 100 else (1 if v >= 10 else 2)
                return f"{v:.{dp}f}{suf}"
        return f"{n:,}"

    def hm(td: dt.timedelta) -> str:
        s = int(td.total_seconds())
        return f"{s // 3600}h {s % 3600 // 60:02d}m"

    extras = []
    if any(f"{os.sep}subagents{os.sep}" in f for f in files):
        extras.append("incl. subagents")
    if ALIAS_PROJECT_DIRS or args.also:
        extras.append("pre-rename history")
    if base:
        extras.append("incl. committed baseline")
    note = f" ({', '.join(extras)})" if extras else ""
    n_files = len(files) - skipped

    c = costs(outp, inp, cc, cr, rates)
    cr_pct = f"{cr / total * 100:.0f}%" if total else "0%"
    out = [
        f"📊 Session stats — {project_name()}",
        f"_{n_files} transcripts{note} · {n_records:,} records_",
        "",
        f"Tokens — {mag(total)} total",
        f"- Output (generated): {mag(outp)}",
        f"- Cache read: {mag(cr)} ({cr_pct})",
        f"- Cache write: {mag(cc)} · Input: {mag(inp)}",
        "",
        "Activity",
        f"- Human prompts: {prompts:,}",
        f"- Assistant turns: {assistant_turns:,}",
    ]
    if events:
        s, e = events[0].astimezone(), events[-1].astimezone()
        out += [
            "",
            f"Time (idle >{args.idle:g}m excluded)",
            f"- Active: {hm(active)} — Claude working {hm(claude)} · "
            f"you prompting {hm(user)}",
            f"- Span: {s:%Y-%m-%d} → {e:%Y-%m-%d}",
        ]
    out += [
        "",
        "Cost (metered-API equivalent, Opus 5 / Opus 4.8 rates)",
        f"- ${c['total']:,.2f} — cache read ${c['cache_read']:,.0f} · "
        f"cache write ${c['cache_write']:,.0f} · output ${c['output']:,.0f} · "
        f"input ${c['input']:,.0f}",
        f"- Caching saved ~${c['cache_savings']:,.0f} vs. uncached",
        "",
        "_On a flat Claude Code subscription this build is effectively "
        "included — the figure above is the metered pay-as-you-go equivalent._",
    ]
    print("\n".join(out))


if __name__ == "__main__":
    main()
