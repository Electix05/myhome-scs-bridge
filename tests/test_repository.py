"""What may leave this machine: the allowlist in .gitignore and what it lets pass.

The repository is public, and whatever was pushed once can stay in forks and
caches for good.  So .gitignore hides everything and names the few things that
may go up.  These tests keep that list from growing by accident and look through
every file it lets pass for things that have no place in public: real addresses,
host names, mail addresses, paths of a development machine, access tokens.

The search works without git, because it also runs inside the add-on image, which
does not have it.  Where git is available, the list is compared with what git
really takes.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import os
import re
import shutil
import subprocess
import unittest

import yaml

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW_DIR = os.path.join(PROJECT_DIR, ".github", "workflows")

ALLOWED_ENTRIES = {
    ".github/",
    ".gitignore",
    "KOMPATIBILITAET.md",
    "README.md",
    "myhome_scs_mqtt/",
    "repository.yaml",
    "testgeraet/",
    "tests/",
}
# Names that would describe a real installation, wherever they turn up - with the
# variants a second installation or a backup ends up with.
INSTALLATION_FILES = (
    "devices.yaml", "devices.yml", "devices_haus2.yaml", "discovered.json", "discovered_2.json",
    "options.json", "options_2.json", "scan_result.yaml", "scan_result_2.yaml", "bridge.log",
    "backup.tar", "backup.tar.gz", "backup.tgz", "backup.zip", ".env", "server.pem", "server.key",
)
# What must stay on this machine, whether or not it exists right now.
LOCAL_ONLY = ("berichte/x.md", "lauf-99.txt", "ARBEITSSTAND.md", "ARCHITEKTUR.md", ".arbeit/x",
              ".venv/x", "neue-messung.txt")

# No digit or dot next to it: "scan_<address>.json" is an address, "1.2.3.4.5" is not.
IPV4 = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?!\.?\d)")
ALLOWED_V4_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "192.0.2.0/24",     # the three networks reserved for documentation
        "198.51.100.0/24",
        "203.0.113.0/24",
        "127.0.0.0/8",      # loopback
        "0.0.0.0/32",       # "listen on every interface"
        "172.30.32.0/24",   # the Supervisor network, the same on every installation
    )
)
# Looks like an address, but is the firmware version tests/test_probe.py expects.
NOT_AN_ADDRESS = {"2.15.0.4"}
# Deliberately rough: ipaddress decides what really is an address.
IPV6 = re.compile(r"(?<![0-9A-Za-z:])[0-9A-Fa-f:]*:[0-9A-Fa-f:]*:[0-9A-Fa-f:.]*(?![0-9A-Za-z:])")
DOCUMENTATION_V6 = ipaddress.ip_network("2001:db8::/32")
MAC = re.compile(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b")
# A top level domain made of letters: "actions/checkout@v7.0.1" is no mail address.
MAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
ALLOWED_MAIL = re.compile(r"(?:.*@users\.noreply\.github\.com|noreply@anthropic\.com|git@github\.com)")
# "homeassistant.local" is the default name of every installation and says nothing.
PRIVATE_HOST = re.compile(r"\b(?!homeassistant\.local\b)[\w-]+"
                          r"\.(?:local|lan|duckdns\.org|ui\.nabu\.casa|myfritz\.net)\b")
# Put together at run time, so that this file does not report itself.
HOME_PATH = re.compile("(?:/Us" + "ers/|/ho" + "me/[A-Za-z]|[A-Za-z]:" + r"\\Us" + r"ers\\)")
TOKEN = re.compile("|".join(("gh" + "[opsur]_[A-Za-z0-9]{8,}",
                             "github" + "_pat_[A-Za-z0-9_]{8,}",
                             "ey" + "J[A-Za-z0-9_-]{20,}",
                             "-----" + "BEGIN [A-Z ]*PRIVATE KEY-----")))

# A full version or a commit: "v2" moves along with every release.
PINNED_REF = re.compile(r"^(?:v?\d+\.\d+\.\d+|[0-9a-f]{40})$")
USES = re.compile(r"""^\s*(?:-\s+)?uses:\s*["']?([^\s"'#]+)""", re.MULTILINE)


def gitignore_rules() -> list[str]:
    """The rules, read the way git reads them.

    Git takes spaces and a carriage return as part of the pattern, so " /*" would
    hide nothing.  strip() would make such a typo look fine - it is an error here.
    """
    with open(os.path.join(PROJECT_DIR, ".gitignore"), encoding="utf-8", newline="") as handle:
        lines = handle.read().split("\n")
    rules = []
    for number, line in enumerate(lines, 1):
        if line != line.strip():
            raise ValueError(f".gitignore line {number}: white space at the edge")
        if line and not line.startswith("#"):
            rules.append(line)
    return rules


def ignore_patterns() -> list[str]:
    """The rules that apply inside the allowed folders."""
    return [rule for rule in gitignore_rules() if not rule.startswith(("!", "/"))]


def _is_ignored(name: str, is_dir: bool, patterns: list[str]) -> bool:
    for pattern in patterns:
        if pattern.endswith("/"):
            if is_dir and fnmatch.fnmatchcase(name, pattern[:-1]):
                return True
        elif fnmatch.fnmatchcase(name, pattern):
            return True
    return False


def published_files() -> list[str]:
    """The files .gitignore lets pass, relative to the project folder."""
    rules = gitignore_rules()
    entries = [rule[2:] for rule in rules if rule.startswith("!/")]
    patterns = ignore_patterns()
    published = []
    for entry in entries:
        path = os.path.join(PROJECT_DIR, entry.rstrip("/"))
        if os.path.isfile(path):
            published.append(entry)
        for root, folders, files in os.walk(path):
            folders[:] = [name for name in folders if not _is_ignored(name, True, patterns)]
            published.extend(
                os.path.relpath(os.path.join(root, name), PROJECT_DIR)
                for name in files if not _is_ignored(name, False, patterns)
            )
    return sorted(published)


def _is_allowed_v4(candidate: str) -> bool:
    try:
        # Padded octets are still an address, but ipaddress refuses them.
        address = ipaddress.ip_address(".".join(str(int(part)) for part in candidate.split(".")))
    except ValueError:
        return True         # an octet above 255: a version number, no address
    return any(address in network for network in ALLOWED_V4_NETWORKS)


def _is_allowed_v6(candidate: str) -> bool:
    try:
        address = ipaddress.IPv6Address(candidate)
    except ValueError:
        return True         # "12:34:56" and the like
    return address.is_loopback or address.is_unspecified or address in DOCUMENTATION_V6


def findings(text: str) -> list[str]:
    """Everything in ``text`` that has no place in a public repository."""
    found = [f"address {candidate}" for candidate in IPV4.findall(text)
             if candidate not in NOT_AN_ADDRESS and not _is_allowed_v4(candidate)]
    found.extend(f"address {candidate}" for candidate in IPV6.findall(text)
                 if not _is_allowed_v6(candidate))
    found.extend(f"mail address {match}" for match in MAIL.findall(text)
                 if not ALLOWED_MAIL.fullmatch(match))
    if MAC.search(text):
        found.append("hardware address")
    if PRIVATE_HOST.search(text):
        found.append("private host name")
    if HOME_PATH.search(text):
        found.append("path of a development machine")
    if TOKEN.search(text):
        found.append("access token")
    return found


def workflow_files() -> list[str]:
    if not os.path.isdir(WORKFLOW_DIR):
        return []
    return sorted(os.path.join(WORKFLOW_DIR, name) for name in os.listdir(WORKFLOW_DIR)
                  if name.endswith((".yaml", ".yml")))


def read_text(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class TestAllowlist(unittest.TestCase):
    def test_everything_is_hidden_unless_it_is_named(self):
        rules = gitignore_rules()
        self.assertEqual(rules[0], "/*")
        named = {rule[2:] for rule in rules if rule.startswith("!")}
        self.assertEqual(named, ALLOWED_ENTRIES)
        # The exceptions are all anchored: "!foo" would reach into every folder.
        self.assertEqual([rule for rule in rules
                          if rule.startswith("!") and not rule.startswith("!/")], [])

    def test_installation_data_stays_hidden_inside_the_allowed_folders(self):
        patterns = ignore_patterns()
        for name in INSTALLATION_FILES:
            with self.subTest(name=name):
                self.assertTrue(_is_ignored(name, False, patterns), name)

    def test_nothing_the_add_on_needs_is_hidden_by_a_pattern(self):
        published = published_files()
        for name in ("CHANGELOG.md", "DOCS.md", "Dockerfile", "README.md", "config.yaml",
                     "requirements.txt", "run.sh", os.path.join("translations", "de.yaml"),
                     os.path.join("translations", "en.yaml"),
                     os.path.join("app", "__main__.py"),
                     os.path.join("app", "devices", "light.py")):
            with self.subTest(name=name):
                self.assertIn(os.path.join("myhome_scs_mqtt", name), published)

    def test_no_second_gitignore_can_reopen_the_list(self):
        self.assertEqual([path for path in published_files()
                          if os.path.basename(path) == ".gitignore" and path != ".gitignore"], [])


@unittest.skipUnless(shutil.which("git") and os.path.exists(os.path.join(PROJECT_DIR, ".git")),
                     "needs git and a repository (neither is in the add-on image)")
class TestAgainstGit(unittest.TestCase):
    """The ground truth - the search above only looks at what it thinks git takes."""

    def test_git_takes_nothing_the_search_does_not_see(self):
        result = subprocess.run(
            ["git", "-c", "core.quotePath=false", "ls-files", "--cached", "--others",
             "--exclude-standard", "-z"],
            cwd=PROJECT_DIR, capture_output=True, check=True, timeout=60)
        taken_by_git = {name for name in result.stdout.decode("utf-8").split("\0") if name}
        searched = {path.replace(os.sep, "/") for path in published_files()}
        self.assertEqual(sorted(taken_by_git - searched), [])

    def test_git_really_hides_what_stays_local(self):
        hidden = list(LOCAL_ONLY)
        for folder in ("tests", "myhome_scs_mqtt", "myhome_scs_mqtt/app"):
            hidden.extend(f"{folder}/{name}" for name in INSTALLATION_FILES)
        for path in hidden:
            with self.subTest(path=path):
                result = subprocess.run(["git", "check-ignore", "-q", "--no-index", path],
                                        cwd=PROJECT_DIR, check=False, timeout=60)
                self.assertEqual(result.returncode, 0, f"git would upload {path}")


class TestNothingPrivateGoesPublic(unittest.TestCase):
    def test_no_published_file_holds_anything_private(self):
        problems = {}
        for path in published_files():
            with open(os.path.join(PROJECT_DIR, path), "rb") as handle:
                # No file is skipped: one stray byte must not hide the rest of it,
                # and an image keeps author and path in readable text.
                text = handle.read().decode("utf-8", errors="replace")
            found = findings(text) + [f"in the file name: {item}" for item in findings(path)]
            if found:
                problems[path] = found
        self.assertEqual(problems, {})

    def test_the_check_really_reports_what_it_is_there_for(self):
        """A check that never fires looks exactly like a clean repository.

        Each sample names what it has to trigger, so that one pattern cannot hide
        behind the hit of another.
        """
        private = ".".join(("192", "168", "1", "20"))
        for sample, expected in (
            ("gateway " + private, "address"),
            ("file scan_" + private + ".json", "address"),
            ("gateway " + ".".join(("192", "168", "001", "020")), "address"),
            ("gateway " + ".".join(("10", "0", "0", "5")), "address"),
            ("broker " + ".".join(("8", "8", "8", "8")), "address"),
            ("gateway fd12:3456" + "::1", "address"),
            ("office" + "@" + "example.com", "mail address"),
            ("mac " + ":".join(("aa", "bb", "cc", "dd", "ee", "ff")), "hardware address"),
            ("mac " + "-".join(("aa", "bb", "cc", "dd", "ee", "ff")), "hardware address"),
            ("host gateway." + "lan", "private host name"),
            ("host haus." + "duckdns.org", "private host name"),
            ("host abc." + "ui.nabu.casa", "private host name"),
            ("cd /Us" + "ers/somebody/project", "path of a development machine"),
            ("cd /ho" + "me/somebody/project", "path of a development machine"),
            ("cd C:" + "\\Us" + "ers\\somebody", "path of a development machine"),
            ("token " + "gh" + "p_abcdefgh12345678", "access token"),
            ("token " + "gh" + "s_abcdefgh12345678", "access token"),
            ("token " + "github" + "_pat_abcdefgh12345678", "access token"),
            ("token " + "ey" + "JhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", "access token"),
            ("-----" + "BEGIN RSA PRIVATE KEY-----", "access token"),
        ):
            with self.subTest(sample=sample):
                self.assertIn(expected, " ".join(findings(sample)))

    def test_the_check_leaves_ordinary_text_alone(self):
        for sample in (
            "gateway_host: 192.0.2.10",
            "127.0.0.1 and 0.0.0.0",
            "the ingress proxy is 172.30.32.2",
            "uses: actions/checkout@v7.0.1",
            "uses: home-assistant/builder/actions/build-image@2026.09.0",
            "999.1.1.1 is no address",
            "Firmware: 2.15.0.4",
            "python 3.12.14 and v1.2.3",
            "version 1.2.3.4.5",
            "*#1*0211*12*243*96*100##",
            "16:59:55.232 INFO",
            "12:34:56",
            "::1 and 2001:db8::1",
            "http://homeassistant.local:8123",
            "/homeassistant/config",
            "12345678+name" + "@" + "users.noreply.github.com",
            "Co-Authored-By: Claude <noreply" + "@" + "anthropic.com>",
            "ghp_ on its own",
        ):
            with self.subTest(sample=sample):
                self.assertEqual(findings(sample), [])


class TestWorkflows(unittest.TestCase):
    def test_there_are_workflows_to_check(self):
        self.assertTrue(workflow_files(), "the checks below would pass on nothing")

    def test_every_workflow_starts_with_read_access_only(self):
        """A job that needs more has to say so itself."""
        for path in workflow_files():
            with self.subTest(path=path):
                self.assertEqual(yaml.safe_load(read_text(path)).get("permissions"),
                                 {"contents": "read"})

    def test_every_action_is_pinned_to_a_release(self):
        """A branch can change under our feet - the Home Assistant docs ask for a release."""
        for path in workflow_files():
            used = USES.findall(read_text(path))
            self.assertTrue(used, path)
            for action in used:
                if action.startswith("./"):
                    continue
                with self.subTest(path=path, action=action):
                    self.assertIn("@", action)
                    self.assertRegex(action.rsplit("@", 1)[1], PINNED_REF)

    def test_the_pin_check_tells_a_release_from_a_branch(self):
        for ref in ("v7.0.1", "2026.09.0", "0123456789abcdef0123456789abcdef01234567"):
            with self.subTest(ref=ref):
                self.assertRegex(ref, PINNED_REF)
        for ref in ("master", "main", "latest", "HEAD", "v2"):
            with self.subTest(ref=ref):
                self.assertNotRegex(ref, PINNED_REF)

    def test_dependency_updates_go_to_the_development_branch(self):
        """Proposals are tested first; "main" is what installations see."""
        config = yaml.safe_load(read_text(os.path.join(PROJECT_DIR, ".github", "dependabot.yml")))
        updates = config["updates"]
        self.assertEqual({entry["package-ecosystem"] for entry in updates},
                         {"pip", "docker", "docker-compose", "github-actions"})
        for entry in updates:
            with self.subTest(ecosystem=entry["package-ecosystem"]):
                self.assertEqual(entry["target-branch"], "entwicklung")
                self.assertEqual(entry["schedule"]["interval"], "monthly")


if __name__ == "__main__":
    unittest.main()
