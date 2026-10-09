#!/usr/bin/env python3
"""Validate the textile-expert package.

Usage:
  python3 scripts/validate_package.py [repo_root]     validate the package
  python3 scripts/validate_package.py --self-test     prove the validator fails on broken copies

Checks: required files; SKILL.md frontmatter (strict subset, plus a real YAML parse when PyYAML is installed)
including the `hooks:` block, which `claude plugin validate` does not inspect; size budgets; relative links and
every repo path mentioned in the docs; hook scripts (no network, process, eval or disk-write primitives);
shipped data; manifests against SKILL.md and README; evals; CI workflow; credential-like strings, e-mail
addresses and absolute user paths; development material and AI-attribution trailers kept out of the repo.
Exits 0 when every check passes, 1 otherwise. Standard library only (PyYAML optional).
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:  # optional: CI installs it, so the real parse always runs there
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None

SKILL_NAME = "textile-expert"
HEADER_RE = re.compile(r"^# Textile Expert — .+ — v(\d+\.\d+\.\d+) \((\d{4}-\d{2}-\d{2})\)$", re.M)

REFERENCES = [
    "content-and-seo.md", "economics-and-pricing.md", "fabrics-and-fibres.md", "hooks.md",
    "origins-by-country.md", "quality-control.md", "ukraine-legal.md",
]
HOOK_SCRIPTS = {"PostToolUse": "lint_content.py", "PreToolUse": "price_guard.py", "Stop": "stop_check.py"}
DATA_FILES = ["config.json", "fibres.json", "fabric_specs.json"]
REQUIRED_FILES = [
    "SKILL.md", "README.md", "LICENSE", ".gitignore",
    ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json",
    *(f"references/{n}" for n in REFERENCES),
    *(f"data/{n}" for n in DATA_FILES),
    *(f"hooks/{n}" for n in HOOK_SCRIPTS.values()),
    "scripts/landed_cost.py", "scripts/validate_package.py", "scripts/test_hooks.py",
    "evals/cases.json", "evals/JUDGE.md", ".github/workflows/validate.yml",
]
MIN_BYTES = {"README.md": 1500, "LICENSE": 500, "evals/JUDGE.md": 1500, ".github/workflows/validate.yml": 200}
MIN_REFERENCE_BYTES = 3000

# Budgets: (words, bytes). Words are whitespace-separated, as `wc -w`.
WORDS, BYTES = 0, 1
SKILL_BODY_WORDS = 1500
SKILL_FILE_BYTES = 25000
REFERENCE_BUDGET = (2500, 32000)
LOAD_BUDGET = (4200, 55000)        # SKILL.md plus the largest reference
ALL_REFERENCES_WORDS = 11000       # everything loaded at once

CONFIG_KEYS = {"min_gross_margin_pct", "price_outlier_ratio", "lint_block_on_error", "stop_check_enabled",
               "legal_as_of", "confidential_terms"}
EVAL_CATEGORIES = {
    "composition", "origin_claims", "legal_dating", "confidentiality", "landed_cost", "language",
    "unsupported_claims", "routing", "price_sanity", "missing_data",
}
MIN_EVAL_CASES = 10
RULE_REF = re.compile(r"^(R[1-9]|W-[A-G]|NEVER)$")

RESERVED_MARKETPLACE_NAMES = {
    "claude-code-marketplace", "claude-code-plugins", "claude-plugins-official", "anthropic-marketplace",
    "anthropic-plugins", "agent-skills", "anthropic-agent-skills", "life-sciences", "knowledge-work-plugins",
    "claude-for-legal", "claude-for-financial-services", "financial-services-plugins", "first-party-plugins",
    "claude-tag-plugins", "claude-community", "claude-plugins-community", "healthcare",
    "anthropic-plugin-directory", "claude-plugin-directory", "inline", "builtin", "skills-dir", "synced",
    "claude-plugin-test", "npm", "pip", "uv", "cargo", "github", "gh",
}
SECRET_PATTERNS = [
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("API key (sk-)", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"AIza[0-9A-Za-z_-]{30,}")),
    ("bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{30,}")),
    ("absolute user path", re.compile(r"(?:/Users|/home)/[A-Za-z0-9._-]+/|[A-Za-z]:\\Users\\")),
]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
ALLOWED_EMAIL_DOMAINS = {"example.com", "example.org", "example.net"}

# Generic names of working files that must never ship (this file is exempt from the mention scan below).
DEV_NAMES = {"SPEC.md", "PLAN.md", "TODO.md", "NOTES.md", "tasks", "dist"}
DEV_MENTION = re.compile(r"\b(?:SPEC|PLAN|TODO|NOTES)\.md\b|\btasks/[\w.-]+\.md\b")
JUNK_NAMES = {".DS_Store", "Thumbs.db"}
SKIP_DIRS = {".git", "node_modules", ".venv"}  # __pycache__ is NOT skipped: bytecode must not ship (git mode honours .gitignore)

FORBIDDEN_MODULES = {
    "socket", "ssl", "urllib", "http", "requests", "httpx", "subprocess", "ftplib", "smtplib", "telnetlib",
    "poplib", "imaplib", "xmlrpc", "ctypes", "pickle", "marshal", "shelve", "webbrowser", "asyncio",
    "multiprocessing", "shutil", "tempfile", "sqlite3", "importlib",
}
FORBIDDEN_NAMES = {"eval", "exec", "__import__"}
FORBIDDEN_OS = {
    "system", "popen", "remove", "unlink", "rename", "renames", "replace", "mkdir", "makedirs", "rmdir",
    "removedirs", "chmod", "chown", "symlink", "link", "fork", "kill", "killpg", "truncate", "putenv", "unsetenv",
} | {n for n in dir(__import__("os")) if n.startswith(("exec", "spawn", "posix_spawn"))}
FORBIDDEN_O_FLAGS = {"O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC"}


def words(text: str) -> int:
    return len(text.split())


def repo_files(root: Path) -> list[str]:
    """Files of the package: tracked plus untracked-not-ignored when in git, otherwise a walk."""
    if (root / ".git").exists():
        try:
            out = subprocess.run(
                ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                capture_output=True, check=True,
            ).stdout.decode("utf-8", "replace")
            return sorted({f for f in out.split("\0") if f and (root / f).is_file()})
        except (OSError, subprocess.CalledProcessError):
            pass
    found = []
    for path in root.rglob("*"):
        if path.is_file() and not (set(path.relative_to(root).parts[:-1]) & SKIP_DIRS):
            found.append(path.relative_to(root).as_posix())
    return sorted(found)


def split_frontmatter(text: str):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return (m.group(1), text[m.end():]) if m else (None, text)


PLAIN_SCALAR_BAD_START = tuple("[{>|*&!%@`\"'#-?,")


def parse_scalar(raw: str):
    """A value we accept: a JSON double-quoted string, or a plain scalar that every YAML loader reads as a string."""
    raw = raw.strip()
    if raw.startswith('"'):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return None, "not a valid double-quoted string"
        return (value, "") if isinstance(value, str) else (None, "not a string")
    if not raw:
        return None, "empty"
    if raw.startswith(PLAIN_SCALAR_BAD_START) or ": " in raw or " #" in raw or raw.endswith(":") or "\t" in raw:
        return None, "unquoted value that YAML could misread; put it in double quotes"
    if raw.lower() in {"true", "false", "null", "yes", "no", "on", "off", "~"} or re.fullmatch(r"[-+]?[\d.]+", raw):
        return None, "would not parse as a string; put it in double quotes"
    return raw, ""


def parse_top(block: str, allowed: set[str], nested: dict[str, set[str]]):
    """Strict subset: `key: value` lines, plus `key:` followed by two-space-indented `sub: value` lines."""
    fields, errors, current = {}, [], None
    for n, line in enumerate(block.split("\n"), 1):
        if not line.strip():
            continue
        if "\t" in line:
            errors.append(f"line {n}: tab character")
            continue
        m = re.match(r"^( {0,2})([A-Za-z][A-Za-z0-9_-]*):(.*)$", line)
        if not m or len(m.group(1)) == 1:
            errors.append(f"line {n}: not a `key: value` line")
            continue
        indent, key, rest = len(m.group(1)), m.group(2), m.group(3)
        if indent == 0:
            current = None
            if key not in allowed:
                errors.append(f"line {n}: unknown key {key!r}")
                continue
            if key in fields:
                errors.append(f"line {n}: duplicate key {key!r}")
                continue
            if key in nested:
                if rest.strip():
                    errors.append(f"line {n}: {key!r} must be a block")
                fields[key], current = {}, key
            else:
                value, err = parse_scalar(rest)
                if err:
                    errors.append(f"line {n}: {key}: {err}")
                fields[key] = value
        else:
            if current is None or key not in nested.get(current, set()):
                errors.append(f"line {n}: unexpected nested key {key!r}")
                continue
            value, err = parse_scalar(rest)
            if err:
                errors.append(f"line {n}: {current}.{key}: {err}")
            fields[current][key] = value
    return fields, errors


def parse_hooks(lines: list[str]):
    """The one hooks shape this skill uses: event -> matcher groups -> command handlers with a timeout."""
    struct: dict = {}
    errors: list[str] = []
    event = group = handler = None
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        if n == 1:
            if line.rstrip() != "hooks:":
                errors.append("hooks block must start with `hooks:`")
            continue
        m = re.match(r"^  ([A-Za-z]+):$", line)
        if m:
            event = m.group(1)
            if event in struct:
                errors.append(f"hooks: duplicate event {event!r}")
            struct[event] = []
            group = handler = None
            continue
        m = re.match(r'^    - matcher: "([^"\\]*)"$', line)
        if m and event:
            group = {"matcher": m.group(1), "handlers": [], "open": False}
            struct[event].append(group)
            handler = None
            continue
        if line == "    - hooks:" and event:
            group = {"matcher": None, "handlers": [], "open": True}
            struct[event].append(group)
            handler = None
            continue
        if line == "      hooks:" and group is not None and not group["open"]:
            group["open"] = True
            continue
        m = re.match(r"^        - type: (\S+)$", line)
        if m and group is not None and group["open"]:
            handler = {"type": m.group(1)}
            group["handlers"].append(handler)
            continue
        m = re.match(r"^          command: '([^']*)'$", line)
        if m and handler is not None:
            handler["command"] = m.group(1)
            continue
        m = re.match(r"^          timeout: (\d+)$", line)
        if m and handler is not None:
            handler["timeout"] = int(m.group(1))
            continue
        errors.append(f"hooks line {n}: unsupported structure: {line.strip()[:50]!r}")
    return struct, errors


def hooks_as_yaml(struct: dict) -> dict:
    out = {}
    for ev, groups in struct.items():
        out[ev] = []
        for g in groups:
            item = {} if g["matcher"] is None else {"matcher": g["matcher"]}
            item["hooks"] = [dict(h) for h in g["handlers"]]
            out[ev].append(item)
    return out


def relative_links(text: str) -> list[str]:
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    return [l for l in re.findall(r"\]\(([^)\s]+)\)", text) if not re.match(r"^([A-Za-z][A-Za-z0-9+.-]*:|#)", l)]


def load_json(root: Path, rel: str):
    try:
        return json.loads((root / rel).read_text(encoding="utf-8")), ""
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, f"{rel}: {exc}"


def unsafe_calls(source: str) -> list[str]:
    """Network, process, eval and disk-write primitives in a Python source file (AST based, so comments do not count)."""
    found = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"syntax error: {exc.msg}"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in FORBIDDEN_MODULES:
                    found.append(f"line {node.lineno}: import {a.name}")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in FORBIDDEN_MODULES:
                found.append(f"line {node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in FORBIDDEN_NAMES:
                found.append(f"line {node.lineno}: {fn.id}()")
            if isinstance(fn, ast.Name) and fn.id == "open":
                mode = None
                if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                    mode = node.args[1].value
                for kw in node.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                        mode = kw.value.value
                if isinstance(mode, str) and set(mode) & set("wax+"):
                    found.append(f"line {node.lineno}: open(..., {mode!r})")
            if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id == "os" and fn.attr in FORBIDDEN_OS:
                found.append(f"line {node.lineno}: os.{fn.attr}()")
            if isinstance(fn, ast.Attribute) and fn.attr in {"write_text", "write_bytes", "unlink", "rmdir", "mkdir", "touch"}:
                found.append(f"line {node.lineno}: .{fn.attr}()")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_O_FLAGS:
            found.append(f"line {node.lineno}: os.{node.attr}")
    return found


def check_commit_messages(root: Path) -> tuple[bool, str]:
    """No AI-attribution trailers in any commit message (only when the folder is a git repository)."""
    if not (root / ".git").exists():
        return True, "not a git repository"
    try:
        out = subprocess.run(["git", "-C", str(root), "log", "--format=%B%x00"], capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return True, "git log unavailable"
    text = out.decode("utf-8", "replace")
    bad = re.findall(r"(?im)^(?:co-authored-by:.*|.*generated with \[?claude.*)$", text)
    return (not bad), "; ".join(sorted(set(b.strip()[:60] for b in bad)))


def validate(root: Path) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []

    def check(label: str, ok: bool, detail: str = "") -> bool:
        results.append((label, bool(ok), detail))
        return bool(ok)

    def read(rel: str):
        try:
            return (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    files = repo_files(root)

    # 1. Required files ----------------------------------------------------------------------------------
    missing = [p for p in REQUIRED_FILES if not (root / p).is_file()]
    check(f"{len(REQUIRED_FILES)} required files exist", not missing, "missing: " + ", ".join(missing))
    small = [f"{p} ({len((root / p).read_bytes())} bytes)" for p, n in MIN_BYTES.items()
             if (root / p).is_file() and len((root / p).read_bytes()) < n]
    check("README, LICENSE, JUDGE.md and the workflow are not empty shells", not small, ", ".join(small))
    on_disk = sorted(Path(f).name for f in files if f.startswith("references/"))
    check("references/ holds exactly the known files (a new file needs a budget)", on_disk == sorted(REFERENCES), f"found {on_disk}")
    refs = {n: read(f"references/{n}") for n in REFERENCES}
    stubs = [n for n, t in refs.items() if t is not None and (len(t.encode()) < MIN_REFERENCE_BYTES or not t.startswith("# "))]
    check("every reference has a title and real content", not stubs, ", ".join(stubs))

    # 2. SKILL.md frontmatter ---------------------------------------------------------------------------------
    skill = read("SKILL.md")
    skill_version = header_version = header_date = license_name = None
    hooks_struct: dict = {}
    if check("SKILL.md is readable", skill is not None):
        block, body = split_frontmatter(skill)
        if check("SKILL.md has frontmatter", block is not None):
            lines = block.split("\n")
            idx = next((i for i, l in enumerate(lines) if l == "hooks:"), None)
            top_lines = lines if idx is None else lines[:idx]
            hook_lines = [] if idx is None else lines[idx:]
            fm, errors = parse_top("\n".join(top_lines), {"name", "description", "license", "metadata"}, {"metadata": {"version"}})
            check("SKILL.md frontmatter uses only the strict subset", not errors, "; ".join(errors))
            tail = [l for l in hook_lines[1:] if l.strip() and not l.startswith(" ")]
            check("hooks: is the last frontmatter key", idx is not None and not tail, "hooks block missing or followed by other keys")
            hooks_struct, herrors = parse_hooks(hook_lines)
            check("hooks: block parses in the one supported shape", idx is not None and not herrors, "; ".join(herrors[:4]))
            if yaml is None:
                check("frontmatter parses as YAML to the same values", True, "PyYAML not installed; strict subset check only")
            else:
                try:
                    loaded = yaml.safe_load(block)
                    expected = dict(fm)
                    expected["hooks"] = hooks_as_yaml(hooks_struct)
                    ok, detail = loaded == expected, ""
                    if not ok:
                        detail = "YAML parse differs from the strict parse"
                except Exception as exc:  # noqa: BLE001
                    ok, detail = False, f"YAML error: {str(exc).splitlines()[0]}"
                check("frontmatter parses as YAML to the same values", ok, detail)
            description = fm.get("description") or ""
            meta = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
            skill_version, license_name = meta.get("version"), fm.get("license")
            check("name is the skill name", fm.get("name") == SKILL_NAME, repr(fm.get("name")))
            check("description is a string of 1 to 1,024 characters",
                  isinstance(description, str) and 0 < len(description) <= 1024, f"{len(description)} characters")
            check("license is declared", bool(license_name))
            check("metadata.version is semantic", isinstance(skill_version, str)
                  and bool(re.fullmatch(r"\d+\.\d+\.\d+", skill_version)), repr(skill_version))

            # hook structure, which `claude plugin validate` does not look at
            problems = []
            if set(hooks_struct) != set(HOOK_SCRIPTS):
                problems.append(f"events {sorted(hooks_struct)} != {sorted(HOOK_SCRIPTS)}")
            for ev, groups in hooks_struct.items():
                handlers = [h for g in groups for h in g["handlers"]]
                if not handlers:
                    problems.append(f"{ev}: no handlers")
                for g in groups:
                    if g["matcher"] is not None:
                        try:
                            re.compile(g["matcher"])
                        except re.error as exc:
                            problems.append(f"{ev}: matcher is not a regex ({exc})")
                    if not g["handlers"]:
                        problems.append(f"{ev}: group without handlers")
                for h in handlers:
                    cmd = h.get("command", "")
                    if h.get("type") != "command":
                        problems.append(f"{ev}: handler type {h.get('type')!r}")
                    if not isinstance(h.get("timeout"), int) or not 1 <= h["timeout"] <= 60:
                        problems.append(f"{ev}: timeout {h.get('timeout')!r} not in 1..60")
                    if not cmd.rstrip().endswith("exit 0"):
                        problems.append(f"{ev}: command must end with `exit 0` (fail-open)")
                    named = set(re.findall(r"hooks/([A-Za-z0-9_]+\.py)", cmd))
                    if named != {HOOK_SCRIPTS.get(ev)}:
                        problems.append(f"{ev}: command names {sorted(named)}, expected {HOOK_SCRIPTS.get(ev)}")
                    for need in ("CLAUDE_SKILL_DIR", "CLAUDE_PLUGIN_ROOT", "$HOME/.claude/skills/textile-expert",
                                 "$CLAUDE_PROJECT_DIR/.claude/skills/textile-expert", "python3 -I"):
                        if need not in cmd:
                            problems.append(f"{ev}: command lacks {need}")
            check("every hook is wired once, to the right script, fail-open, with a sane timeout", not problems, "; ".join(problems[:5]))
            on_disk_hooks = sorted(Path(f).name for f in files if f.startswith("hooks/") and f.endswith(".py"))
            check("hooks/ holds exactly the wired scripts", on_disk_hooks == sorted(HOOK_SCRIPTS.values()), f"found {on_disk_hooks}")
        m = HEADER_RE.search(body)
        header_version = m.group(1) if m else None
        header_date = m.group(2) if m else None
        check("SKILL.md carries the version header", header_version is not None)
        check("header version equals metadata.version", bool(header_version and header_version == skill_version),
              f"header v{header_version} vs metadata {skill_version}")
        body_words = words(body)
        check(f"SKILL.md body is within budget ({body_words:,} of {SKILL_BODY_WORDS:,} words)", body_words <= SKILL_BODY_WORDS)
        check(f"SKILL.md file is within budget ({len(skill.encode()):,} of {SKILL_FILE_BYTES:,} bytes)", len(skill.encode()) <= SKILL_FILE_BYTES)
        absent = [n for n in REFERENCES if f"references/{n}" not in skill]
        check("every reference is named in SKILL.md", not absent, ", ".join(absent))

    # 3. Budgets ----------------------------------------------------------------------------------------------------
    if skill is not None and all(v is not None for v in refs.values()):
        over = [f"{n}: {words(t):,} words, {len(t.encode()):,} bytes" for n, t in refs.items()
                if words(t) > REFERENCE_BUDGET[WORDS] or len(t.encode()) > REFERENCE_BUDGET[BYTES]]
        check(f"each reference is within {REFERENCE_BUDGET[WORDS]:,} words / {REFERENCE_BUDGET[BYTES]:,} bytes", not over, "; ".join(over))
        biggest = max(refs.values(), key=words)
        w, b = words(skill) + words(biggest), len(skill.encode()) + len(biggest.encode())
        check(f"SKILL.md plus the largest reference: {w:,} words, {b:,} bytes (budget {LOAD_BUDGET[WORDS]:,} / {LOAD_BUDGET[BYTES]:,})",
              w <= LOAD_BUDGET[WORDS] and b <= LOAD_BUDGET[BYTES])
        total = sum(words(t) for t in refs.values())
        check(f"all references together: {total:,} words (budget {ALL_REFERENCES_WORDS:,})", total <= ALL_REFERENCES_WORDS)
    else:
        check("budgets can be measured", False, "SKILL.md or a reference file is missing")

    # 4. Links and paths named in the docs ----------------------------------------------------------------------------
    broken = []
    md_files = [f for f in files if f.endswith(".md")]
    for rel in md_files:
        text = read(rel)
        if text is None:
            continue
        base = (root / rel).parent
        for link in relative_links(text):
            target = link.split("#")[0]
            if not target:
                continue
            resolved = (base / target).resolve()
            if target.startswith("/") or root.resolve() not in [resolved, *resolved.parents] or not resolved.exists():
                broken.append(f"{rel} -> {link}")
    check("every relative link resolves from the file that contains it", not broken, "; ".join(broken))
    ghosts = []
    path_rx = re.compile(r"`((?:references|data|hooks|scripts|evals)/[A-Za-z0-9_./-]+)`|\$\{CLAUDE_SKILL_DIR\}/([A-Za-z0-9_./-]+)")
    for rel in [f for f in md_files if f.startswith(("references/", "evals/")) or f in ("SKILL.md", "README.md", "README.uk.md")]:
        text = read(rel) or ""
        for m in path_rx.finditer(text):
            target = (m.group(1) or m.group(2)).rstrip(".")
            if not (root / target).exists():
                ghosts.append(f"{rel}: {target}")
    check("every repo path named in the docs exists", not ghosts, "; ".join(sorted(set(ghosts))[:8]))

    # 5. Hook scripts: the promises made in references/hooks.md ---------------------------------------------------------------
    offences = []
    for rel in [*(f"hooks/{n}" for n in HOOK_SCRIPTS.values()), "scripts/landed_cost.py"]:
        src = read(rel)
        if src is not None:
            offences += [f"{rel} {o}" for o in unsafe_calls(src)]
    check("hooks and the calculator use no network, process, eval or disk-write primitives", not offences, "; ".join(offences[:6]))
    shebang = [rel for rel in [*(f"hooks/{n}" for n in HOOK_SCRIPTS.values()), "scripts/landed_cost.py"]
               if (read(rel) or "").split("\n", 1)[0] != "#!/usr/bin/env python3"]
    check("scripts start with the python3 shebang", not shebang, ", ".join(shebang))

    # 6. Shipped data --------------------------------------------------------------------------------------------------------------
    data = {n: load_json(root, f"data/{n}") for n in DATA_FILES}
    bad_json = [err for _, (v, err) in data.items() if v is None]
    check("data/*.json parse", not bad_json, "; ".join(bad_json))
    cfg = data["config.json"][0] if isinstance(data["config.json"][0], dict) else {}
    unknown = sorted(k for k in cfg if k not in CONFIG_KEYS and not k.startswith("_"))
    check("data/config.json has only known keys", not unknown, ", ".join(unknown))
    check("shipped confidential_terms is empty", cfg.get("confidential_terms") == [], repr(cfg.get("confidential_terms")))
    check("legal_as_of is a DD.MM.YYYY date", bool(re.fullmatch(r"[0-9]{2}\.[0-9]{2}\.[0-9]{4}", str(cfg.get("legal_as_of", "")))))
    fabrics = data["fabric_specs.json"][0].get("fabrics", []) if isinstance(data["fabric_specs.json"][0], dict) else []
    insane = [f.get("key", "?") for f in fabrics if not (isinstance(f.get("gsm"), list) and len(f["gsm"]) == 2 and 0 < f["gsm"][0] < f["gsm"][1] and f.get("aliases"))]
    check("every fabric has aliases and a sane density range", bool(fabrics) and not insane, ", ".join(insane))

    # 7. Evals -------------------------------------------------------------------------------------------------------------------------
    payload, err = load_json(root, "evals/cases.json")
    if check("evals/cases.json parses", payload is not None, err):
        cases = payload.get("cases") if isinstance(payload, dict) else None
        cases = cases if isinstance(cases, list) else []

        def text_list(v):
            return isinstance(v, list) and len(v) > 0 and all(isinstance(x, str) and x.strip() for x in v)

        malformed = []
        for i, c in enumerate(cases):
            label = c.get("id") if isinstance(c, dict) and isinstance(c.get("id"), str) else f"case {i}"
            if not isinstance(c, dict):
                malformed.append(f"{label}: not an object")
                continue
            if not (isinstance(c.get("id"), str) and c["id"].strip()):
                malformed.append(f"{label}: id")
            if c.get("category") not in EVAL_CATEGORIES:
                malformed.append(f"{label}: category")
            if not (isinstance(c.get("prompt"), str) and c["prompt"].strip()):
                malformed.append(f"{label}: prompt")
            if not isinstance(c.get("input"), str):
                malformed.append(f"{label}: input")
            if not text_list(c.get("requirements")):
                malformed.append(f"{label}: requirements")
            if not text_list(c.get("prohibitions")):
                malformed.append(f"{label}: prohibitions")
            if not (text_list(c.get("rule_refs")) and all(RULE_REF.match(r) for r in c["rule_refs"])):
                malformed.append(f"{label}: rule_refs")
        good = [c for c in cases if isinstance(c, dict)]
        check(f"at least {MIN_EVAL_CASES} eval cases ({len(cases)} found)", len(cases) >= MIN_EVAL_CASES)
        check("eval cases are well formed (types, category, rule_refs)", not malformed, "; ".join(malformed[:10]))
        ids = [c.get("id") for c in good]
        check("eval case ids are unique", len(ids) == len(set(map(str, ids))))
        prompts = [(c.get("prompt"), c.get("input")) for c in good]
        check("no two eval cases share prompt and input", len(prompts) == len(set(map(str, prompts))))
        missing_cat = sorted(EVAL_CATEGORIES - {c.get("category") for c in good})
        check("eval categories are all covered", not missing_cat, "missing: " + ", ".join(missing_cat))
    judge = (read("evals/JUDGE.md") or "").lower()
    check("JUDGE.md applies each case's requirements and prohibitions", "requirement" in judge and "prohibition" in judge)

    # 8. Manifests, license, README, CI ---------------------------------------------------------------------------------------------------
    plugin, err1 = load_json(root, ".claude-plugin/plugin.json")
    market, err2 = load_json(root, ".claude-plugin/marketplace.json")
    check("plugin.json and marketplace.json parse", plugin is not None and market is not None, err1 or err2)
    plugin = plugin if isinstance(plugin, dict) else {}
    market = market if isinstance(market, dict) else {}
    readme = read("README.md") or ""
    license_text = read("LICENSE") or ""
    author = plugin.get("author") if isinstance(plugin.get("author"), dict) else {}
    owner = market.get("owner") if isinstance(market.get("owner"), dict) else {}
    entries = [e for e in market.get("plugins", []) if isinstance(e, dict)] if isinstance(market.get("plugins"), list) else []

    check("plugin name is the skill name", plugin.get("name") == SKILL_NAME, repr(plugin.get("name")))
    check("plugin name does not pass as an Anthropic plugin", not re.match(r"(?i)^(claude|anthropic|cc-plugin)", str(plugin.get("name", ""))))
    check("plugin version equals SKILL.md metadata.version", bool(skill_version) and plugin.get("version") == skill_version,
          f"plugin.json {plugin.get('version')!r} vs SKILL.md {skill_version!r}")
    check("plugin declares description, author.name and license",
          bool(isinstance(plugin.get("description"), str) and plugin["description"].strip() and author.get("name") and plugin.get("license")))
    check("no e-mail address in the manifests", "email" not in author and "email" not in owner)
    check("license agrees in SKILL.md, plugin.json and LICENSE",
          bool(license_name) and plugin.get("license") == license_name and f"{license_name} License" in license_text,
          f"SKILL.md {license_name!r}, plugin.json {plugin.get('license')!r}")
    repo = plugin.get("repository") if isinstance(plugin.get("repository"), str) else ""
    slug = re.match(r"^https://github\.com/([\w.-]+/[\w.-]+?)(?:\.git)?/?$", repo)
    check("plugin.json has a GitHub repository URL", slug is not None, repr(repo))
    mname = market.get("name") if isinstance(market.get("name"), str) else ""
    check("marketplace name is valid and not reserved",
          bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", mname)) and mname.lower() not in RESERVED_MARKETPLACE_NAMES
          and not mname.lower().startswith("claudeai-"), repr(mname))
    check("marketplace owner has a name", bool(owner.get("name")))
    entry = next((e for e in entries if e.get("name") == SKILL_NAME), None)
    if check("marketplace lists the plugin exactly once", sum(1 for e in entries if e.get("name") == SKILL_NAME) == 1):
        check("entry source is the repo root and omits version", entry.get("source") in (".", "./") and "version" not in entry,
              f"source={entry.get('source')!r}")
        check("entry description equals the plugin.json description", entry.get("description") == plugin.get("description"))
    dangling = [str(e.get("source")) for e in entries if isinstance(e.get("source"), str) and not (root / e["source"]).exists()]
    check("every marketplace entry points at an existing path", not dangling, ", ".join(dangling))
    wanted = [f"{SKILL_NAME}@{mname}"]
    if slug:
        wanted.append(f"claude plugin marketplace add {slug.group(1)}")
    if header_version:
        wanted.append(f"v{header_version} ({header_date})")
    absent = [w for w in wanted if w not in readme]
    check("README install commands and version match the manifests", not absent, "README lacks: " + ", ".join(absent))
    check("README states the legal-advice disclaimer", "not legal" in readme.lower() or "не є юридичн" in readme.lower())

    workflow = read(".github/workflows/validate.yml") or ""
    need = ["scripts/validate_package.py", "--self-test", "scripts/test_hooks.py", "pull_request", "push", "contents: read", "3.9"]
    check("CI workflow runs the validator, its self-test and the tests on Python 3.9", all(n in workflow for n in need),
          "lacks: " + ", ".join(n for n in need if n not in workflow))

    # 9. Nothing private, nothing developmental ships ------------------------------------------------------------------------------------------
    hits, dev_hits = [], []
    for rel in files:
        text = read(rel)
        if text is None:
            continue
        for label, rx in SECRET_PATTERNS:
            if rx.search(text):
                hits.append(f"{rel}: {label}")
        for m in EMAIL.finditer(text):
            if m.group(1).lower() not in ALLOWED_EMAIL_DOMAINS:
                hits.append(f"{rel}: email address at {m.group(1)}")
        if rel != "scripts/validate_package.py" and DEV_MENTION.search(text):
            dev_hits.append(rel)
    check(f"no credentials, e-mail addresses or absolute user paths in {len(files)} files", not hits, "; ".join(sorted(set(hits))[:10]))
    present = sorted(f for f in files if Path(f).parts[0] in DEV_NAMES or Path(f).name in DEV_NAMES)
    check("no development material (spec, plan, tasks, dist) in the package", not present, ", ".join(present[:6]))
    check("no shipped file mentions development material", not dev_hits, ", ".join(sorted(dev_hits)[:6]))
    junk = sorted(f for f in files if Path(f).name in JUNK_NAMES or "__pycache__" in Path(f).parts or f.endswith(".pyc"))
    check("no editor, OS or bytecode junk", not junk, ", ".join(junk[:6]))
    ignore = read(".gitignore") or ""
    check(".gitignore covers junk and does not hint at development files",
          all(n in ignore for n in (".DS_Store", "__pycache__/", "*.pyc")) and not re.search(r"(?i)spec|tasks|plan|todo", ignore))
    ok, detail = check_commit_messages(root)
    check("commit messages carry no co-author or AI-attribution trailers", ok, detail)
    return results


def report(results) -> int:
    for label, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  -> {detail}" if detail and not ok else ""))
    failures = [label for label, ok, _ in results if not ok]
    print()
    if failures:
        print(f"{len(failures)} validation failure(s)")
        return 1
    print("Package validation passed" + ("" if yaml else " (PyYAML not installed: frontmatter checked by the strict subset only)"))
    return 0


# --------------------------------------------------------------------------------------- self-test
def _edit(rel, fn):
    def apply(root: Path):
        path = root / rel
        path.write_text(fn(path.read_text(encoding="utf-8")), encoding="utf-8")
    return apply


def _write(rel, text):
    def apply(root: Path):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return apply


def _json(rel, fn):
    def apply(root: Path):
        path = root / rel
        data = json.loads(path.read_text(encoding="utf-8"))
        fn(data)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return apply


def _first_line(key, new):
    return lambda t: re.sub(rf"^{key}:.*$", new, t, count=1, flags=re.M)


def mutations():
    secret = "AKIA" + "B" * 16
    user_path = "/Users/" + "someone/project"
    dev_file = "SPEC.md"
    return [
        ("unquoted description with a colon", _edit("SKILL.md", _first_line("description", "description: Use for textiles. Topics: fabrics"))),
        ("description with unescaped inner quotes", _edit("SKILL.md", _first_line("description", 'description: "Use for "textile" work"'))),
        ("description over 1,024 characters", _edit("SKILL.md", _first_line("description", 'description: "' + "x" * 1100 + '"'))),
        ("tab-indented version", _edit("SKILL.md", lambda t: t.replace('  version: "', '\tversion: "', 1))),
        ("metadata key renamed", _edit("SKILL.md", lambda t: t.replace("\nmetadata:\n", "\nwrongkey:\n", 1))),
        ("unknown frontmatter key", _edit("SKILL.md", lambda t: t.replace("\nlicense:", "\nallowed-tools: Bash\nlicense:", 1))),
        ("header version differs from metadata", _edit("SKILL.md", lambda t: re.sub(r"— v\d+\.\d+\.\d+ \(", "— v9.9.9 (", t, count=1))),
        ("hook event name misspelt", _edit("SKILL.md", lambda t: t.replace("  PostToolUse:\n", "  PostToolUseX:\n", 1))),
        ("hook timeout not a number", _edit("SKILL.md", lambda t: t.replace("          timeout: 15\n", "          timeout: fifteen\n", 1))),
        ("hook type invalid", _edit("SKILL.md", lambda t: t.replace("        - type: command\n", "        - type: cmd\n", 1))),
        ("hook command not fail-open", _edit("SKILL.md", lambda t: t.replace("; exit 0'", "'", 1))),
        ("hook wired to the wrong script", _edit("SKILL.md", lambda t: t.replace("hooks/price_guard.py", "hooks/lint_content.py"))),
        ("hook wired to a missing script", _edit("SKILL.md", lambda t: t.replace("hooks/stop_check.py", "hooks/ghost.py"))),
        ("hook matcher not a regex", _edit("SKILL.md", lambda t: t.replace('matcher: "Write|Edit|MultiEdit"', 'matcher: "Write|(Edit"', 1))),
        ("hooks block drops its install fallbacks", _edit("SKILL.md", lambda t: t.replace("$HOME/.claude/skills/textile-expert", "/opt/x"))),
        ("hooks block not last", _edit("SKILL.md", lambda t: t.replace("\n---\n", "\nlicense2: x\n---\n", 1))),
        ("oversized SKILL.md", _edit("SKILL.md", lambda t: t + "\n" + "word " * 700)),
        ("emptied reference", _write("references/quality-control.md", "")),
        ("unbudgeted reference file", _write("references/extra-playbook.md", "# Extra\n\n" + "word " * 20000)),
        ("oversized reference", _edit("references/origins-by-country.md", lambda t: t + "\n" + "word " * 3000)),
        ("hook imports subprocess", _edit("hooks/stop_check.py", lambda t: t.replace("import json\n", "import json\nimport subprocess\n", 1))),
        ("hook opens a file for writing", _edit("hooks/lint_content.py", lambda t: t + "\n\ndef _leak(p):\n    open(p, 'w').write('x')\n")),
        ("hook calls eval", _edit("hooks/price_guard.py", lambda t: t + "\n\ndef _x(s):\n    return eval(s)\n")),
        ("hook uses the network", _edit("hooks/lint_content.py", lambda t: t.replace("import json\n", "import json\nimport urllib.request\n", 1))),
        ("calculator removes a file", _edit("scripts/landed_cost.py", lambda t: t + "\n\ndef _rm(p):\n    __import__('os').remove(p)\n")),
        ("shebang lost", _edit("hooks/stop_check.py", lambda t: t.replace("#!/usr/bin/env python3\n", "", 1))),
        ("e-mail address", _edit("README.md", lambda t: t + "\nContact someone@" + "gmail.com\n")),
        ("credential in LICENSE", _edit("LICENSE", lambda t: t + "\n" + secret + "\n")),
        ("absolute user path in a reference", _edit("references/hooks.md", lambda t: t + "\nSee " + user_path + "\n")),
        ("development spec ships", _write(dev_file, "# Spec\n")),
        ("development tasks ship", _write("tasks/notes.md", "# Notes\n")),
        ("development notes ship", _write("NOTES.md", "# Notes\n")),
        ("README mentions the spec", _edit("README.md", lambda t: t + "\nSee " + dev_file + "\n")),
        ("editor junk ships", _write(".DS_Store", "x")),
        ("bytecode ships", _write("hooks/__pycache__/x.pyc", "x")),
        (".gitignore hints at development files", _edit(".gitignore", lambda t: t + "\ntasks/\n")),
        ("confidential terms shipped", _json("data/config.json", lambda d: d.update({"confidential_terms": ["Acme"]}))),
        ("unknown config key", _json("data/config.json", lambda d: d.update({"surprise": 1}))),
        ("fabric with an inverted range", _json("data/fabric_specs.json", lambda d: d["fabrics"][0].update({"gsm": [200, 100]}))),
        ("data file corrupted", _write("data/fibres.json", "{not json")),
        ("plugin version not bumped", _json(".claude-plugin/plugin.json", lambda d: d.update({"version": "0.0.1"}))),
        ("plugin author with an e-mail", _json(".claude-plugin/plugin.json", lambda d: d["author"].update({"email": "a@example.com"}))),
        ("license mismatch", _json(".claude-plugin/plugin.json", lambda d: d.update({"license": "Proprietary"}))),
        ("plugin name passes as Anthropic's", _json(".claude-plugin/plugin.json", lambda d: d.update({"name": "claude-textile-expert"}))),
        ("reserved marketplace name", _json(".claude-plugin/marketplace.json", lambda d: d.update({"name": "claude-code-plugins"}))),
        ("marketplace renamed but README not updated", _json(".claude-plugin/marketplace.json", lambda d: d.update({"name": "renamed-marketplace"}))),
        ("marketplace entry points elsewhere", _json(".claude-plugin/marketplace.json", lambda d: d["plugins"][0].update({"source": "./plugins/textile-expert"}))),
        ("marketplace entry description drifts", _json(".claude-plugin/marketplace.json", lambda d: d["plugins"][0].update({"description": "other"}))),
        ("doc names a script that does not exist", _edit("SKILL.md", lambda t: t + "\n`${CLAUDE_SKILL_DIR}/scripts/ghost.py`\n")),
        ("doc names a reference that does not exist", _edit("references/hooks.md", lambda t: t + "\nSee `references/ghost.md`\n")),
        ("broken relative link", _edit("README.md", lambda t: t + "\n[gone](missing-file.md)\n")),
        ("absolute link", _edit("README.md", lambda t: t + "\n[hosts](/etc/hosts)\n")),
        ("eval case with wrong types", _json("evals/cases.json", lambda d: d["cases"][0].update({"requirements": "anything", "prohibitions": 7}))),
        ("eval case with an unknown category", _json("evals/cases.json", lambda d: d["cases"][0].update({"category": "composittion"}))),
        ("eval case with a bad rule reference", _json("evals/cases.json", lambda d: d["cases"][0].update({"rule_refs": ["Z9"]}))),
        ("duplicate eval prompts", _json("evals/cases.json", lambda d: d["cases"].append(dict(d["cases"][0], id="copy")))),
        ("too few eval cases", _json("evals/cases.json", lambda d: d.update({"cases": d["cases"][:3]}))),
        ("JUDGE.md ignores the rubric", _write("evals/JUDGE.md", "# Evaluation protocol\n\n" + "Score each output from 1 to 5 on fit. " * 60)),
        ("CI no longer runs the self-test", _edit(".github/workflows/validate.yml", lambda t: t.replace("--self-test", "--version"))),
        ("CI drops Python 3.9", _edit(".github/workflows/validate.yml", lambda t: t.replace("3.9", "3.12"))),
        ("emptied README", _write("README.md", "# Textile Expert\n")),
        ("README release date differs from the SKILL.md header", _edit("README.md", lambda t: re.sub(r"\(\d{4}-\d{2}-\d{2}\)", "(2001-01-01)", t, count=1))),
        ("README loses the disclaimer", _edit("README.md", lambda t: re.sub(r"(?i)not legal|не є юридичн", "legal", t))),
    ]


def commit_message_selftest() -> list[tuple[str, bool]]:
    """The attribution-trailer check needs a real git history, so it is exercised in a throwaway repository."""
    outcomes = []
    if shutil.which("git") is None:
        return [("git not installed: commit-message check not exercised", True)]
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)

        def git(*args):
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                           capture_output=True, check=True)

        git("init", "-q")
        (repo / "a").write_text("1", encoding="utf-8")
        git("add", "a")
        git("commit", "-q", "-m", "Add a")
        outcomes.append(("accepts a clean commit message", check_commit_messages(repo)[0]))
        (repo / "a").write_text("2", encoding="utf-8")
        git("add", "a")
        git("commit", "-q", "-m", "Change a\n\nCo-Authored-By: Someone <someone@example.com>")
        outcomes.append(("rejects a Co-Authored-By trailer", not check_commit_messages(repo)[0]))
    return outcomes


def self_test(root: Path) -> int:
    files = repo_files(root)
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        clean = Path(tmp) / "clean"
        for rel in files:
            (clean / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / rel, clean / rel)
        bad = [label for label, ok, _ in validate(clean) if not ok]
        print(f"{'PASS' if not bad else 'FAIL'}  unmodified copy passes" + (f"  -> {bad}" if bad else ""))
        failures += bool(bad)
        cases = mutations()
        for i, (name, apply) in enumerate(cases):
            work = Path(tmp) / f"m{i}"
            shutil.copytree(clean, work)
            apply(work)
            caught = [label for label, ok, _ in validate(work) if not ok]
            print(f"{'PASS' if caught else 'FAIL'}  rejects: {name}" + (f"  ({caught[0][:70]})" if caught else "  -> NOT DETECTED"))
            failures += not caught
    extra = commit_message_selftest()
    for name, ok in extra:
        print(f"{'PASS' if ok else 'FAIL'}  commit messages: {name}")
        failures += not ok
    total = len(cases) + 1 + len(extra)
    print()
    print(f"Self-test: {total - failures} of {total} checks behaved as expected")
    return 1 if failures else 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    base = Path(args[0]).resolve() if args else Path(__file__).resolve().parent.parent
    sys.exit(self_test(base) if "--self-test" in sys.argv else report(validate(base)))
