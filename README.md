# claude-code-stats

**What did this repo cost to build with Claude Code?**

A [Claude Code](https://claude.com/claude-code) skill that reads the session
transcripts already on your disk and tells you: how many tokens went into a
project, how many hours you actually spent, and what the same work would have
cost on the metered API.

No API calls, no telemetry, no account access. It parses the JSONL files Claude
Code already writes to `~/.claude/projects/`.

```
📊 Session stats — my-project
_18 transcripts (incl. subagents) · 12,405 records_

Tokens — 1.42B total
- Output (generated): 4.1M
- Cache read: 1.39B (98%)
- Cache write: 24M · Input: 0.6M

Activity
- Human prompts: 231
- Assistant turns: 3,904

Time (idle >5m excluded)
- Active: 14h 22m — Claude working 9h 05m · you prompting 5h 17m
- Span: 2026-03-02 → 2026-05-19

Cost (metered-API equivalent, Opus 5 rates)
- $912.40 — cache read $695 · cache write $150 · output $65 · input $3
- Caching saved ~$6,250 vs. uncached
```

*(Illustrative output. Your numbers will differ.)*

## Why

Two questions come up constantly and are annoying to answer honestly:

1. **"How much did AI actually contribute to this project?"** Commit counts lie —
   they do not capture the conversation. Token counts and active hours do.
2. **"What would this have cost on the API?"** On a flat Claude Code
   subscription you never see a bill, so the leverage is invisible. This prints
   the metered equivalent.

The cost figure is deliberately framed as *"what this would have cost
pay-as-you-go"* — not what you were charged.

## Install

```bash
git clone https://github.com/jimzucker/claude-code-stats.git
mkdir -p ~/.claude/skills
ln -s "$(pwd)/claude-code-stats" ~/.claude/skills/stats
```

Symlinking (rather than copying) is the point: edits are version-controlled in
place, so the skill cannot quietly drift from the repo. This tool exists because
four copies of it in four repos had already drifted apart.

Verify:

```bash
cd ~/your-project
python3 ~/.claude/skills/stats/session_stats.py
```

Claude Code picks the skill up automatically. You can also just ask *"how much
did this repo cost to build?"* or wire it to a `/stats` command.

## Usage

Run it **from the repo you want to measure** — it resolves the target with
`git rev-parse --show-toplevel`, so one installed copy serves every repo.

```bash
python3 ~/.claude/skills/stats/session_stats.py              # this repo
python3 ~/.claude/skills/stats/session_stats.py --md         # + Markdown report
python3 ~/.claude/skills/stats/session_stats.py --idle 10    # 10-min idle cutoff
python3 ~/.claude/skills/stats/session_stats.py FILE.jsonl   # one transcript
```

| Flag | Effect |
|---|---|
| `--md [PATH]` | Write a Markdown report (default `<repo>/docs/SESSION_STATS.md`) |
| `--also REPO` | Fold in another project dir — for renamed repos |
| `--idle N` | Idle-gap threshold in minutes (default 5) |
| `--rate-input` / `--rate-output` / `--rate-cache-write` / `--rate-cache-read` | Per-MTok rate overrides |

### Renamed repos

Claude Code keys transcripts by **path**, so renaming or moving a repo orphans
everything recorded under the old name. Fold it back in:

```bash
# all three forms work
--also old-name
--also ~/code/old-name
--also=-Users-me-code-old-name     # encoded form needs the '=' (leading dash)
```

List what Claude Code actually has with `ls ~/.claude/projects/`.

## How it works

Claude Code writes one JSONL file per session under
`~/.claude/projects/<repo-path-with-/-replaced-by-->/`, plus
`<session>/subagents/*.jsonl` for subagent runs. The script reads all of them.

- **Tokens** — summed from each assistant record's `message.usage`:
  `output_tokens`, `input_tokens`, `cache_creation_input_tokens`,
  `cache_read_input_tokens`. These are reported by the API, not estimated.
- **Prompts** — `user` records that are genuine human messages, excluding
  tool results, meta records, and slash-command expansions.
- **Time** — records sorted by timestamp; gaps over the idle threshold are
  excluded entirely. A gap ending in a user event counts as *you prompting*;
  everything else counts as *Claude working*.
- **Cost** — tokens ÷ 1e6 × rate, per token class.

### Rates

Defaults are Anthropic's published **Opus 5 / Opus 4.8** rates — input $5,
output $25, 5-min cache write $6.25, cache read $0.50 per MTok. Those two are
priced identically, as is the whole Opus 4.5–5 line.

Cache reads are typically **95–98% of all tokens**, so `--rate-cache-read` is
the constant that actually moves the total. Check the
[current pricing](https://platform.claude.com/docs/en/about-claude/pricing)
before quoting a number for a different model — note Fable 5.1 prices cache
reads at 0.025× input rather than the usual 0.1×.

## Accuracy, honestly

Worth knowing before you put a number in a slide:

- **Token counts are exact.** They come from the API's own `usage` field.
- **Active time is an approximation.** "You prompting" is measured as the gap
  before each of your messages, so it bundles your thinking time with the time
  you spent reading the previous response. The 5-minute idle cutoff is a
  heuristic; try `--idle 10` and see how much the total moves.
- **The cost is a counterfactual**, not a receipt. It is what the same token
  volume would cost at list price — ignoring subscription pricing, batch
  discounts, and the fact that you would likely have worked differently if
  every message had a price tag.
- **Transcripts can disappear.** They are local files with no guarantee of
  permanence. If a project's history matters to you, run `--md` and commit the
  report — the report survives even when the transcripts do not.

## Requirements

Python 3.9+. No third-party packages.

## License

Apache-2.0 — see [LICENSE](LICENSE).
