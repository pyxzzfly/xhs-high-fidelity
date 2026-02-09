import unittest

from PIL import Image

from app.services.harmonize import edge_only_blend


class TestEdgeOnlyBlend(unittest.TestCase):
    def test_preserves_opaque_pixels(self):
        base = Image.new("RGB", (4, 4), (10, 20, 30))
        adj = Image.new("RGB", (4, 4), (200, 210, 220))
        alpha = Image.new("L", (4, 4), 255)  # fully opaque

        out = edge_only_blend(original_rgb=base, adjusted_rgb=adj, alpha_l=alpha, power=1.6)
        self.assertEqual(out.getpixel((0, 0)), base.getpixel((0, 0)))
        self.assertEqual(out.getpixel((3, 3)), base.getpixel((3, 3)))

    def test_uses_adjusted_on_transparent(self):
        base = Image.new("RGB", (4, 4), (10, 20, 30))
        adj = Image.new("RGB", (4, 4), (200, 210, 220))
        alpha = Image.new("L", (4, 4), 0)  # fully transparent

        out = edge_only_blend(original_rgb=base, adjusted_rgb=adj, alpha_l=alpha, power=1.6)
        self.assertEqual(out.getpixel((1, 1)), adj.getpixel((1, 1)))


if __name__ == "__main__":
    unittest.main()

