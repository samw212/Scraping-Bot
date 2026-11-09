from __future__ import annotations

import pytest
import yaml

from scrapebot.config import Config, ConfigError, interpolate_env, load_config
from scrapebot.profiles import BrowserVersion, accept_language, brand_version_list, resolve_profile

from .conftest import ROOT, base_config


def test_example_config_loads():
    config = load_config(ROOT / "config" / "config.example.yaml")
    assert len(config.profiles) >= 3
    assert [p.name for p in config.proxies.gluetun] == ["vpn-1", "vpn-2", "vpn-3"]
    assert config.proxies.gluetun[0].api_key is None  # ${GLUETUN_API_KEY:-} -> empty -> None
    assert config.target("quotes").fields["tags"].multiple is True
    assert config.target("quotes").fields["text"].selector == ".text"  # shorthand


def test_env_interpolation(monkeypatch):
    monkeypatch.setenv("SB_KEY", "secret")
    assert interpolate_env({"a": "x-${SB_KEY}", "b": ["${SB_MISSING:-dflt}"]}) == {"a": "x-secret", "b": ["dflt"]}
    with pytest.raises(ConfigError):
        interpolate_env("${SB_DEFINITELY_MISSING}")


def test_refuses_direct_scraping_without_opt_in(tmp_path):
    raw = base_config(tmp_path)
    raw["proxies"] = {}
    with pytest.raises(ValueError, match="allow_direct"):
        Config.model_validate(raw)


def test_follow_target_must_exist(tmp_path):
    raw = base_config(tmp_path)
    raw["targets"][0]["follow"] = {"selector": "a", "target": "nope"}
    with pytest.raises(ValueError, match="unknown target"):
        Config.model_validate(raw)


def test_overrides_and_profiles_file(tmp_path):
    raw = base_config(tmp_path)
    profiles = raw.pop("profiles")
    (tmp_path / "profiles.yaml").write_text(yaml.safe_dump({"profiles": profiles}))
    raw["profiles_file"] = "profiles.yaml"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    config = load_config(path, {"limits.max_pages_total": 7, "orchestrator.workers": 5})
    assert [p.name for p in config.profiles] == ["test-win", "test-win-2"]
    assert config.limits.max_pages_total == 7
    assert config.orchestrator.workers == 5


@pytest.mark.parametrize(
    "major, expected",
    [
        # Values observed from real Google Chrome releases.
        (120, '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"'),
        (124, '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'),
        (131, '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"'),
    ],
)
def test_grease_brands_match_real_chrome(major, expected):
    header = ", ".join(f'"{b["brand"]}";v="{b["version"]}"' for b in brand_version_list(major, "Google Chrome"))
    assert header == expected


def test_full_version_list():
    brands = brand_version_list(131, "Google Chrome", "131.0.6778.86")
    assert {"brand": "Google Chrome", "version": "131.0.6778.86"} in brands
    assert {"brand": "Not_A Brand", "version": "24.0.0.0"} in brands


def test_accept_language():
    assert accept_language(["en-US", "en"]) == "en-US,en;q=0.9"
    assert accept_language(["de-DE", "de", "en-US", "en"]) == "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7"


def test_resolve_profile(tmp_path):
    config = Config.model_validate(base_config(tmp_path))
    profile = config.profiles[0].model_copy(update={"timezone": "auto"})
    resolved = resolve_profile(profile, BrowserVersion.parse("153.0.8010.12"), timezone="Asia/Tokyo", identity_key="a@b")
    assert "Chrome/153.0.0.0" in resolved.user_agent
    assert resolved.timezone == "Asia/Tokyo"
    meta = resolved.ua_metadata()
    assert meta["platform"] == "Windows" and meta["fullVersion"] == "153.0.8010.12"
    # No proxy geo -> fallback timezone; noise seed is stable per identity.
    again = resolve_profile(profile, BrowserVersion.parse("153.0.1.2"), timezone=None, identity_key="a@b")
    assert again.timezone == profile.fallback_timezone
    assert again.noise_seed == resolved.noise_seed
    assert resolve_profile(profile, BrowserVersion.parse("153"), timezone=None, identity_key="other").noise_seed != resolved.noise_seed
    opts = resolved.fingerprint_options(hardware=True, screen=True, webgl=True, canvas=True, audio=False)
    assert opts["screen"]["availHeight"] == 728
    assert opts["webgl"]["parameters"] == {"3379": 16384}


def test_profile_rejects_viewport_larger_than_screen(tmp_path):
    raw = base_config(tmp_path)
    raw["profiles"][0] = {**raw["profiles"][0], "viewport": {"width": 4000, "height": 650}}
    with pytest.raises(ValueError, match="viewport"):
        Config.model_validate(raw)
