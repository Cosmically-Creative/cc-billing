"""Tests for the owner appearance (theme) switch, backed by the ccb_theme cookie."""

from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

pytestmark = pytest.mark.django_db

SET_URL = reverse('billing:set_theme')


@pytest.fixture
def owner_client(client):
    user = get_user_model().objects.create_user('sample-owner', is_superuser=True, is_staff=True)
    client.force_login(user)
    session = client.session
    session['ccb_2fa_verified'] = True
    session.save()
    return client


def test_default_render_is_system(owner_client):
    html = owner_client.get(reverse('home')).content.decode()
    assert 'data-theme="system"' in html


@pytest.mark.parametrize('theme', ['light', 'dark', 'system'])
def test_cookie_value_renders(owner_client, theme):
    owner_client.cookies['ccb_theme'] = theme
    html = owner_client.get(reverse('home')).content.decode()
    assert f'data-theme="{theme}"' in html


def test_unknown_cookie_renders_system(owner_client):
    owner_client.cookies['ccb_theme'] = 'purple'
    html = owner_client.get(reverse('home')).content.decode()
    assert 'data-theme="system"' in html
    assert 'data-theme="purple"' not in html


@pytest.mark.parametrize('theme', ['light', 'dark', 'system'])
def test_set_theme_post(owner_client, theme):
    response = owner_client.post(SET_URL, {'theme': theme, 'next': '/settings/'})
    assert response.status_code == 302
    assert response['Location'] == '/settings/'
    cookie = response.cookies['ccb_theme']
    assert cookie.value == theme
    assert str(cookie['max-age']) == '31536000'
    assert cookie['httponly']
    assert cookie['samesite'] == 'Lax'


def test_set_theme_unknown_value_rejected(owner_client):
    response = owner_client.post(SET_URL, {'theme': 'purple', 'next': '/settings/'})
    assert response.status_code == 400
    assert 'ccb_theme' not in response.cookies


@pytest.mark.parametrize('next_url', ['https://evil.example/x', '//evil.example/x'])
def test_set_theme_rejects_foreign_next(owner_client, next_url):
    response = owner_client.post(SET_URL, {'theme': 'dark', 'next': next_url})
    assert response.status_code == 302
    assert response['Location'] == '/'


def test_get_not_allowed(owner_client):
    assert owner_client.get(SET_URL).status_code == 405


def test_anonymous_cannot_set_theme(client):
    response = client.post(SET_URL, {'theme': 'dark', 'next': '/settings/'})
    assert response.status_code != 200
    assert 'ccb_theme' not in response.cookies


def test_dashboard_has_theme_switch_buttons(owner_client):
    html = owner_client.get(reverse('home')).content.decode()
    assert 'class="theme-switch"' in html
    assert 'value="dark"' in html
    assert 'value="light"' in html


def test_settings_marks_current_choice(owner_client):
    owner_client.cookies['ccb_theme'] = 'dark'
    html = owner_client.get(reverse('billing:settings')).content.decode()
    assert 'value="dark" checked' in html
    assert 'value="light" checked' not in html


def test_pdf_template_unaffected():
    path = Path(__file__).resolve().parents[1] / 'billing/templates/billing/pdf/document.html'
    assert 'data-theme' not in path.read_text()
