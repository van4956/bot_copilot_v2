"""Проверяет синтетические истории штатными движками без БД и Telegram."""

import random
import unittest
from datetime import date

from prototypes.quordle.demo_histories import (
    make_quordle_profile,
    make_wordli_profile,
)
from prototypes.quordle.engine import QuordleEngine
from prototypes.wordli.engine import LocalGameEngine


class DemoHistoriesTest(unittest.TestCase):
    """Проверяет очки, воспроизводимость и независимость новых партий."""

    today = date(2026, 11, 22)

    def test_wordli(self) -> None:
        """Проверяет настоящие победы, поражения и неповторяющиеся ответы."""
        engine = LocalGameEngine()
        profile = make_wordli_profile(
            engine, random.Random(42), -1105, self.today
        )
        self.assertEqual(
            profile,
            make_wordli_profile(engine, random.Random(42), -1105, self.today),
        )
        history = profile["history"]
        self.assertTrue(12 <= len(history) <= 30)
        answers = {record["answer"] for record in history}
        self.assertEqual(len(answers), len(history))
        for record in history:
            self.assertLess(record["day"], self.today.isoformat())
            guesses = record["guesses"]
            self.assertEqual(len(set(guesses)), len(guesses))
            self.assertTrue(set(guesses) <= engine._words)
            won = guesses[-1] == record["answer"]
            self.assertTrue(won or len(guesses) == 6)
        stats = engine.statistics(history, self.today)
        self.assertEqual(stats["played"], len(history))
        self.assertTrue(0 < stats["wins"] < stats["played"])
        self.assertGreaterEqual(sum(bool(x) for x in stats["distribution"]), 2)
        self.assertGreater(stats["score"], 0)
        self.assertEqual(profile["dictionary_id"], engine.dictionary_id)
        self.assertNotIn("score", profile)
        state = engine.bootstrap(history, self.today, player_id=-1105)
        self.assertNotIn(state["history"][-1]["answer"], answers)

    def test_quordle(self) -> None:
        """Проверяет колоду, рейтинг и все четыре вида частичного поражения."""
        engine = QuordleEngine()
        profile = make_quordle_profile(engine, random.Random(42), self.today)
        self.assertEqual(
            profile,
            make_quordle_profile(engine, random.Random(42), self.today),
        )
        other = make_quordle_profile(engine, random.Random(77), self.today)
        self.assertNotEqual(other["history"], profile["history"])
        history = profile["history"]
        self.assertTrue(12 <= len(history) <= 30)
        answered = set()
        outcomes = set()
        for record in history:
            self.assertLess(record["day"], self.today.isoformat())
            answers, guesses = set(record["answers"]), record["guesses"]
            self.assertEqual(len(answers), 4)
            self.assertTrue(answered.isdisjoint(answers))
            answered.update(answers)
            self.assertTrue(set(guesses) <= engine._words)
            self.assertEqual(len(set(guesses)), len(guesses))
            found = len(answers.intersection(guesses))
            outcomes.add(found)
            if found < 4:
                self.assertEqual(len(guesses), 9)
            else:
                self.assertTrue(4 <= len(guesses) <= 9)
                self.assertIn(guesses[-1], answers)
        self.assertEqual(outcomes, set(range(5)))
        self.assertTrue(answered.isdisjoint(profile["deck"]))
        self.assertEqual(answered | set(profile["deck"]), set(engine._answers))
        stats = engine.stats(profile, self.today)
        self.assertEqual(stats["played"], len(history))
        self.assertTrue(0 < stats["score"] < 10000)
        self.assertNotIn("score", profile)
        state = engine.bootstrap(profile, self.today)
        self.assertEqual(state["game"]["attempts"], 0)
        self.assertTrue(answered.isdisjoint(profile["history"][-1]["answers"]))
