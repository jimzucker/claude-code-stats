---
name: stats
description: Use when the user asks how much a repo's Claude Code build cost, how many tokens or hours went into it, or invokes /stats. Runs the bundled session_stats.py against the local transcripts for the current repo and summarizes tokens, prompts/turns, active time, and the metered-API-equivalent cost.
---

# Claude Code build-session stats

Reports tokens, prompt/turn counts, active vs. idle time, and the
metered-API-equivalent cost for the **current repository**, by parsing the JSONL
transcripts Claude Code writes under `~/.claude/projects/`.

The script is self-contained in this directory, so it works from any project and
any git branch with nothing checked into the repo being measured.

## How to invoke

Run it **with the target repo as the working directory** — the repo is resolved
from `git rev-parse --show-toplevel`, not from where the script lives:

```bash
python3 ~/.claude/skills/stats/session_stats.py
```

Then present the summary it prints. **The script is the single source of truth —
never hand-calculate these stats.**

Useful flags:

| Flag | Effect |
|---|---|
| `--md [PATH]` | Also write the Markdown report (default `<repo>/docs/SESSION_STATS.md`) |
| `--also REPO` | Fold in another project dir (see *Renamed repos* below) |
| `--idle N` | Idle-gap threshold in minutes (default 5) |
| `FILE.jsonl` | Score one specific transcript instead of the whole repo |
| `--rate-output` / `--rate-input` / `--rate-cache-write` / `--rate-cache-read` | Override the per-MTok rates |

## Renamed repos

Claude Code keys transcripts by path, so sessions from before a rename live
under the old encoded name and are invisible to a plain run. Fold them in with
`--also`, which accepts whichever form is handiest:

```bash
python3 ~/.claude/skills/stats/session_stats.py --also old-repo-name
python3 ~/.claude/skills/stats/session_stats.py --also ~/code/old-repo-name
python3 ~/.claude/skills/stats/session_stats.py --also=-Users-me-code-old-repo-name
```

The encoded form is the repo's absolute path with every `/` replaced by `-`.
List what exists with `ls ~/.claude/projects/`. If you pass the encoded form,
**write it as `--also=VALUE`** — it starts with `-`, which argparse otherwise
reads as the next flag.

An empty dir under `~/.claude/projects/` means that history is gone, not that
the flag failed. The script's "no transcripts found" message names every
directory it searched and flags any that do not exist.

## Rates

The built-in rates are Anthropic's official **Opus 5 / Opus 4.8** figures —
input $5, output $25, 5-min cache write $6.25, cache read $0.50 per MTok. Those
two models are priced identically, and the whole Opus 4.5–5 line shares these
numbers, so no override is needed across that family.

Override for a genuinely different model:

| Model | Override |
|---|---|
| Sonnet 5 | `--rate-input 2 --rate-output 10 --rate-cache-write 2.50 --rate-cache-read 0.20` |
| Haiku 4.5 | `--rate-input 1 --rate-output 5 --rate-cache-write 1.25 --rate-cache-read 0.10` |

Cache reads dominate the total in almost every real build, so that constant is
the one that moves the number. If the user asks about a model not listed here,
fetch `platform.claude.com/docs/en/about-claude/pricing.md` rather than
recalling the rates.

## Reporting

Present tokens, prompts/turns, active time, and estimated cost. Note that the
cost is the **metered-API equivalent** — on a flat Claude Code subscription the
build is effectively included.

## Fallback

If the script fails or the repo has no transcripts, the raw data is the JSONL
under `~/.claude/projects/<encoded-repo-path>/` — sum each `assistant` record's
`message.usage` (`output_tokens`, `input_tokens`, `cache_creation_input_tokens`,
`cache_read_input_tokens`), count real human `user` records for prompts, and
treat gaps > 5 min as idle. Prefer fixing the script over hand-parsing.
