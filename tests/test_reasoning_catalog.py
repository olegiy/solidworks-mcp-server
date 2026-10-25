import asyncio

import pytest


def catalog(*ids):
    return {"models": [{"id": name, "reasoning": {"mandatory": False,
        "default_effort": "high", "supported_efforts": ["max", "high", "low", "none"]}} for name in ids]}


@pytest.mark.parametrize(('name', 'expected'), [
    ('deepseek/deepseek-v4.1-flash-1017', 'deepseek/deepseek-v4.1-flash-1017'),
    ('deepseek-v4.1-flash-1017', 'deepseek/deepseek-v4.1-flash-1017'),
    ('gpt-6.1-sol', 'openai/gpt-6.1-sol'),
    ('kimi-k3.1-pro-highspeed', None),
])
async def test_public_catalog_matches_only_exact_or_unique_model_names(name, expected):
    from cadnext.reasoning import ReasoningCatalog

    async def load():
        return catalog('deepseek/deepseek-v4.1-flash-1017', 'openai/gpt-6.1-sol', 'moonshotai/kimi-k3.1-pro')

    result = await ReasoningCatalog(load).lookup(name)
    assert result['model_id'] == expected
    assert result['status'] == ('matched' if expected else 'not_found')
    assert (result['reasoning'] is not None) is bool(expected)


async def test_ambiguous_short_name_does_not_select_arbitrary_model():
    from cadnext.reasoning import ReasoningCatalog

    async def load():
        return catalog('one/model', 'two/model')

    assert (await ReasoningCatalog(load).lookup('model'))['status'] == 'ambiguous'


async def test_parallel_lookups_share_public_catalog_request():
    from cadnext.reasoning import ReasoningCatalog

    requests = []

    async def load():
        requests.append(True)
        await asyncio.sleep(0)
        return catalog('one/a', 'two/b')

    source = ReasoningCatalog(load)
    first, second = await asyncio.gather(source.lookup('a'), source.lookup('b'))
    assert first['model_id'] == 'one/a'
    assert second['model_id'] == 'two/b'
    assert requests == [True]


async def test_public_catalog_failure_returns_explicit_fallback():
    from cadnext.provider import ProviderError
    from cadnext.reasoning import ReasoningCatalog

    async def load():
        raise ProviderError('API недоступен')

    result = await ReasoningCatalog(load).lookup('model')
    assert result == {'status': 'unavailable', 'model_id': None, 'reasoning': None}
