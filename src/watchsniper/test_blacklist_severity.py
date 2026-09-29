"""Blacklist severity: every rule declares one, and the corpus agrees with it.

Hermetic. The rule file and corpus are the repository's own; the load-path
tests write throwaway TOML to a temporary directory.
"""

from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path

from . import config as C
from .blacklist import FLAG, HARD, SEVERITIES, Blacklist, Hit, flag_hits, hard_hits
from .selftest import CORPUS


def _strongest(hits: list[Hit]) -> str:
    return HARD if hard_hits(hits) else FLAG


def _load(toml: str) -> Blacklist:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "blacklist.toml"
        path.write_text(toml, encoding="utf-8")
        return Blacklist.load(path)


class TestRuleFile(unittest.TestCase):
    def test_every_rule_declares_a_valid_severity(self):
        raw = tomllib.loads(C.BLACKLIST_PATH.read_text(encoding="utf-8"))
        self.assertTrue(raw["rule"])
        for r in raw["rule"]:
            # Explicit, not defaulted: the fail-safe is for mistakes, not policy.
            self.assertIn("severity", r, r["id"])
            self.assertIn(r["severity"], SEVERITIES, r["id"])

    def test_loaded_rules_carry_the_declared_severity(self):
        raw = tomllib.loads(C.BLACKLIST_PATH.read_text(encoding="utf-8"))
        declared = {r["id"]: r["severity"] for r in raw["rule"]}
        loaded = {r.id: r.severity for r in Blacklist.load().rules}
        self.assertEqual(loaded, declared)


class TestCorpusSeverity(unittest.TestCase):
    def test_every_drop_case_states_its_severity(self):
        cases = tomllib.loads(CORPUS.read_text(encoding="utf-8"))["case"]
        for case in cases:
            if case["drop"]:
                self.assertIn(case.get("severity"), SEVERITIES, case["title"])

    def test_strongest_severity_matches_the_corpus(self):
        bl = Blacklist.load()
        cases = tomllib.loads(CORPUS.read_text(encoding="utf-8"))["case"]
        failures = []
        for case in cases:
            if not case["drop"]:
                continue
            hits = bl.check(case["title"])
            if not hits:
                continue  # TestBlacklistCorpus reports the missed drop
            got = _strongest(hits)
            if got != case["severity"]:
                failures.append(
                    f"{case['title']!r}: expected {case['severity']}, got {got} "
                    f"from {[(h.rule_id, h.severity) for h in hits]}"
                )
        self.assertEqual(failures, [], "\n".join(failures))

    def test_corpus_exercises_both_severities(self):
        cases = tomllib.loads(CORPUS.read_text(encoding="utf-8"))["case"]
        seen = {c["severity"] for c in cases if c["drop"]}
        self.assertEqual(seen, set(SEVERITIES))


class TestLoading(unittest.TestCase):
    def test_missing_severity_loads_as_hard(self):
        bl = _load('[[rule]]\nid = "x"\npattern = "\\\\bx\\\\b"\n')
        self.assertEqual(bl.rules[0].severity, HARD)
        self.assertEqual(bl.check("an x here")[0].severity, HARD)

    def test_declared_flag_is_carried_onto_the_hit(self):
        bl = _load('[[rule]]\nid = "x"\npattern = "\\\\bx\\\\b"\nseverity = "flag"\n')
        self.assertEqual(bl.check("an x here")[0].severity, FLAG)

    def test_unknown_severity_refuses_to_load(self):
        with self.assertRaises(ValueError):
            _load('[[rule]]\nid = "x"\npattern = "x"\nseverity = "soft"\n')


class TestSplit(unittest.TestCase):
    def test_hard_and_flag_hits_partition(self):
        hits = [
            Hit(rule_id="a", matched="a", context="a", severity=HARD),
            Hit(rule_id="b", matched="b", context="b", severity=FLAG),
            Hit(rule_id="c", matched="c", context="c", severity=HARD),
        ]
        self.assertEqual([h.rule_id for h in hard_hits(hits)], ["a", "c"])
        self.assertEqual([h.rule_id for h in flag_hits(hits)], ["b"])
        self.assertEqual(hard_hits([]), [])
        self.assertEqual(flag_hits([]), [])

    def test_mixed_title_is_strongest_hard(self):
        hits = Blacklist.load().check("Seiko replica - spares or repairs")
        self.assertTrue(hard_hits(hits))
        self.assertTrue(flag_hits(hits))
        self.assertEqual(_strongest(hits), HARD)


if __name__ == "__main__":
    unittest.main()
