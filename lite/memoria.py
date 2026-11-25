#!/usr/bin/env python3
"""Memória nativa do Claude Code: aviso de índice, auditoria e backup (macOS, só stdlib).

Porta enxuta de peças do hipocampo (github.com/JhouCode/hipocampo).

  memoria.py hook
      SessionStart. Lê o payload no stdin e olha o MEMORY.md da sessão (a pasta do
      transcript_path). Avisa se o índice será cortado, está perto do corte, tem link
      quebrado ou se há memória fora do índice. Silêncio = tudo ok. Sempre sai 0.
  memoria.py check [PROJECTS]
      Audita todos os índices. Sai 1 se algum tiver problema.
  memoria.py backup [PROJECTS] [--dest DIR] [--exclude TEXTO ...]
      Espelha PROJECTS/*/memory/**.md em DIR/<slug>/ e faz commit num git local se algo
      mudou. Faz push só se o repo de DIR tiver um remote. Slugs que contêm TEXTO ficam fora.

PROJECTS padrão: $CLAUDE_CONFIG_DIR/projects ou ~/.claude/projects.
DIR padrão: $MEMORIA_BACKUP_DIR ou ~/.local/share/claude-memory-backup.
"""
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time

LINES_CAP = 200
UNITS_CAP = 25000
FLOOR = 15
LINK = re.compile(r"\]\(([^)\s]+\.md)\)")


# --- The loader's cut rule. Copied from the upstream scripts/memory-cap-probe.py (v3),
# --- which matched live sessions on 7 files: trim, count UTF-16 units, CR counts.

def _read(path, newline=None):
    with open(path, encoding="utf-8", newline=newline) as f:
        return f.read()


def _load(path):
    s = _read(path, newline="")
    t = re.sub(r"^[\s﻿]+|[\s﻿]+$", "", s)
    return t.encode("utf-16-le"), (t.count("\n") + 1 if t else 0)


def _newline_before(u, end):
    i = u.rfind(b"\n\x00", 0, 2 * end + 2)
    while i > 0 and i % 2:
        i = u.rfind(b"\n\x00", 0, i + 1)
    return i // 2 if i >= 0 else -1


def measure(path):
    """Which cap binds first and where it cuts, as prose ('no cut ...' when it fits)."""
    u, n = _load(path)
    units = len(u) // 2
    if n <= LINES_CAP and units <= UNITS_CAP:
        return "no cut (%d/%d lines, %d/%d units)" % (n, LINES_CAP, units, UNITS_CAP)
    kept = u
    if n > LINES_CAP:
        j = -1
        for _ in range(LINES_CAP):
            j = u.find(b"\n\x00", 2 * j + 2 if j >= 0 else 0)
            while j % 2:
                j = u.find(b"\n\x00", j + 1)
            j //= 2
        kept = u[:2 * j]
    which = "line"
    if len(kept) // 2 > UNITS_CAP:
        q = _newline_before(kept, UNITS_CAP)
        kept = kept[:2 * q] if q > 0 else kept[:2 * UNITS_CAP]
        which = "size"
    k = len(kept) // 2
    if u[2 * k:2 * k + 2] != b"\n\x00":
        return "size-bound, line 1 cut after %d units (last kept: none)" % k
    cut = kept.decode("utf-16-le").count("\n") + 2
    return "%s-bound, first line dropped: %d (last kept: %d)" % (which, cut, cut - 1)


def metrics(path):
    """Lines, units and the binding slack in lines (the smaller of the two caps)."""
    u, n = _load(path)
    units = len(u) // 2
    density = units / n if n else 1.0
    size_free = int((UNITS_CAP - units) / density) if density > 0 else LINES_CAP
    return {"lines": n, "units": units, "binding_free": min(LINES_CAP - n, size_free)}


# --- Index integrity.

def _memory_files(mem):
    return {f for f in os.listdir(mem) if f.endswith(".md") and f != "MEMORY.md"}


def index_problems(mem):
    """(broken links, orphan files) of one memory folder, both sorted."""
    idx = os.path.join(mem, "MEMORY.md")
    text = _read(idx) if os.path.isfile(idx) else ""
    cited = {os.path.normpath(t) for t in LINK.findall(text) if "://" not in t}
    broken = sorted(c for c in cited if not os.path.isfile(os.path.join(mem, c)))
    return broken, sorted(_memory_files(mem) - cited)


def _names(items, limit=5):
    shown = ", ".join(items[:limit])
    return shown + (" e mais %d" % (len(items) - limit) if len(items) > limit else "")


def store_lines(mem):
    """Warnings for one memory folder. Empty list = healthy (or nothing there)."""
    if not os.path.isdir(mem) or not _memory_files(mem) and not os.path.isfile(
            os.path.join(mem, "MEMORY.md")):
        return []
    out = []
    idx = os.path.join(mem, "MEMORY.md")
    if os.path.isfile(idx):
        m, cut = metrics(idx), measure(idx)
        if not cut.startswith("no cut"):
            out.append("[memória] índice TRUNCADO (%d/%d linhas, %d/%d unidades; %s): o que "
                       "passa do corte não chega à sessão. Proponha um enxugamento ao usuário."
                       % (m["lines"], LINES_CAP, m["units"], UNITS_CAP, cut))
        elif m["binding_free"] < FLOOR:
            out.append("[memória] índice perto do corte: folga de %d linhas (%d/%d linhas, "
                       "%d/%d unidades). Proponha um enxugamento ao usuário."
                       % (m["binding_free"], m["lines"], LINES_CAP, m["units"], UNITS_CAP))
    broken, orphans = index_problems(mem)
    if broken:
        out.append("[memória] links quebrados no MEMORY.md: %s" % _names(broken))
    if orphans:
        out.append("[memória] memórias fora do índice (nunca serão abertas): %s"
                   % _names(orphans))
    return out


def slug(path):
    """Claude Code's folder name for a path: every non-alphanumeric character becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


GIT_REPO_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                 "GIT_COMMON_DIR", "GIT_PREFIX")


def _git_env():
    """The environment without the variables that point git at another repo (a git hook
    exports them)."""
    return {k: v for k, v in os.environ.items() if k not in GIT_REPO_VARS}


def _main_root(cwd):
    """Root of the main worktree of the git repo holding cwd, or None."""
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--path-format=absolute",
                            "--git-common-dir"], capture_output=True, text=True, timeout=5,
                           env=_git_env())
    except Exception:
        return None
    common = r.stdout.strip()
    if r.returncode or os.path.basename(common) != ".git":
        return None
    return os.path.dirname(common)


def hook_lines(payload):
    """A session in a git repo (worktrees included) loads the main repo's store; otherwise
    the store is the transcript's folder."""
    tp = (payload or {}).get("transcript_path") or ""
    if not tp:
        return []
    projects = os.path.dirname(os.path.dirname(tp))
    root = _main_root((payload or {}).get("cwd") or "")
    candidates = [os.path.join(projects, slug(root), "memory")] if root else []
    candidates.append(os.path.join(os.path.dirname(tp), "memory"))
    for mem in candidates:
        if os.path.isdir(mem):
            return store_lines(mem)
    return []


def hook():
    try:
        for line in hook_lines(json.loads(sys.stdin.read() or "{}")):
            print(line)
    except Exception:
        pass  # a session hook never blocks the session
    return 0


# --- Audit and backup over every store.

def default_projects():
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(base, "projects")


def _raise(error):
    raise error


def stores(projects):
    """{slug: memory folder} for every folder with at least one .md file. An unreadable
    folder raises: skipping it would make the backup delete its mirror."""
    out = {}
    for slug in sorted(os.listdir(projects)) if os.path.isdir(projects) else []:
        mem = os.path.join(projects, slug, "memory")
        if os.path.isdir(mem) and any(f.endswith(".md") for _, _, fs in
                                      os.walk(mem, onerror=_raise) for f in fs):
            out[slug] = mem
    return out


def check(projects):
    found = stores(projects)
    bad = 0
    for slug, mem in found.items():
        try:
            lines = store_lines(mem)
        except (OSError, UnicodeDecodeError) as e:
            lines = ["[memória] não foi possível ler o índice: %s" % e]
        if lines:
            bad += 1
            print(slug)
            for line in lines:
                print("    " + line)
    print("%d de %d índices com problema" % (bad, len(found)))
    return 1 if bad else 0


def _git(dest, *args):
    return subprocess.run(["git", "-C", dest, "-c", "user.name=memoria backup",
                           "-c", "user.email=backup@localhost", "-c", "commit.gpgsign=false"]
                          + list(args), capture_output=True, text=True, check=True,
                          env=_git_env()).stdout


def _inside(child, parent):
    child, parent = os.path.realpath(child), os.path.realpath(parent)
    return os.path.commonpath([child, parent]) == parent


MARKER = ".memoria-backup"


def _claim(dest, projects):
    """dest must be empty or this tool's mirror of these same projects; anything else is
    refused before a single file is touched."""
    owner = os.path.realpath(projects)
    marker = os.path.join(dest, MARKER)
    if os.path.isfile(marker):
        if _read(marker).strip() != owner:
            sys.exit("destino %s espelha outra pasta; nada foi tocado" % dest)
        return
    if os.path.isdir(dest) and os.listdir(dest):
        sys.exit("destino %s não está vazio e não é um backup desta ferramenta; nada foi "
                 "tocado" % dest)
    os.makedirs(dest, exist_ok=True)
    with open(marker, "w", encoding="utf-8") as f:
        f.write(owner + "\n")


def _md_only(d, names):
    """copytree filter: keep .md files and folders, never follow a symlink."""
    return [n for n in names if os.path.islink(os.path.join(d, n))
            or not n.endswith(".md") and not os.path.isdir(os.path.join(d, n))]


def _push(dest):
    """'' when pushed or when there is no remote; ', push falhou' otherwise."""
    if not _git(dest, "remote").strip():
        return ""
    try:
        r = subprocess.run(["git", "-C", dest, "push", "-q", "origin", "HEAD"],
                           capture_output=True, text=True, timeout=60, env=_git_env())
    except subprocess.TimeoutExpired:
        return ", push falhou"
    return "" if r.returncode == 0 else ", push falhou"


def backup(projects, dest, exclude=()):
    """'committed' or 'unchanged' (plus ', push falhou'), or 'locked'. Every run pushes when
    the mirror repo has a remote, so a commit whose push failed goes out on the next run.
    Exits without touching dest on unsafe input. exclude: slugs containing any of these
    substrings stay out of the mirror (what was already committed stays in its history)."""
    if _inside(dest, projects) or _inside(projects, dest):
        sys.exit("destino %s e origem %s se sobrepõem; nada foi tocado" % (dest, projects))
    found = {s: m for s, m in stores(projects).items() if not any(x in s for x in exclude)}
    if not found:
        sys.exit("nenhuma memória em %s; nada foi tocado" % projects)
    _claim(dest, projects)
    with open(os.path.join(dest, ".lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "locked"
        if not os.path.isdir(os.path.join(dest, ".git")):
            _git(dest, "init", "-q")
            with open(os.path.join(dest, ".gitignore"), "w") as f:
                f.write(".lock\n")
        for entry in os.listdir(dest):
            if not entry.startswith(".") and entry not in found:
                shutil.rmtree(os.path.join(dest, entry))
        for slug, mem in found.items():
            target = os.path.join(dest, slug)
            shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(mem, target, ignore=_md_only)
        _git(dest, "add", "-A")
        status = "unchanged"
        if _git(dest, "status", "--porcelain").strip():
            _git(dest, "commit", "-q", "-m", "backup " + time.strftime("%Y-%m-%d %H:%M"))
            status = "committed"
        return status + _push(dest)


def main(argv):
    cmd, rest = (argv[0], argv[1:]) if argv else ("", [])
    if cmd == "hook":
        return hook()
    dest = os.environ.get("MEMORIA_BACKUP_DIR") or os.path.expanduser(
        "~/.local/share/claude-memory-backup")
    exclude = []
    while "--dest" in rest or "--exclude" in rest:
        flag = "--dest" if "--dest" in rest else "--exclude"
        i = rest.index(flag)
        if flag == "--dest":
            dest = os.path.expanduser(rest[i + 1])
        else:
            exclude.append(rest[i + 1])
        rest = rest[:i] + rest[i + 2:]
    projects = rest[0] if rest else default_projects()
    if cmd == "check":
        return check(projects)
    if cmd == "backup":
        print(backup(projects, dest, exclude))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
