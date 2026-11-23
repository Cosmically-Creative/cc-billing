"""Line-item editor: one row per line, server-calculated line amounts, clear labels. Fake sample data only."""
import json
import re

from django.urls import reverse

from billing.models import Document

import pytest

from .test_ui_delivery import draft_data, local_media, owner_client  # noqa: F401  (fixtures)

pytestmark = pytest.mark.django_db


def amounts(response):
    match = re.search(rb'<script id="line-amounts" type="application/json">(.*?)</script>', response.content)
    return json.loads(match.group(1)) if match else None


def test_preview_returns_each_line_amount_by_row(owner_client, customer):
    data = draft_data(customer)
    data.update({'lines-TOTAL_FORMS': '3', 'action': 'preview',
                 'lines-1-description': 'Sample domain', 'lines-1-quantity': '2', 'lines-1-rate': '15.95',
                 'lines-1-discount': '5', 'lines-1-discount_label': 'Sample discount',
                 'lines-2-description': 'Removed sample', 'lines-2-quantity': '1', 'lines-2-rate': '999', 'lines-2-DELETE': 'on'})
    response = owner_client.post('/documents/invoice/new/', data)
    assert response.status_code == 200
    # Server arithmetic: 2 × $15.95 − $5.00; the removed line has no amount and is not in the total.
    assert amounts(response) == {'lines-0': '$100.00', 'lines-1': '$26.90'}
    assert b'$999' not in response.content
    assert Document.objects.count() == 0


def test_preview_with_errors_has_no_line_amounts(owner_client, customer):
    data = draft_data(customer)
    data.update({'action': 'preview', 'lines-0-rate': 'abc'})
    assert amounts(owner_client.post('/documents/invoice/new/', data)) is None


def test_editor_renders_rows_with_clear_labels(owner_client, customer):
    page = owner_client.get('/documents/invoice/new/').content.decode()
    assert 'class="line-head"' in page and page.count('data-line>') >= 2
    product_select = re.search(r'<select name="lines-0-product".*?</select>', page, re.S).group(0)
    assert '>Custom item</option>' in product_select and '---------' not in product_select
    assert 'Add discount' in page and 'Shown on the invoice as' in page
    assert 'Remove line' in page and '<legend>Line' not in page
    # Without the script the Remove checkbox still works.
    assert 'name="lines-0-DELETE"' in page


def test_saved_rates_load_without_dollar_sign(owner_client, customer):
    data = draft_data(customer)
    response = owner_client.post('/documents/invoice/new/', data)
    doc = Document.objects.get()
    page = owner_client.get(reverse('billing:edit_document', args=[doc.pk])).content.decode()
    assert 'name="lines-0-rate" value="100.00"' in page
    assert 'value="$' not in page
