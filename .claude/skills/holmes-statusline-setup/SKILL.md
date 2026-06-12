---
name: holmes-statusline-setup
description: >
  Install Holmes's preferred two-line Claude Code status line — directory + git branch on line 1,
  model name + context usage bar + input/output tokens + 5h/7d session usage on line 2. Writes
  ~/.claude/statusline.sh (pure bash + python3, no jq dependency) and registers it in
  ~/.claude/settings.json. Use this skill when the user asks to set up, restore, fix, or customize
  the Claude Code status line. Trigger on: "상태바 설정", "스테이터스바", "statusline 설정",
  "statusline setup", "하단 상태바", "클로드코드 상태바", "statusline 복구", "statusline 고쳐",
  "상태바 추가", or when the user wants to show directory/branch/model/context/rate-limits in the
  Claude Code chat footer.
---

# Holmes Status Line Setup

Install a two-line compact status line for Claude Code that renders as:

```
 compan.io on holmes
  Claude Opus 4.6 (1M context) [███████░░░] 69% | in:0.0k out:1.3k | 5h:5% 7d:73%
```

Line 1: current directory basename (cyan) + dim " on " + git branch (green).
Line 2: model name + context window label + 10-char block bar + used % + effective input/output tokens in k + 5-hour and 7-day session usage %.

## Design Notes

- **No `jq` dependency.** Uses `python3` (always present on Linux/macOS) for JSON parsing. An earlier version relied on `jq` and broke on hosts without it.
- **Branch lookup uses `git -C "$current_dir"`** so the script never changes its working directory — no lock contention, no cwd side effects.
- **Context usage** reads `context_window.used_percentage` from the statusline JSON first. If absent (first turn of a session), falls back to walking the transcript JSONL and picking the last `usage` object.
- **Token display** sums `input_tokens + cache_read_input_tokens + cache_creation_input_tokens` as effective input (all three contribute to context pressure), and shows `output_tokens` separately.
- **Context label**: ≥ 900k → `1M context`; ≥ 200k → `200k context`; else raw.
- **5h / 7d rate limits**: read from `rate_limits.five_hour.used_percentage` / `seven_day.used_percentage` if Claude Code populates them (subscription users, after first API response). Otherwise try `bunx ccusage@latest statusline` then `npx --yes ccusage@latest statusline`. Falls back to `--` if neither is available.

## Step 1: Write ~/.claude/statusline.sh

Create the script with the Write tool:

```bash
#!/usr/bin/env bash
# ~/.claude/statusline.sh
# Claude Code status line — two-line compact display
# Uses python3 for JSON parsing (no jq dependency).

input=$(cat)

# ── ANSI colors ──────────────────────────────────────────────────────────────
RESET=$'\033[0m'
DIM=$'\033[2m'
CYAN=$'\033[36m'
GREEN=$'\033[32m'

# ── Parse all needed fields from JSON in one python call ─────────────────────
parsed=$(python3 -c '
import sys, json
try:
    d = json.loads(sys.stdin.read())
except Exception:
    d = {}

def g(obj, *keys, default=""):
    for k in keys:
        if not isinstance(obj, dict):
            return default
        obj = obj.get(k)
        if obj is None:
            return default
    return obj

current_dir  = g(d, "workspace", "current_dir") or g(d, "cwd") or ""
model_name   = g(d, "model", "display_name") or "Unknown"
transcript   = g(d, "transcript_path") or ""

cw           = d.get("context_window") or {}
used_pct_raw = cw.get("used_percentage")
ctx_size     = cw.get("context_window_size") or 200000
usage        = cw.get("current_usage") or {}
in_tokens    = usage.get("input_tokens") or 0
cache_read   = usage.get("cache_read_input_tokens") or 0
cache_write  = usage.get("cache_creation_input_tokens") or 0
out_tokens   = usage.get("output_tokens") or 0

rl           = d.get("rate_limits") or {}
five_pct     = (rl.get("five_hour") or {}).get("used_percentage")
week_pct     = (rl.get("seven_day") or {}).get("used_percentage")

used_pct = "" if used_pct_raw is None else str(used_pct_raw)
five_pct = "" if five_pct is None else str(five_pct)
week_pct = "" if week_pct is None else str(week_pct)

print(current_dir)
print(model_name)
print(transcript)
print(used_pct)
print(in_tokens)
print(cache_read)
print(cache_write)
print(out_tokens)
print(ctx_size)
print(five_pct)
print(week_pct)
' <<< "$input")

{
  IFS= read -r current_dir
  IFS= read -r model_name
  IFS= read -r transcript_path
  IFS= read -r used_pct
  IFS= read -r in_tokens
  IFS= read -r cache_read
  IFS= read -r cache_write
  IFS= read -r out_tokens
  IFS= read -r ctx_size
  IFS= read -r five_pct
  IFS= read -r week_pct
} <<< "$parsed"

in_tokens=${in_tokens:-0}
cache_read=${cache_read:-0}
cache_write=${cache_write:-0}
out_tokens=${out_tokens:-0}
ctx_size=${ctx_size:-200000}

# ── Line 1: directory basename + git branch ──────────────────────────────────
dir_base=""
if [ -n "$current_dir" ]; then
  dir_base=$(basename "$current_dir")
fi

git_branch=""
if [ -n "$current_dir" ] && [ -d "$current_dir" ]; then
  git_branch=$(git -C "$current_dir" branch --show-current 2>/dev/null)
fi

if [ -n "$git_branch" ]; then
  line1=" ${CYAN}${dir_base}${RESET}${DIM} on ${RESET}${GREEN}${git_branch}${RESET}"
else
  line1=" ${CYAN}${dir_base}${RESET}"
fi

# ── Context window fallback: parse transcript if used_pct missing ────────────
if [ -z "$used_pct" ]; then
  if [ -n "$transcript_path" ] && [ -f "$transcript_path" ]; then
    read -r t_in t_cr t_cw t_out < <(python3 -c '
import sys, json
path = sys.argv[1]
last = None
try:
    with open(path, "r", errors="ignore") as f:
        for line in f:
            try:
                j = json.loads(line)
            except Exception:
                continue
            stack = [j]
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    if "usage" in node and isinstance(node["usage"], dict):
                        last = node["usage"]
                    for v in node.values():
                        if isinstance(v, (dict, list)):
                            stack.append(v)
                elif isinstance(node, list):
                    stack.extend(node)
except Exception:
    pass
if not last:
    print("0 0 0 0")
else:
    print(last.get("input_tokens") or 0,
          last.get("cache_read_input_tokens") or 0,
          last.get("cache_creation_input_tokens") or 0,
          last.get("output_tokens") or 0)
' "$transcript_path")
    in_tokens=${t_in:-0}
    cache_read=${t_cr:-0}
    cache_write=${t_cw:-0}
    out_tokens=${t_out:-0}
    total_used=$(( in_tokens + cache_read + cache_write + out_tokens ))
    if [ "$ctx_size" -gt 0 ] 2>/dev/null; then
      used_pct=$(awk "BEGIN { printf \"%.0f\", ($total_used / $ctx_size) * 100 }")
    fi
  fi
fi

if [ -z "$used_pct" ]; then
  used_pct=0
fi
used_pct_int=$(awk "BEGIN { printf \"%.0f\", $used_pct + 0 }")

# ── Block bar (10 chars) ─────────────────────────────────────────────────────
filled=$(awk "BEGIN { x = int($used_pct_int / 10 + 0.5); if (x > 10) x = 10; if (x < 0) x = 0; print x }")
empty=$(( 10 - filled ))
bar="["
for (( i=0; i<filled; i++ )); do bar+="█"; done
for (( i=0; i<empty;  i++ )); do bar+="░"; done
bar+="]"

# ── Token counts in k ────────────────────────────────────────────────────────
eff_in=$(( in_tokens + cache_read + cache_write ))
in_k=$(awk "BEGIN { printf \"%.1f\", $eff_in / 1000 }")
out_k=$(awk "BEGIN { printf \"%.1f\", $out_tokens / 1000 }")

# ── Context window size label ────────────────────────────────────────────────
if [ "$ctx_size" -ge 900000 ] 2>/dev/null; then
  ctx_label="1M context"
elif [ "$ctx_size" -ge 200000 ] 2>/dev/null; then
  ctx_label="200k context"
else
  ctx_label="${ctx_size} context"
fi

# ── Rate limits: try JSON fields, then ccusage, else placeholder ─────────────
if [ -z "$five_pct" ] && [ -z "$week_pct" ]; then
  ccusage_out=""
  if command -v bunx &>/dev/null; then
    ccusage_out=$(bunx ccusage@latest statusline 2>/dev/null)
  fi
  if [ -z "$ccusage_out" ] && command -v npx &>/dev/null; then
    ccusage_out=$(npx --yes ccusage@latest statusline 2>/dev/null)
  fi
  if [ -n "$ccusage_out" ]; then
    read -r five_pct week_pct < <(python3 -c '
import sys, json
try:
    d = json.loads(sys.stdin.read())
except Exception:
    d = {}
fh = (d.get("five_hour") or {}).get("used_percentage")
sd = (d.get("seven_day") or {}).get("used_percentage")
print("" if fh is None else fh, "" if sd is None else sd)
' <<< "$ccusage_out")
  fi
fi

if [ -n "$five_pct" ]; then
  five_str=$(awk "BEGIN { printf \"%.0f%%\", $five_pct + 0 }")
else
  five_str="--"
fi

if [ -n "$week_pct" ]; then
  week_str=$(awk "BEGIN { printf \"%.0f%%\", $week_pct + 0 }")
else
  week_str="--"
fi

# ── Assemble line 2 ──────────────────────────────────────────────────────────
line2="  ${model_name} (${ctx_label}) ${bar} ${used_pct_int}% ${DIM}|${RESET} in:${in_k}k out:${out_k}k ${DIM}|${RESET} 5h:${five_str} 7d:${week_str}"

printf "%s\n%s\n" "$line1" "$line2"
```

After writing, make it executable:

```bash
chmod +x ~/.claude/statusline.sh
```

## Step 2: Register in ~/.claude/settings.json

Read `~/.claude/settings.json` first (do not overwrite other keys), then add or replace the `statusLine` block:

```json
"statusLine": {
  "type": "command",
  "command": "bash ~/.claude/statusline.sh",
  "padding": 0
}
```

Use the Edit tool to merge this into the existing JSON — do not clobber unrelated keys.

## Step 3: Smoke Test (Optional)

Confirm the script runs without errors using a fake statusline JSON:

```bash
echo '{"workspace":{"current_dir":"'"$PWD"'"},"model":{"display_name":"Claude Opus 4.6"},"context_window":{"used_percentage":42,"context_window_size":1000000,"current_usage":{"input_tokens":100,"cache_read_input_tokens":0,"cache_creation_input_tokens":0,"output_tokens":500}}}' | bash ~/.claude/statusline.sh
```

Expected: two-line output with the current directory basename, current git branch, and `42%` bar.

## Step 4: Apply

The status line takes effect on the next Claude Code prompt — no restart required. Tell the user:
- `5h:` and `7d:` will show `--` until Claude Code populates `rate_limits` (after first API response this session) or `ccusage` is installed globally (`bun add -g ccusage` or `npm i -g ccusage`).
- Context % shows `0%` on the very first turn before any usage data is available.

## Customization Points

When the user asks for tweaks, these are the knobs:

| Change | Where |
|--------|-------|
| Colors | `CYAN`, `GREEN`, `DIM` at the top of the script |
| Bar length | `filled / empty` loops — change `/ 10`, `10 - filled`, and loop bounds |
| Bar glyphs | `█` and `░` characters in the loop bodies |
| Field order on line 2 | The final `line2=` assembly |
| Hide 5h/7d | Drop the trailing `| 5h:... 7d:...` segment from `line2` |
| Show full path instead of basename | Replace `dir_base=$(basename "$current_dir")` with `dir_base="$current_dir"` |
