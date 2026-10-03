"""FR-001: ``LLMProvider.key_fp`` (read-only, derived from ``api_key`` on every read)."""

from __future__ import annotations

import hashlib

import pytest
from _h009 import FP_RE, KEY_AWORK, KEY_MOEKA, keyed_provider, ref_fp

from nanobot.providers.base import LLMProvider, LLMResponse


class _Minimal(LLMProvider):
    """A direct subclass defining nothing about keys: it must inherit ``key_fp``."""

    async def chat(self, **_kwargs):  # pragma: no cover - never called
        return LLMResponse(content="")

    def get_default_model(self) -> str:
        return "m"


@pytest.mark.fr("FR-001")
def test_terms_definition_exact():
    p = keyed_provider(KEY_MOEKA)
    expected = hashlib.sha256(KEY_MOEKA.encode("utf-8")).hexdigest()[:12]
    assert p.key_fp == expected
    assert FP_RE.match(p.key_fp)


@pytest.mark.fr("FR-001")
def test_lower_hex_length_12():
    fp = keyed_provider(KEY_AWORK).key_fp
    assert isinstance(fp, str) and len(fp) == 12 and fp == fp.lower()
    int(fp, 16)


@pytest.mark.fr("FR-001")
@pytest.mark.parametrize("padded", [
    f"  {KEY_MOEKA}", f"{KEY_MOEKA}\n", f"\t {KEY_MOEKA} \r\n", f"\n{KEY_MOEKA}\t",
])
def test_surrounding_whitespace_is_stripped(padded):
    assert keyed_provider(padded).key_fp == ref_fp(KEY_MOEKA)
    assert keyed_provider(padded).key_fp == keyed_provider(KEY_MOEKA).key_fp


@pytest.mark.fr("FR-001")
def test_inner_whitespace_is_not_stripped():
    inner = "sk-or-test-AAAA KEYWITHSPACEINSIDEXYZ0123"
    assert keyed_provider(inner).key_fp == ref_fp(inner)
    assert keyed_provider(inner).key_fp != ref_fp(inner.replace(" ", ""))


@pytest.mark.fr("FR-001")
@pytest.mark.parametrize("empty", [None, "", " ", "   ", "\n", "\t \r\n"])
def test_no_key_is_none(empty):
    assert keyed_provider(empty).key_fp is None


@pytest.mark.fr("FR-001")
def test_non_ascii_key_hashes_utf8():
    key = "sk-or-test-ÜMLAUT-日本-KEYXYZ0123456789"
    assert keyed_provider(key).key_fp == hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


@pytest.mark.fr("FR-001")
def test_different_keys_differ():
    assert keyed_provider(KEY_MOEKA).key_fp != keyed_provider(KEY_AWORK).key_fp


@pytest.mark.fr("FR-001")
def test_changing_api_key_after_construction_changes_fp():
    p = keyed_provider(KEY_MOEKA)
    first = p.key_fp
    assert first == ref_fp(KEY_MOEKA)
    p.api_key = KEY_AWORK
    assert p.key_fp == ref_fp(KEY_AWORK) != first
    p.api_key = None
    assert p.key_fp is None
    p.api_key = "   "
    assert p.key_fp is None
    p.api_key = KEY_MOEKA
    assert p.key_fp == first


@pytest.mark.fr("FR-001")
def test_key_set_after_keyless_construction():
    p = keyed_provider(None)
    assert p.key_fp is None
    p.api_key = KEY_AWORK
    assert p.key_fp == ref_fp(KEY_AWORK)


@pytest.mark.fr("FR-001")
def test_is_a_read_only_property_on_the_base_class():
    attr = None
    for klass in LLMProvider.__mro__:
        if "key_fp" in vars(klass):
            attr = vars(klass)["key_fp"]
            break
    assert isinstance(attr, property)
    assert attr.fset is None
    p = keyed_provider(KEY_MOEKA)
    with pytest.raises(AttributeError):
        p.key_fp = "0123456789ab"  # type: ignore[misc]
    assert p.key_fp == ref_fp(KEY_MOEKA)


@pytest.mark.fr("FR-001")
def test_direct_subclass_inherits():
    p = _Minimal(api_key=KEY_MOEKA, provider_name="mini")
    assert p.key_fp == ref_fp(KEY_MOEKA)
    assert _Minimal(provider_name="mini").key_fp is None
    assert _Minimal(api_key=" \n", provider_name="mini").key_fp is None


@pytest.mark.fr("FR-001")
def test_concrete_provider_classes_do_not_shadow_it():
    import importlib

    names = [
        ("nanobot.providers.openai_compat_provider", "OpenAICompatProvider"),
        ("nanobot.providers.anthropic_provider", "AnthropicProvider"),
        ("nanobot.providers.azure_openai_provider", "AzureOpenAIProvider"),
        ("nanobot.providers.fallback_provider", "FallbackProvider"),
    ]
    base_attr = next(vars(k)["key_fp"] for k in LLMProvider.__mro__ if "key_fp" in vars(k))
    checked = 0
    for mod_name, cls_name in names:
        try:
            cls = getattr(importlib.import_module(mod_name), cls_name)
        except Exception:  # noqa: BLE001 - optional extras may be missing
            continue
        assert issubclass(cls, LLMProvider)
        found = next((vars(k)["key_fp"] for k in cls.__mro__ if "key_fp" in vars(k)), None)
        assert isinstance(found, property), cls_name
        if found is not base_attr:  # an override must still be a read-only property
            assert found.fset is None, cls_name
        checked += 1
    assert checked >= 2


@pytest.mark.fr("FR-001")
def test_anthropic_provider_key_fp():
    try:
        from nanobot.providers.anthropic_provider import AnthropicProvider

        p = AnthropicProvider(api_key="sk-ant-test-AAAAKEYZXCVBNMQWERTY0123")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"AnthropicProvider not constructible offline: {type(exc).__name__}")
    assert p.key_fp == ref_fp("sk-ant-test-AAAAKEYZXCVBNMQWERTY0123")
