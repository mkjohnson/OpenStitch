import unittest

from embdmaker.converter import convert_points_to_stitches


class TestConverter(unittest.TestCase):
    def test_converts_points_to_stitches(self):
        commands = convert_points_to_stitches([(0, 0), (4, 2), (5, 2)])
        self.assertEqual(commands, [(0, 0, False), (4, 2, True), (5, 2, True)])

    def test_rejects_invalid_coordinate_types(self):
        with self.assertRaises(TypeError):
            convert_points_to_stitches([(0, "2")])  # type: ignore[arg-type]

    def test_rejects_invalid_point_shape_with_index(self):
        with self.assertRaisesRegex(ValueError, r"index 1"):
            convert_points_to_stitches([(0, 0), (1, 2, 3)])  # type: ignore[list-item]


if __name__ == "__main__":
    unittest.main()
