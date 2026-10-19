import unittest
from unittest.mock import patch

from poc.bank_form import PRESETS, render


class BankFormTests(unittest.TestCase):
    def test_renders_without_macos_fonts(self):
        with patch('poc.bank_form.HAND_FONT', '/missing/hand.ttf'), patch('poc.bank_form.PRINT_FONT', '/missing/print.ttf'):
            image = render(PRESETS[0])
        self.assertEqual(image.size, (1700, 2200))


if __name__ == '__main__':
    unittest.main()
