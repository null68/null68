import os
import sys
import json
import yaml
import random
import hashlib
import requests
from datetime import datetime, date
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
ROOT_DIR = SCRIPT_DIR.parent

ART_CANVAS_LINES = 22          # vertical slots for art
ART_CANVAS_COLS = 36           # horizontal character slots
FONT_SIZE = 14
LINE_HEIGHT = 19
CHAR_WIDTH = 8.4               # monospace char width at 14px

SVG_W = 835
SVG_H = 460
PAD_TOP = 28
PAD_LEFT = 18
RIGHT_START_X = (ART_CANVAS_COLS + 4) * CHAR_WIDTH + PAD_LEFT + 10
SEP_X = RIGHT_START_X - 12

GLITCH_CHARS = list("!@#$%&*?/|+~^")   # ASCII-only (no unicode block char —
                                          # avoids mojibake if a renderer ever
                                          # mishandles the file's charset)


def load_config():
    with open(ROOT_DIR / "profile.yml") as f:
        return yaml.safe_load(f)


def compute_uptime(birthdate_str):
    bd = datetime.strptime(birthdate_str, "%Y-%m-%d").date()
    today = date.today()
    years = today.year - bd.year
    months = today.month - bd.month
    days = today.day - bd.day
    if days < 0:
        months -= 1
        from calendar import monthrange
        pm = today.month - 1 or 12
        py = today.year if today.month > 1 else today.year - 1
        days += monthrange(py, pm)[1]
    if months < 0:
        years -= 1
        months += 12
    parts = [f"{years} years"]
    if months or days:
        parts.append(f"{months} months")
    if days:
        parts.append(f"{days} days")
    return " ".join(parts)


# ──────────────────────────────────────────────
# GitHub API
# ──────────────────────────────────────────────
GRAPHQL_URL = "https://api.github.com/graphql"
STATS_QUERY = """
query($login: String!) {
  user(login: $login) {
    repositories(first: 100, ownerAffiliations: OWNER, privacy: PUBLIC) {
      totalCount
      nodes { stargazerCount }
    }
    followers { totalCount }
    contributionsCollection {
      totalCommitContributions
      restrictedContributionsCount
      contributionCalendar { totalContributions }
    }
  }
}
"""


def fetch_github_stats(username, token):
    if not token:
        print("WARN: No GITHUB_TOKEN, using fallback stats")
        return None
    headers = {"Authorization": f"bearer {token}", "Content-Type": "application/json"}
    try:
        resp = requests.post(GRAPHQL_URL,
                             json={"query": STATS_QUERY, "variables": {"login": username}},
                             headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            print(f"WARN: GraphQL errors: {data['errors']}")
            return None
        user = data["data"]["user"]
        repos = user["repositories"]
        cc = user["contributionsCollection"]
        return {
            "repos": repos["totalCount"],
            "stars": sum(n["stargazerCount"] for n in repos["nodes"]),
            "followers": user["followers"]["totalCount"],
            "commits": cc["totalCommitContributions"] + cc["restrictedContributionsCount"],
            "contributions": cc["contributionCalendar"]["totalContributions"],
        }
    except Exception as e:
        print(f"WARN: GitHub API failed: {e}")
        return None


def fetch_loc_stats(username, token, cache_file):
    """Sum additions/deletions from the per-repo contributor-stats endpoint.
    Skips forks so vendored/upstream code someone else wrote never counts
    as yours. Each repo is re-queried only when its `pushed_at` has moved
    since the last successful fetch — this endpoint is expensive (GitHub
    computes it async — a 202 means 'try again shortly'), but caching on
    "have we ever fetched this repo" (the old behaviour) means a repo's
    count freezes forever the moment it's first cached, even as you keep
    pushing to it. Keying the cache on last-push time fixes that while
    still avoiding needless re-fetches for untouched repos."""
    cache_path = ROOT_DIR / cache_file
    cache = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
        except Exception:
            cache = {}
    if not token:
        return cache.get("total", 0), cache.get("additions", 0), cache.get("deletions", 0)

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.v3+json"}
    try:
        page, repos = 1, []
        while True:
            r = requests.get(f"https://api.github.com/users/{username}/repos?per_page=100&page={page}&type=owner",
                             headers=headers, timeout=30)
            if r.status_code != 200:
                print(f"WARN: Fetch repos failed with {r.status_code}")
                break
            batch = r.json()
            if not batch:
                break
            repos.extend(batch)
            page += 1

        if not repos and page == 1:
            print("WARN: No repos fetched, returning cached LOC")
            return cache.get("total", 0), cache.get("additions", 0), cache.get("deletions", 0)

        repo_cache = cache.get("repos", {})
        total_add = total_del = 0
        for repo in repos:
            if repo.get("fork"):
                continue  # not your code — skip forked upstream history
            fn = repo["full_name"]
            pushed_at = repo.get("pushed_at")
            cached = repo_cache.get(fn)

            # Trust the cache only if nothing has been pushed since we last counted it
            if cached and cached.get("pushed_at") == pushed_at:
                total_add += cached.get("additions", 0)
                total_del += cached.get("deletions", 0)
                continue

            try:
                r = requests.get(f"https://api.github.com/repos/{fn}/stats/contributors",
                                 headers=headers, timeout=30)
                if r.status_code == 202:
                    print(f"INFO: {fn} stats still computing (202), will retry next run")
                    if cached:  # keep last-known numbers instead of dropping the repo to 0
                        total_add += cached.get("additions", 0)
                        total_del += cached.get("deletions", 0)
                    continue
                if r.status_code == 200 and r.json():
                    matched = False
                    for c in r.json():
                        if c.get("author", {}).get("login", "").lower() == username.lower():
                            a = sum(w.get("a", 0) for w in c.get("weeks", []))
                            d = sum(w.get("d", 0) for w in c.get("weeks", []))
                            total_add += a
                            total_del += d
                            repo_cache[fn] = {"additions": a, "deletions": d, "pushed_at": pushed_at}
                            matched = True
                            break
                    if not matched and cached:
                        total_add += cached.get("additions", 0)
                        total_del += cached.get("deletions", 0)
                elif cached:  # non-200/202 hiccup — don't let it zero the repo out
                    total_add += cached.get("additions", 0)
                    total_del += cached.get("deletions", 0)
            except Exception:
                if cached:
                    total_add += cached.get("additions", 0)
                    total_del += cached.get("deletions", 0)
                continue

        total = total_add + total_del
        cache.update({"total": total, "additions": total_add, "deletions": total_del,
                       "repos": repo_cache, "updated": datetime.now().isoformat()})
        cache_path.write_text(json.dumps(cache, indent=2))
        return total, total_add, total_del
    except Exception as e:
        print(f"WARN: LOC failed: {e}")
        return cache.get("total", 0), cache.get("additions", 0), cache.get("deletions", 0)


# ──────────────────────────────────────────────
# ASCII Art Loading — no distortion
# ──────────────────────────────────────────────
def load_art(art_dir):
    """Load all .txt frames, center on fixed canvas WITHOUT scaling unless
    a frame is wider than the canvas."""
    art_path = ROOT_DIR / art_dir
    frames = []

    for fpath in sorted(art_path.glob("*.txt")):
        with open(fpath) as f:
            raw = f.read().rstrip("\n").split("\n")

        lines = [l.rstrip() for l in raw]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        if not lines:
            continue

        # filename like "01_skull.txt" -> display name "skull"
        stem = fpath.stem
        name = stem.split("_", 1)[1] if "_" in stem else stem
        name = name.replace("_", " ")

        min_left = min((len(l) - len(l.lstrip()) for l in lines if l.strip()), default=0)
        max_right = max((len(l) for l in lines if l.strip()), default=0)
        content_w = max_right - min_left

        trimmed = []
        for l in lines:
            if len(l) > min_left:
                trimmed.append(l[min_left:])
            else:
                trimmed.append("")
        lines = trimmed
        content_w = max((len(l) for l in lines if l.strip()), default=0)

        if content_w > ART_CANVAS_COLS:
            ratio = content_w / ART_CANVAS_COLS
            new_lines = []
            for line in lines:
                new_line = []
                for ci in range(ART_CANVAS_COLS):
                    src = int(ci * ratio)
                    src2 = min(src + 1, len(line) - 1) if src < len(line) - 1 else src
                    if src < len(line) and line[src] != " ":
                        new_line.append(line[src])
                    elif src2 < len(line) and line[src2] != " ":
                        new_line.append(line[src2])
                    else:
                        new_line.append(" ")
                new_lines.append("".join(new_line))
            lines = new_lines
            content_w = ART_CANVAS_COLS

        h = len(lines)
        if h > ART_CANVAS_LINES:
            start = (h - ART_CANVAS_LINES) // 2
            lines = lines[start:start + ART_CANVAS_LINES]
        elif h < ART_CANVAS_LINES:
            pad_top = (ART_CANVAS_LINES - h) // 2
            pad_bot = ART_CANVAS_LINES - h - pad_top
            lines = [""] * pad_top + lines + [""] * pad_bot

        normalized = []
        pad_l_block = max(0, (ART_CANVAS_COLS - content_w) // 2)
        for line in lines:
            line = " " * pad_l_block + line
            line = line.ljust(ART_CANVAS_COLS)
            if len(line) > ART_CANVAS_COLS:
                line = line[:ART_CANVAS_COLS]
            normalized.append(line)

        frames.append({"name": name, "lines": normalized})

    return frames


def xml_esc(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("'", "&apos;").replace('"', "&quot;")


# ──────────────────────────────────────────────
# Morph Frame Generation
# ──────────────────────────────────────────────
NUM_MORPH_FRAMES = 5


def generate_morph_frames(src_lines, dst_lines, seed_str):
    """5 intermediate frames between two icons: deconstruct -> scramble -> reconstruct."""
    rng = random.Random(hashlib.md5(seed_str.encode()).hexdigest())

    frame_specs = [
        (0.40, True),
        (0.75, True),
        (0.95, True),
        (0.75, False),
        (0.40, False),
    ]

    frames = []
    for rand_ratio, prefer_src in frame_specs:
        frame_lines = []
        for li in range(ART_CANVAS_LINES):
            s_line = src_lines[li] if li < len(src_lines) else " " * ART_CANVAS_COLS
            d_line = dst_lines[li] if li < len(dst_lines) else " " * ART_CANVAS_COLS
            result = []
            for ci in range(ART_CANVAS_COLS):
                s_ch = s_line[ci] if ci < len(s_line) else " "
                d_ch = d_line[ci] if ci < len(d_line) else " "
                if s_ch == " " and d_ch == " ":
                    result.append(" ")
                elif rng.random() < rand_ratio:
                    result.append(rng.choice(GLITCH_CHARS) if (s_ch != " " or d_ch != " ") else " ")
                else:
                    if prefer_src:
                        result.append(s_ch if s_ch != " " else d_ch)
                    else:
                        result.append(d_ch if d_ch != " " else s_ch)
            frame_lines.append("".join(result))
        frames.append(frame_lines)
    return frames


# ──────────────────────────────────────────────
# SVG Generation
# ──────────────────────────────────────────────
def generate_svg(config, stats, loc_stats, art_list, theme="dark"):
    is_dark = theme == "dark"

    C = {
        "bg":       "#0d1117" if is_dark else "#ffffff",
        "fg":       "#c9d1d9" if is_dark else "#24292f",
        "green":    "#3fb950" if is_dark else "#1a7f37",
        "red":      "#f85149" if is_dark else "#cf222e",
        "dim":      "#8b949e" if is_dark else "#57606a",
        "border":   "#30363d" if is_dark else "#d0d7de",
        "label":    "#79c0ff" if is_dark else "#0550ae",
        "title":    "#58a6ff" if is_dark else "#0969da",
        "glitch":   "#d29922" if is_dark else "#9a6700",
        "art":      "#3fb950" if is_dark else "#1a7f37",
    }

    display_s = config.get("aircraft_display_seconds", 4)
    trans_s = config.get("transition_seconds", 1.5)
    n = len(art_list)

    morph_frame_s = trans_s / NUM_MORPH_FRAMES
    phase_s = display_s + trans_s
    cycle_s = n * phase_s

    username = config["username"]
    hostname = config["hostname"]
    uptime = compute_uptime(config["birthdate"])

    s = stats or {}
    repos = s.get("repos", 0)
    stars = s.get("stars", 0)
    followers = s.get("followers", 0)
    commits = s.get("commits", 0)
    contribs = s.get("contributions", 0)
    loc_total, loc_add, loc_del = loc_stats

    def fmt(n):
        if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
        if n >= 1_000: return f"{n/1_000:.1f}K"
        return f"{n:,}"

    css_parts = []

    for i in range(n):
        t0 = i * phase_s
        t1 = t0 + display_s
        p_show = (t0 / cycle_s) * 100
        p_hide = (t1 / cycle_s) * 100
        p_fadein = max(0, p_show - 0.5)

        css_parts.append(f"""
    @keyframes show{i} {{
      0% {{ opacity: 0; }}
      {p_fadein:.2f}% {{ opacity: 0; }}
      {p_show:.2f}% {{ opacity: 1; }}
      {p_hide:.2f}% {{ opacity: 1; }}
      {min(p_hide + 1, 100):.2f}% {{ opacity: 0; }}
      100% {{ opacity: 0; }}
    }}
    .show{i} {{ animation: show{i} {cycle_s}s ease infinite; }}""")

    for i in range(n):
        next_i = (i + 1) % n
        t_morph_start = i * phase_s + display_s
        for f_idx in range(NUM_MORPH_FRAMES):
            t0 = t_morph_start + f_idx * morph_frame_s
            t1 = t0 + morph_frame_s
            p0 = (t0 / cycle_s) * 100
            p1 = (t1 / cycle_s) * 100
            p_pre = max(0, p0 - 0.3)
            anim_name = f"morph{i}f{f_idx}"
            css_parts.append(f"""
    @keyframes {anim_name} {{
      0% {{ opacity: 0; }}
      {p_pre:.2f}% {{ opacity: 0; }}
      {p0:.2f}% {{ opacity: 1; }}
      {p1:.2f}% {{ opacity: 1; }}
      {min(p1 + 0.3, 100):.2f}% {{ opacity: 0; }}
      100% {{ opacity: 0; }}
    }}
    .{anim_name} {{ animation: {anim_name} {cycle_s}s step-start infinite; }}""")

    css_parts.append("""
    @keyframes blink {
      0%, 100% { opacity: 1; }
      50% { opacity: 0; }
    }
    .cursor { animation: blink 1s step-end infinite; }""")

    art_groups = []
    for i, frame in enumerate(art_list):
        lines_svg = []
        for li, line in enumerate(frame["lines"]):
            y = PAD_TOP + li * LINE_HEIGHT
            lines_svg.append(f'      <text x="{PAD_LEFT}" y="{y}" class="art" xml:space="preserve">{xml_esc(line)}</text>')
        art_groups.append(f'    <g class="show{i}">\n' + "\n".join(lines_svg) + "\n    </g>")

    morph_groups = []
    for i in range(n):
        next_i = (i + 1) % n
        frames = generate_morph_frames(
            art_list[i]["lines"],
            art_list[next_i]["lines"],
            f"{art_list[i]['name']}-{art_list[next_i]['name']}"
        )
        for f_idx, frame_lines in enumerate(frames):
            lines_svg = []
            for li, line in enumerate(frame_lines):
                y = PAD_TOP + li * LINE_HEIGHT
                lines_svg.append(f'      <text x="{PAD_LEFT}" y="{y}" class="glitch" xml:space="preserve">{xml_esc(line)}</text>')
            morph_groups.append(f'    <g class="morph{i}f{f_idx}">\n' + "\n".join(lines_svg) + "\n    </g>")

    title_line = f"{username}@{hostname}"
    separator = "─" * len(title_line)

    info = []
    li = 0

    def txt(y, content, cls=""):
        c = f' class="{cls}"' if cls else ""
        return f'    <text x="{RIGHT_START_X}" y="{y}"{c}>{content}</text>'

    def field(label, value, color=None):
        nonlocal li
        y = PAD_TOP + li * LINE_HEIGHT
        vc = color or C["fg"]
        info.append(
            f'    <text x="{RIGHT_START_X}" y="{y}">'
            f'<tspan class="label">{xml_esc(label)}: </tspan>'
            f'<tspan fill="{vc}" class="val">{xml_esc(str(value))}</tspan></text>'
        )
        li += 1

    def spacer():
        nonlocal li
        li += 1

    info.append(txt(PAD_TOP + li * LINE_HEIGHT, xml_esc(title_line), "title"))
    li += 1
    info.append(txt(PAD_TOP + li * LINE_HEIGHT, separator, "dim"))
    li += 1

    spacer()
    field("OS", config["os"])
    field("Host", config["host"])
    field("Uptime", uptime)
    spacer()
    field("Languages", config["languages"])
    field("Frameworks", config["frameworks"])
    field("IDE", config["ide"])
    spacer()
    field("Packages", f"{fmt(repos)} repositories")
    field("Commits", fmt(commits))
    field("Stars", fmt(stars))
    field("Followers", fmt(followers))
    field("Contributions", fmt(contribs))

    y = PAD_TOP + li * LINE_HEIGHT
    info.append(
        f'    <text x="{RIGHT_START_X}" y="{y}">'
        f'<tspan class="label">Lines of code: </tspan>'
        f'<tspan fill="{C["fg"]}" class="val">{fmt(loc_total)}</tspan>'
        f'<tspan fill="{C["dim"]}" class="val"> (</tspan>'
        f'<tspan fill="{C["green"]}" class="val">{fmt(loc_add)}++</tspan>'
        f'<tspan fill="{C["dim"]}" class="val">, </tspan>'
        f'<tspan fill="{C["red"]}" class="val">{fmt(loc_del)}--</tspan>'
        f'<tspan fill="{C["dim"]}" class="val">)</tspan></text>'
    )
    li += 1

    spacer()
    field("Shell", config.get("shell", "bash"))
    field("Achievements", config.get("achievements", ""))

    svg = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg width="{SVG_W}" height="{SVG_H}" viewBox="0 0 {SVG_W} {SVG_H}"
     xmlns="http://www.w3.org/2000/svg">
  <style>
    .bg {{ fill: {C["bg"]}; stroke: {C["border"]}; stroke-width: 1; rx: 8; ry: 8; }}
    text {{ font-family: 'JetBrains Mono','Cascadia Code','Fira Code','SF Mono','Consolas','Courier New',monospace;
           font-size: {FONT_SIZE}px; fill: {C["fg"]}; white-space: pre; }}
    .art {{ fill: {C["art"]}; }}
    .glitch {{ fill: {C["glitch"]}; }}
    .label {{ fill: {C["label"]}; font-weight: bold; }}
    .title {{ fill: {C["title"]}; font-weight: bold; }}
    .val {{ font-weight: bold; }}
    .dim {{ fill: {C["dim"]}; }}
{"".join(css_parts)}
  </style>

  <rect class="bg" x="0.5" y="0.5" width="{SVG_W - 1}" height="{SVG_H - 1}" />

  <!-- Icon Art -->
  <g id="art-panel">
{chr(10).join(art_groups)}
{chr(10).join(morph_groups)}
  </g>

  <!-- Separator -->
  <line x1="{SEP_X}" y1="12" x2="{SEP_X}" y2="{SVG_H - 12}"
        stroke="{C["border"]}" stroke-width="1" stroke-dasharray="4,4" opacity="0.4"/>

  <!-- Info Panel -->
  <g id="info-panel">
{chr(10).join(info)}
  </g>

  <!-- Blinking cursor -->
  <text x="{RIGHT_START_X + len(title_line) * CHAR_WIDTH + 3}" y="{PAD_TOP}"
        class="title cursor">_</text>
</svg>"""
    return svg


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
def main():
    config = load_config()
    token = os.environ.get("GITHUB_TOKEN", "")
    username = config["username"]
    api_user = config.get("github_username", username)

    print(f"[*] Generating dashboard for {api_user} (display: {username})")

    art_list = load_art(config.get("art_dir", "art"))
    if not art_list:
        print("ERROR: No icon frames in art/ directory")
        sys.exit(1)
    print(f"[*] Loaded {len(art_list)} icons: {[a['name'] for a in art_list]}")

    stats = fetch_github_stats(api_user, token)
    cache_path = ROOT_DIR / ".stats-cache.json"
    if stats:
        print(f"[*] Stats: {stats}")
        cache_path.write_text(json.dumps(stats, indent=2))
    elif cache_path.exists():
        stats = json.loads(cache_path.read_text())
        print("[*] Using cached stats")
    else:
        stats = {"repos": 0, "stars": 0, "followers": 0, "commits": 0, "contributions": 0}

    loc_stats = fetch_loc_stats(api_user, token, config.get("loc_cache_file", ".loc-cache.json"))
    print(f"[*] LOC: total={loc_stats[0]}, add={loc_stats[1]}, del={loc_stats[2]}")

    for theme in ["dark", "light"]:
        svg = generate_svg(config, stats, loc_stats, art_list, theme)
        if "<svg" not in svg or "</svg>" not in svg:
            print(f"ERROR: {theme} SVG invalid, skipping")
            continue
        out = ROOT_DIR / f"profile-{theme}.svg"
        out.write_text(svg, encoding="utf-8")
        print(f"[\u2713] {out.name} ({len(svg):,} bytes)")

    print("[\u2713] Done!")


if __name__ == "__main__":
    main()
