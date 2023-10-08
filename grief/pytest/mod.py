import pytest
from grief.core import modlog

__all__ = ["mod"]


@pytest.fixture
async def mod(config, monkeypatch, red):
    from grief import Config

    with monkeypatch.context() as m:
        m.setattr(Config, "get_conf", lambda *args, **kwargs: config)

        await modlog._init(red)
        return modlog
