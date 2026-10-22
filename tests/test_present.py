"""present: the agent's layout, formatted by the server and checked against the data."""
from __future__ import annotations

from app.agent import present


def test_numbers_are_formatted_by_unit():
    assert present.show(259618.06, "usd") == "USD 259.618,06"
    assert present.show(68.84, "pct") == "68,8%"
    assert present.show(27.33, "bps") == "27,3 pb"
    assert present.show(12139, "count") == "12.139"
    assert present.show(0.99917, "rate") == "0,99917"


def test_every_number_must_be_backed_by_a_tool_result():
    known = present.numbers_in({"rows": [[965, 89529598.68, 259618.06]]})
    spec = {"title": "T", "kpis": [{"label": "Ops", "value": 965, "unit": "count"}],
            "charts": [{"title": "c", "type": "bar", "labels": ["a", "b"], "values": [259618.06, 1.5]}],
            "notes": [{"text": "En 2025 hubo 965 operaciones y 31 clientes, con margen de 33,3 pb."}]}
    assert set(present.unbacked(spec, known)) == {1.5, 33.3}   # days/years/small counts pass


def test_text_numbers_in_either_convention_are_read():
    assert 259618.06 in present.numbers_in("USD 259.618,06")
    assert 259618.06 in present.numbers_in("259,618.06")


def test_build_turns_the_layout_into_blocks():
    out = present.build({"title": "Tablero", "subtitle": "Oct 2025",
                         "kpis": [{"label": "Volumen", "value": 1000, "unit": "usd"}],
                         "charts": [{"title": "Por día", "type": "bar", "labels": ["1", "2"],
                                     "values": [1, 2], "unit": "usd"}],
                         "tables": [{"title": "Proveedores", "columns": ["Proveedor", "Costo"],
                                     "units": [None, "usd"], "rows": [["Reap", 12.5]]}],
                         "notes": [{"text": "Faltan gastos.", "tone": "warning"}]})
    assert [b["type"] for b in out] == ["heading", "kpis", "chart", "table", "notice"]
    assert out[3]["rows"][0] == {"c0": "Reap", "c1": "USD 12,50"}
