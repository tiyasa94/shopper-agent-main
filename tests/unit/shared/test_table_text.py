import pytest

from shared.table_text import rewrite_markdown_tables


def _rewrite(text):
    return rewrite_markdown_tables(
        text,
        row_headers=frozenset({"item", "items"}),
        value_labels={"quantity": "Quantity", "price": "Unit price", "tax": "Tax"},
        required_headers=frozenset({"quantity"}),
        group_label="Category",
        row_label="Item",
        note="Only explicitly headed rows are available.",
    )


def test_rewriting_uses_configured_labels_order_and_literal_values():
    text = (
        "## Order\n"
        "| Items | Price | Tax | Quantity |\n| - | - | - | - |\n"
        "| Stationery | Stationery | Stationery | Stationery |\n"
        "| Pens | $12 per pack | $2 | 5 |\n"
        "| Paper | $20* | exempt | 2 |\n\n*Special handling applies."
    )
    assert _rewrite(text) == (
        "## Order\n\n*Special handling applies.\n\n"
        "Only explicitly headed rows are available.\n\n"
        "Section: Order. Category: Stationery. Item: Pens. "
        "Quantity: 5. Unit price: $12 per pack. Tax: $2.\n\n"
        "Section: Order. Category: Stationery. Item: Paper. "
        "Quantity: 2. Unit price: $20*. Tax: exempt."
    )


@pytest.mark.parametrize(
    "text",
    [
        "No table here.\n",
        "| Paper | 1 | $20 |\n",
        "| Item | Quantity | Quantity |\n| - | - | - |\n| Paper | 1 | 2 |",
        "| Item | | Price |\n| - | - | - |\n| Paper | 1 | $20 |",
        "| Item | Quantity |\n| - | - | - |\n| Paper | 1 |",
        "| Item | Quantity |\n| no separator | 1 |",
        "| Item | Unknown | Quantity |\n| - | - | - |\n| Paper | $20 | 1 |",
        "| Item | Price |\n| - | - |\n| Paper | $20 |",
        "| Item | Quantity |",
    ],
)
def test_unheaded_and_ambiguous_tables_do_not_become_evidence(text):
    assert "Item:" not in _rewrite(text)


def test_headers_do_not_carry_across_sections_or_malformed_rows():
    text = (
        "| Item | Quantity |\n| - | - |\n| Paper | 1 |\n"
        "## Another section\n| Unheaded | 2 |\n"
        "| Items | Price | Quantity |\n| - | - | - |\n| Pens | $20 | 3 |\n"
        "| Missing value | | 4 |\n"
        "| Wrong width | $10 | 5 | unexpected |\n| Unknown owner | 6 |"
    )
    rendered = _rewrite(text)
    assert "Item: Paper. Quantity: 1." in rendered
    assert (
        "Section: Another section. Category: . Item: Pens. Quantity: 3. Unit price: $20."
        in rendered
    )
    assert rendered.count("Item:") == 2


def test_header_normalization_preserves_escaped_values():
    text = "| &nbsp;ITEM&nbsp; | QUANTITY |\n| :--- | ---: |\n| Paper \\| card | 1 |"
    assert "Item: Paper \\| card. Quantity: 1." in _rewrite(text)
