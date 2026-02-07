import unittest

from PIL import Image, ImageDraw, ImageFilter

from app.services.detail_transfer import transfer_high_frequency_details


class TestDetailTransfer(unittest.TestCase):
    def test_transfer_preserves_outside_and_improves_inside(self):
        base = Image.new("RGB", (16, 16), (128, 128, 128))
        draw = ImageDraw.Draw(base)
        draw.rectangle([6, 6, 9, 9], fill=(0, 0, 0))

        out = base.filter(ImageFilter.GaussianBlur(radius=2.0))

        mask = Image.new("L", (16, 16), 0)
        drawm = ImageDraw.Draw(mask)
        drawm.rectangle([4, 4, 11, 11], fill=255)

        outside_before = out.getpixel((0, 0))
        inside_before = out.getpixel((8, 8))[0]

        merged = transfer_high_frequency_details(
            base_rgb=base,
            out_rgb=out,
            product_mask_l=mask,
            alpha=0.5,
            blur_radius=1.5,
            threshold=128,
            inner_erode_px=0,
        )

        self.assertEqual(merged.size, out.size)
        self.assertEqual(merged.getpixel((0, 0)), outside_before)

        inside_after = merged.getpixel((8, 8))[0]
        # Should pull the center darker (closer to base's black square).
        self.assertLess(inside_after, inside_before)


if __name__ == "__main__":
    unittest.main()

