import unittest

from PIL import Image, ImageDraw

from app.services.mask_utils import (
    bbox_dominance_ratio,
    bbox_from_mask_l,
    make_background_edit_mask,
)


class TestMaskUtils(unittest.TestCase):
    def test_make_background_edit_mask_inverts_and_dilates(self):
        m = Image.new("L", (10, 10), 0)
        draw = ImageDraw.Draw(m)
        draw.rectangle([4, 4, 5, 5], fill=255)  # 2x2 product

        edit = make_background_edit_mask(m, threshold=128, protect_dilate_px=1)

        # background editable (white)
        self.assertEqual(edit.getpixel((0, 0)), 255)
        self.assertEqual(edit.getpixel((2, 2)), 255)

        # product protected (black), including 1px dilation border
        self.assertEqual(edit.getpixel((4, 4)), 0)
        self.assertEqual(edit.getpixel((3, 3)), 0)
        self.assertEqual(edit.getpixel((6, 6)), 0)

    def test_bbox_ratio(self):
        m = Image.new("L", (10, 10), 0)
        draw = ImageDraw.Draw(m)
        draw.rectangle([4, 4, 5, 5], fill=255)

        bbox = bbox_from_mask_l(m, threshold=128)
        self.assertEqual(bbox, (4, 4, 6, 6))
        r = bbox_dominance_ratio(bbox, size=m.size)
        self.assertAlmostEqual(r, 0.2, places=6)


if __name__ == "__main__":
    unittest.main()

