import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memoria  # noqa: E402

SCRIPT = os.path.join(HERE, "memoria.py")
GIT_REPO_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                 "GIT_COMMON_DIR", "GIT_PREFIX")
for _var in GIT_REPO_VARS:  # hermetic: a git hook running these tests exports its own repo
    os.environ.pop(_var, None)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


class Tree(unittest.TestCase):
    """A fake ~/.claude/projects with one store per test."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.projects = os.path.join(self.root, "projects")
        os.makedirs(self.projects)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def store(self, slug, index, files=()):
        mem = os.path.join(self.projects, slug, "memory")
        write(os.path.join(mem, "MEMORY.md"), index)
        for name in files:
            write(os.path.join(mem, name), "---\nname: x\n---\nbody\n")
        return mem

    def payload(self, slug):
        return {"transcript_path": os.path.join(self.projects, slug, "s.jsonl"),
                "cwd": "/nowhere"}


class MeasureTest(unittest.TestCase):
    """The loader's cut rule, one case per defect the upstream probe documents."""

    CASES = [
        ("199 lines", "x\n" * 199, None),
        ("200 lines with newline", "x\n" * 200, None),
        ("200 lines without newline", "x\n" * 199 + "x", None),
        ("201 lines", "x\n" * 201, "line"),
        ("one giant line", "y" * 30000 + "\n", "size"),
        ("empty", "", None),
        ("CRLF over the cap", "\r\n".join("x" * 165 for _ in range(150)), "size"),
        ("same file in LF", "\n".join("x" * 165 for _ in range(150)), None),
        ("blank lines at the end", "x\n" * 150 + "\n" * 60 + "   \n", None),
        ("exactly 25000 units", "\n".join(["x" * 199] * 124 + ["x" * 200]), None),
        ("25001 units", "\n".join(["x" * 199] * 124 + ["x" * 201]), "size"),
    ]

    def test_cut_rule(self):
        d = tempfile.mkdtemp()
        try:
            for name, text, edge in self.CASES:
                p = os.path.join(d, "MEMORY.md")
                write(p, text)
                r = memoria.measure(p)
                with self.subTest(name):
                    if edge is None:
                        self.assertTrue(r.startswith("no cut"), r)
                    else:
                        self.assertTrue(r.startswith(edge), r)
        finally:
            shutil.rmtree(d)


class IndexProblemsTest(Tree):
    def test_clean_index_has_no_problems(self):
        mem = self.store("p", "- [A](a.md) — a\n", ["a.md"])
        self.assertEqual(memoria.index_problems(mem), ([], []))

    def test_broken_link_is_reported(self):
        mem = self.store("p", "- [A](a.md) — a\n- [B](gone.md) — b\n", ["a.md"])
        self.assertEqual(memoria.index_problems(mem), (["gone.md"], []))

    def test_file_missing_from_index_is_orphan(self):
        mem = self.store("p", "- [A](a.md) — a\n", ["a.md", "lost.md"])
        self.assertEqual(memoria.index_problems(mem), ([], ["lost.md"]))

    def test_dot_slash_link_counts_as_cited(self):
        mem = self.store("p", "- [A](./a.md) — a\n", ["a.md"])
        self.assertEqual(memoria.index_problems(mem), ([], []))


class HookLinesTest(Tree):
    def test_healthy_store_is_silent(self):
        self.store("p", "- [A](a.md) — a\n", ["a.md"])
        self.assertEqual(memoria.hook_lines(self.payload("p")), [])

    def test_missing_store_is_silent(self):
        os.makedirs(os.path.join(self.projects, "new"))
        self.assertEqual(memoria.hook_lines(self.payload("new")), [])

    def test_truncated_index_warns(self):
        self.store("p", "x\n" * 201)
        out = " ".join(memoria.hook_lines(self.payload("p")))
        self.assertIn("TRUNCADO", out)

    def test_index_near_cap_warns_without_truncation(self):
        self.store("p", "x\n" * 195)
        out = " ".join(memoria.hook_lines(self.payload("p")))
        self.assertIn("folga", out)
        self.assertNotIn("TRUNCADO", out)

    def test_orphan_and_broken_are_named(self):
        self.store("p", "- [B](gone.md) — b\n", ["lost.md"])
        out = " ".join(memoria.hook_lines(self.payload("p")))
        self.assertIn("gone.md", out)
        self.assertIn("lost.md", out)

    def test_files_without_any_index_warn(self):
        mem = os.path.join(self.projects, "p", "memory")
        write(os.path.join(mem, "a.md"), "x\n")
        out = " ".join(memoria.hook_lines(self.payload("p")))
        self.assertIn("a.md", out)

    def test_worktree_session_reads_the_main_repo_store(self):
        main = os.path.join(self.root, "repo")
        subprocess.run(["git", "init", "-q", main], check=True)
        subprocess.run(["git", "-C", main, "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-q", "--allow-empty", "-m", "x"], check=True)
        wt = os.path.join(main, ".claude", "worktrees", "wt1")
        subprocess.run(["git", "-C", main, "worktree", "add", "-q", wt], check=True)
        main_slug = memoria.slug(os.path.realpath(main))
        self.store(main_slug, "x\n" * 201)
        payload = {"cwd": wt, "transcript_path": os.path.join(
            self.projects, memoria.slug(os.path.realpath(wt)), "s.jsonl")}
        self.assertIn("TRUNCADO", " ".join(memoria.hook_lines(payload)))

    def test_slug_replaces_every_non_alphanumeric(self):
        self.assertEqual(memoria.slug("/srv/My Repo/.claude/x_y&z"),
                         "-srv-My-Repo--claude-x-y-z")

    def test_store_comes_from_transcript_path_not_cwd(self):
        self.store("p", "x\n" * 201)
        payload = self.payload("p")
        payload["cwd"] = os.path.join(self.root, "elsewhere")
        self.assertTrue(memoria.hook_lines(payload))


class HookCommandTest(Tree):
    def run_hook(self, stdin):
        return subprocess.run([sys.executable, SCRIPT, "hook"], input=stdin,
                              capture_output=True, text=True, timeout=30)

    def test_garbage_stdin_exits_zero_and_silent(self):
        r = self.run_hook("not json")
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_hook_speaks_through_stdout(self):
        self.store("p", "x\n" * 201)
        r = self.run_hook(json.dumps(self.payload("p")))
        self.assertEqual(r.returncode, 0)
        self.assertIn("TRUNCADO", r.stdout)


class CheckTest(Tree):
    def run_check(self):
        return subprocess.run([sys.executable, SCRIPT, "check", self.projects],
                              capture_output=True, text=True, timeout=30)

    def test_all_healthy_exits_zero(self):
        self.store("a", "- [A](a.md) — a\n", ["a.md"])
        self.assertEqual(self.run_check().returncode, 0)

    def test_unreadable_index_is_reported_and_audit_goes_on(self):
        mem = os.path.join(self.projects, "a-bad", "memory")
        os.makedirs(mem)
        with open(os.path.join(mem, "MEMORY.md"), "wb") as f:
            f.write(b"\xff\xfe broken\n")
        self.store("z-trunc", "x\n" * 201)
        r = self.run_check()
        self.assertEqual(r.returncode, 1)
        self.assertIn("a-bad", r.stdout)
        self.assertIn("z-trunc", r.stdout)

    def test_any_problem_exits_one_and_names_the_store(self):
        self.store("a", "- [A](a.md) — a\n", ["a.md"])
        self.store("bad", "x\n" * 201)
        r = self.run_check()
        self.assertEqual(r.returncode, 1)
        self.assertIn("bad", r.stdout)


class BackupTest(Tree):
    def setUp(self):
        super().setUp()
        self.dest = os.path.join(self.root, "backup")

    def git(self, *args):
        return subprocess.run(["git", "-C", self.dest] + list(args),
                              capture_output=True, text=True).stdout

    def commits(self):
        return int(self.git("rev-list", "--count", "HEAD").strip() or 0)

    def test_first_run_mirrors_and_commits(self):
        self.store("p", "- [A](a.md) — a\n", ["a.md"])
        self.assertEqual(memoria.backup(self.projects, self.dest), "committed")
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "p", "a.md")))
        self.assertEqual(self.commits(), 1)

    def test_second_run_without_changes_does_not_commit(self):
        self.store("p", "- [A](a.md) — a\n", ["a.md"])
        memoria.backup(self.projects, self.dest)
        self.assertEqual(memoria.backup(self.projects, self.dest), "unchanged")
        self.assertEqual(self.commits(), 1)

    def test_deleted_memory_leaves_the_mirror_but_stays_in_history(self):
        mem = self.store("p", "- [A](a.md) — a\n", ["a.md", "b.md"])
        memoria.backup(self.projects, self.dest)
        os.remove(os.path.join(mem, "b.md"))
        self.assertEqual(memoria.backup(self.projects, self.dest), "committed")
        self.assertFalse(os.path.exists(os.path.join(self.dest, "p", "b.md")))
        self.assertIn("b.md", self.git("log", "--all", "--name-only"))

    def test_no_store_found_refuses_and_touches_nothing(self):
        with self.assertRaises(SystemExit):
            memoria.backup(self.projects, self.dest)
        self.assertFalse(os.path.exists(self.dest))

    def test_dest_inside_source_is_refused_and_source_survives(self):
        mem = self.store("p", "- [A](a.md) — a\n", ["a.md"])
        with self.assertRaises(SystemExit):
            memoria.backup(self.projects, self.projects)
        self.assertTrue(os.path.isfile(os.path.join(mem, "a.md")))

    def test_store_without_index_is_backed_up(self):
        mem = os.path.join(self.projects, "noidx", "memory")
        write(os.path.join(mem, "a.md"), "x\n")
        memoria.backup(self.projects, self.dest)
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "noidx", "a.md")))

    def bare_remote(self):
        remote = os.path.join(self.root, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
        return remote

    def test_commit_is_pushed_when_origin_exists(self):
        self.store("p", "- [A](a.md) — a\n", ["a.md"])
        memoria.backup(self.projects, self.dest)
        remote = self.bare_remote()
        self.git("remote", "add", "origin", remote)
        write(os.path.join(self.projects, "p", "memory", "b.md"), "new\n")
        self.assertEqual(memoria.backup(self.projects, self.dest), "committed")
        log = subprocess.run(["git", "-C", remote, "log", "--all", "--name-only"],
                             capture_output=True, text=True).stdout
        self.assertIn("b.md", log)

    def test_failed_push_keeps_the_local_commit(self):
        self.store("p", "- [A](a.md) — a\n", ["a.md"])
        memoria.backup(self.projects, self.dest)
        self.git("remote", "add", "origin", os.path.join(self.root, "missing.git"))
        write(os.path.join(self.projects, "p", "memory", "b.md"), "new\n")
        self.assertEqual(memoria.backup(self.projects, self.dest), "committed, push falhou")
        self.assertEqual(self.commits(), 2)

    def test_excluded_slug_is_not_mirrored(self):
        self.store("-work-client", "- [A](a.md) — a\n", ["a.md"])
        self.store("-home-mine", "- [A](a.md) — a\n", ["a.md"])
        memoria.backup(self.projects, self.dest, exclude=["client"])
        self.assertFalse(os.path.exists(os.path.join(self.dest, "-work-client")))
        self.assertTrue(os.path.exists(os.path.join(self.dest, "-home-mine")))

    def test_foreign_non_empty_dest_is_refused_and_kept(self):
        self.store("p", "- [A](a.md) — a\n", ["a.md"])
        write(os.path.join(self.dest, "keep", "file.txt"), "mine\n")
        with self.assertRaises(SystemExit):
            memoria.backup(self.projects, self.dest)
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "keep", "file.txt")))

    def test_mirror_of_other_projects_is_refused_and_kept(self):
        self.store("p", "- [A](a.md) — a\n", ["a.md"])
        memoria.backup(self.projects, self.dest)
        other = os.path.join(self.root, "other-projects")
        write(os.path.join(other, "q", "memory", "MEMORY.md"), "- [B](b.md) — b\n")
        with self.assertRaises(SystemExit):
            memoria.backup(other, self.dest)
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "p", "a.md")))

    def test_unreadable_store_aborts_and_keeps_its_mirror(self):
        mem = self.store("p", "- [A](a.md) — a\n", ["a.md"])
        self.store("q", "- [B](b.md) — b\n", ["b.md"])
        memoria.backup(self.projects, self.dest)
        os.chmod(mem, 0)
        try:
            with self.assertRaises(OSError):
                memoria.backup(self.projects, self.dest)
        finally:
            os.chmod(mem, 0o755)
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "p", "a.md")))

    def test_symlink_in_memory_is_not_followed(self):
        mem = self.store("p", "- [A](a.md) — a\n", ["a.md"])
        secret = os.path.join(self.root, "secret.md")
        write(secret, "outside\n")
        os.symlink(secret, os.path.join(mem, "link.md"))
        memoria.backup(self.projects, self.dest)
        self.assertFalse(os.path.lexists(os.path.join(self.dest, "p", "link.md")))

    def test_pending_commit_is_pushed_on_a_run_without_changes(self):
        self.store("p", "- [A](a.md) — a\n", ["a.md"])
        memoria.backup(self.projects, self.dest)
        self.git("remote", "add", "origin", os.path.join(self.root, "missing.git"))
        write(os.path.join(self.projects, "p", "memory", "b.md"), "new\n")
        memoria.backup(self.projects, self.dest)
        remote = self.bare_remote()
        self.git("remote", "set-url", "origin", remote)
        self.assertEqual(memoria.backup(self.projects, self.dest), "unchanged")
        log = subprocess.run(["git", "-C", remote, "log", "--all", "--name-only"],
                             capture_output=True, text=True).stdout
        self.assertIn("b.md", log)

    def test_inherited_git_repo_variables_are_ignored(self):
        self.store("p", "- [A](a.md) — a\n", ["a.md"])
        other = os.path.join(self.root, "other")
        subprocess.run(["git", "init", "-q", other], check=True)
        os.environ["GIT_DIR"] = os.path.join(other, ".git")
        os.environ["GIT_INDEX_FILE"] = os.path.join(other, ".git", "index")
        try:
            self.assertEqual(memoria.backup(self.projects, self.dest), "committed")
        finally:
            os.environ.pop("GIT_DIR")
            os.environ.pop("GIT_INDEX_FILE")
        self.assertEqual(self.commits(), 1)

    def test_concurrent_run_is_skipped(self):
        import fcntl
        self.store("p", "- [A](a.md) — a\n", ["a.md"])
        memoria.backup(self.projects, self.dest)
        with open(os.path.join(self.dest, ".lock"), "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertEqual(memoria.backup(self.projects, self.dest), "locked")


if __name__ == "__main__":
    unittest.main()
