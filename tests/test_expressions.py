from __future__ import annotations

import unittest
from dataclasses import dataclass

from varscope.core.expressions import ExpressionError, resolve_expression
from varscope.core.typesystem import StructField, TypeInfo


@dataclass
class _Var:
    name: str
    address: int
    type_info: TypeInfo


class _Db(dict):
    def lookup_variable(self, name: str):
        return self.get(name)


class ExpressionTests(unittest.TestCase):
    def test_resolve_struct_member(self) -> None:
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
        db = _Db(p=_Var(name="p", address=0x20000000, type_info=point))

        res = resolve_expression("p.y", db)
        self.assertEqual(res.address, 0x20000002)
        self.assertEqual(res.byte_size, 2)

    def test_invalid_member_raises(self) -> None:
        u32 = TypeInfo(kind="base", name="uint32_t", byte_size=4)
        db = _Db(v=_Var(name="v", address=0x20000000, type_info=u32))
        with self.assertRaises(ExpressionError):
            resolve_expression("v.nope", db)


if __name__ == "__main__":
    unittest.main()
