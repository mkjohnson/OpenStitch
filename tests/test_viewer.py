import unittest

from embdmaker.viewer import stitch_bounds


class TestViewer(unittest.TestCase):
    def test_returns_bounds_for_stitches(self):
        bounds = stitch_bounds([(3, 4, False), (-1, 8, True), (5, -2, True)])
        self.assertEqual(bounds, (-1, -2, 5, 8))

    def test_returns_none_for_empty_input(self):
        self.assertIsNone(stitch_bounds([]))


if __name__ == "__main__":
    unittest.main()
