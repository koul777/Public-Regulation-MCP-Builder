from __future__ import annotations

import unittest

from app.processors.answer_profile import _procedure_steps, _sentences


class AnswerProfileProcedureStepsTests(unittest.TestCase):
    def test_procedure_step_containing_a_digit_is_not_dropped(self) -> None:
        sentence = (
            "채용 절차는 다음과 같다. "
            "① 서류심사는 접수 마감일부터 5일 이내에 실시한다 "
            "② 면접은 서류합격자를 대상으로 한다"
        )

        steps = _procedure_steps(sentence)

        self.assertIn("서류심사는 접수 마감일부터 5일 이내에 실시한다", steps)
        self.assertIn("면접은 서류합격자를 대상으로 한다", steps)

    def test_sentences_preserve_korean_spaced_effective_date(self) -> None:
        # "YYYY. M. D." spaced dates must not be shredded into digit fragments
        # when the answer profile splits sentences at ingestion time.
        sentences = _sentences("이 규정은 2025. 1. 1.부터 시행한다.")

        self.assertIn("이 규정은 2025.1.1.부터 시행한다.", sentences)
        self.assertNotIn("1.부터 시행한다.", sentences)


if __name__ == "__main__":
    unittest.main()
