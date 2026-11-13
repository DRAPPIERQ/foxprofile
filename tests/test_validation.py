import os

import pytest

from src.utils.proxy_parser import parse_proxy
from src.utils.validation import validate_profile_name, validate_proxy_format


@pytest.mark.parametrize("name", ["shop-01", "TikTok US 2", "ads_agency.3", "tài-khoản-1"])
def test_valid_profile_names(name):
    assert validate_profile_name(name) == (True, "")


@pytest.mark.parametrize(
    "name",
    ["", " lead", "trail ", "a/b", "a\\b", "x" * 65, "CON", "com1", ".", "..", "name."],
)
def test_invalid_profile_names(name):
    ok, msg = validate_profile_name(name)
    assert not ok and msg


@pytest.mark.parametrize(
    "proxy",
    [
        "",
        "1.2.3.4:8080",
        "http://h.com:80",
        "socks5://u:p@1.2.3.4:1080",
        "https://proxy.vn:443",
        "1.2.3.4:5062:user:secret",
        "socks5://1.2.3.4:1080:user:secret",
    ],
)
def test_valid_proxies(proxy):
    assert validate_proxy_format(proxy)[0]


@pytest.mark.parametrize(
    "proxy", ["nohost", "ftp://h:21", "h:0", "h:70000", "u:p@h", "1.2.3.4:5062:user:", "h:x:u:p"]
)
def test_invalid_proxies(proxy):
    assert not validate_proxy_format(proxy)[0]


def test_parse_proxy_with_auth():
    assert parse_proxy("socks5://user:pass@1.2.3.4:1080") == {
        "server": "socks5://1.2.3.4:1080",
        "username": "user",
        "password": "pass",
    }


def test_parse_proxy_host_port_user_pass():
    assert parse_proxy("1.2.3.4:5062:user:secret") == {
        "server": "http://1.2.3.4:5062",
        "username": "user",
        "password": "secret",
    }
    assert parse_proxy("socks5://1.2.3.4:1080:user:secret")["server"] == "socks5://1.2.3.4:1080"


def test_parse_proxy_defaults_to_http():
    assert parse_proxy("1.2.3.4:8080") == {"server": "http://1.2.3.4:8080"}


def test_parse_proxy_empty():
    assert parse_proxy("") is None


# ---------------------------------------------------------------------------
# Proxy host-length and port-range bounds (fix 2)
# ---------------------------------------------------------------------------


def test_proxy_host_exactly_253_chars_valid():
    host = "a" * 63 + "." + "b" * 63 + "." + "c" * 63 + "." + "d" * 61  # 253 chars
    assert len(host) == 253
    ok, _ = validate_proxy_format(f"{host}:8080")
    assert ok


def test_proxy_host_254_chars_rejected():
    host = "a" * 63 + "." + "b" * 63 + "." + "c" * 63 + "." + "d" * 62  # 254 chars
    assert len(host) == 254
    ok, _ = validate_proxy_format(f"{host}:8080")
    assert not ok


def test_proxy_host_9000_chars_rejected():
    host = "x" * 9000
    ok, _ = validate_proxy_format(f"{host}:8080")
    assert not ok


@pytest.mark.parametrize("port", ["0", "99999"])
def test_proxy_port_out_of_range_rejected(port):
    ok, _ = validate_proxy_format(f"192.0.2.1:{port}")
    assert not ok


# Windows treats a device name followed by an extension ("NUL.txt") as the device.
@pytest.mark.parametrize("name", ["CON.txt", "con.shop", "nul.tar.gz", "LPT1.backup", "aux .x"])
def test_device_names_with_an_extension_are_refused_for_new_profiles(name):
    ok, msg = validate_profile_name(name)
    assert not ok and msg
    # A profile that already has such a name (made on Linux or macOS) keeps working.
    assert validate_profile_name(name, existing=True) == (True, "")


@pytest.mark.parametrize("name", ["console", "nullable.1", "com10", "aux-shop"])
def test_names_that_only_start_like_a_device_are_fine(name):
    assert validate_profile_name(name) == (True, "")


def test_existing_device_like_profile_loads_and_can_be_edited(tmp_path, monkeypatch):
    import json

    from fastapi.testclient import TestClient

    from src.api.app import create_app
    from src.core.container import Container
    from src.services.profile.manager import ProfileManager

    monkeypatch.chdir(tmp_path)
    (tmp_path / "profiles.json").write_text(
        json.dumps({"con.shop": {"name": "con.shop", "proxy": None, "os_type": "linux"}}),
        encoding="utf-8",
    )
    assert "con.shop" in ProfileManager().profiles

    client = TestClient(create_app(Container()), base_url="http://127.0.0.1")
    # Saving it with its own name is allowed; renaming another profile to such a name is not.
    r = client.patch("/api/v1/profiles/con.shop", json={"name": "con.shop", "locale": "vi-VN"})
    assert r.status_code == 200, r.text
    assert client.post("/api/v1/profiles", json={"name": "shop"}).status_code == 201
    assert client.patch("/api/v1/profiles/shop", json={"name": "nul.shop"}).status_code == 400


@pytest.mark.parametrize(
    ("name", "plain", "with_extension"),
    [
        ("CON", True, True),
        ("con.shop", False, True),
        ("console", False, False),
        ("com1", True, True),
    ],
)
def test_is_reserved_name(name, plain, with_extension):
    from src.utils.validation import is_reserved_name

    assert is_reserved_name(name, with_extension=False) is plain
    assert is_reserved_name(name) is with_extension


@pytest.mark.skipif(os.name == "nt", reason="Windows refuses such a folder name")
def test_importing_a_device_like_profile_works_where_its_folder_can_exist(tmp_path):
    import json
    import zipfile

    from src.services.profile.transfer import import_from_zip

    archive = tmp_path / "nul.shop.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("profile.json", json.dumps({"name": "nul.shop", "os_type": "linux"}))
    ok, result = import_from_zip(str(archive), str(tmp_path / "data"))
    assert ok, result
    assert result.name == "nul.shop"
