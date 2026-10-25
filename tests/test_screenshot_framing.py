import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

import solidworks_mcp_server as server


@pytest.mark.parametrize('as_property', [False, True])
def test_screenshot_fits_and_redraws_before_full_frame_capture(monkeypatch, as_property):
    events = []

    class Model:
        def ViewZoomtofit2(self):
            events.append('fit')

        def GraphicsRedraw2(self):
            events.append('redraw')

        def SaveBMP(self, path, width, height):
            events.append(('capture', width, height))
            picture = Image.new('RGB', (160, 80), 'red')
            picture.paste('blue', (80, 0, 160, 80))
            picture.save(path)
            return True

    if as_property:
        Model.ViewZoomtofit2 = property(Model.ViewZoomtofit2)
        Model.GraphicsRedraw2 = property(Model.GraphicsRedraw2)
    monkeypatch.setattr(server, '_model', Model)
    monkeypatch.setattr(server, 'com_call', lambda job: job())
    result = server.screenshot(width=100, height=100, zoom_to_fit=False)
    assert events == ['fit', 'redraw', ('capture', 0, 0)]
    with Image.open(io.BytesIO(result.data)) as picture:
        assert picture.size == (100, 50)
        assert picture.getpixel((0, 25)) == (255, 0, 0)
        assert picture.getpixel((99, 25)) == (0, 0, 255)


def test_failed_fit_never_captures_or_repeats(monkeypatch):
    events = []

    def fail():
        events.append('fit')
        raise RuntimeError('fit failed')

    model = SimpleNamespace(ViewZoomtofit2=fail, SaveBMP=lambda *args: events.append('capture'))
    monkeypatch.setattr(server, '_model', lambda: model)
    monkeypatch.setattr(server, 'com_call', lambda job: job())
    result = json.loads(server.screenshot())
    assert result['status'] == 'error'
    assert events == ['fit']
