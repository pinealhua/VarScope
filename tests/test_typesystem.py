from __future__ import annotations

import unittest

from varscope.core.typesystem import StructField, TypeInfo


class TypeInfoLeafPathsTests(unittest.TestCase):
    def test_leaf_paths_expand_struct_members(self) -> None:
        u16 = TypeInfo(kind="base", name="uint16_t", byte_size=2)
        point = TypeInfo(
            kind="struct",
            name="Point",
            byte_size=4,
            fields=[
                StructField(name="x", type_info=u16, bit_offset=0),
                StructField(name="y", type_info=u16, bit_offset=16),
            ],
        )

        self.assertEqual(point.leaf_paths("p"), ["p.x", "p.y"])

    def test_leaf_paths_expand_array_of_structs(self) -> None:
        u8 = TypeInfo(kind="base", name="uint8_t", byte_size=1)
        node = TypeInfo(
            kind="struct",
            name="Node",
            byte_size=2,
            fields=[
                StructField(name="a", type_info=u8, bit_offset=0),
                StructField(name="b", type_info=u8, bit_offset=8),
            ],
        )
        arr = TypeInfo(kind="array", name="Node[4]", byte_size=8, element_type=node, array_length=4)

        self.assertEqual(arr.leaf_paths("arr", max_array_items=2), ["arr[0].a", "arr[0].b", "arr[1].a", "arr[1].b"])


if __name__ == "__main__":
    unittest.main()
